"""
データ取り: uvicorn を起動 → probe で観測しながら hey で負荷 → 後処理が終わるまで見届けて保存

  python bench.py sync                                                  # results/sync/1/ に保存
  python bench.py async --repeat 3                                      # 3 回回して results/async/1〜3/ に。中央値と幅も出す
  python bench.py blocking --name blocking-backlog128 -- --backlog 128  # -- の後ろは uvicorn に渡す
  python bench.py async -n 100 -c 100 --name async-c100                 # 100 本を一度に
  python bench.py sync --live                                           # ブラウザでグラフをリアルタイムに見ながら

保存するもの（results/<name>/<何回目>/）
  hey.csv     hey -o csv の出力。応答が返ったリクエストだけが並ぶ（タイムアウトや接続エラーの行はない）
  probe.csv   probe.py の観測値
  server.log  uvicorn の出力（/async の TimeoutError のトレースバックなど）
  meta.json   実行条件と時刻（hey_start でグラフの時間軸をそろえる）

同じ名前で回し直すと、前回の results/<name>/<数字>/ は消して取り直す。残したいときは --name を変える。

uvicorn は 1 回ごとに起動し直す。/blocking の後は処理待ちが何十分も残り、/sync の後も数分は処理が続くので、
前の回の残りを次の回に持ち込まないため。T1 で uvicorn を動かしているなら止めてから実行する。

環境変数（stable.env や compose.yaml が設定する）
  HEY_TIMEOUT                              -t の既定値（60）
  RESULTS_DIR                              保存先のルート（results）
  DSN / POOL_SIZE / MAX_OVERFLOW / POOL_TIMEOUT   app.py が読む
"""
import argparse
import csv
import json
import os
import platform
import resource
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from app import POOL
from probe import CsvLog, LiveServer, Probe, format_row, get_stats, ticks

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / os.environ.get("RESULTS_DIR", "results")
ENDPOINTS = ["baseline", "blocking", "sync", "async"]
PACKAGES = ["fastapi", "starlette", "uvicorn", "anyio", "h11", "sqlalchemy", "greenlet", "psycopg"]
NOFILE = 10240  # macOS の ulimit -n 256 では 1000 並列を開けない
STUCK_AFTER = 10  # hey の後、/stats がこの秒数返らなければ見届けを打ち切る（/blocking）


def fail(msg):
    sys.exit(f"bench.py: {msg}")


def raise_nofile():
    """ulimit -n 10240 の代わり。uvicorn と hey はこのプロセスの上限を引き継ぐ"""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < NOFILE:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(NOFILE, hard), hard))
        except (ValueError, OSError) as e:
            print(f"注意: ファイルディスクリプタの上限を上げられなかった（{soft}）: {e}")
    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


def packages():
    found = {}
    for name in PACKAGES:
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            found[name] = None
    return found


def somaxconn():
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "kern.ipc.somaxconn"], capture_output=True, text=True)
            return int(out.stdout)
        return int(Path("/proc/sys/net/core/somaxconn").read_text())
    except (OSError, ValueError):
        return None


def port_in_use(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def preflight(port, pg):
    if port_in_use(port):
        fail(f"ポート {port} は使用中。T1 の uvicorn や前回の残りを止めてから実行する")
    active, _ = pg.sample()
    if active is None:
        fail("Postgres につながらない。docker compose up -d を実行する（接続先は app.py の DSN）")
    # 前の回で強制終了したサーバーの pg_sleep(5) が DB 側に残っていることがある
    deadline = time.monotonic() + 15
    while active and time.monotonic() < deadline:
        print(f"前の回の pg_sleep が {active} 本残っているので終わるのを待つ")
        time.sleep(1)
        active, _ = pg.sample()


def start_server(port, uvicorn_args, log):
    cmd = [sys.executable, "-m", "uvicorn", "app:app", "--port", str(port), "--no-access-log", *uvicorn_args]
    # 新しいプロセスグループにして、--workers のときも子プロセスごと止められるようにする
    server = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server.poll() is not None:
            fail(f"uvicorn が起動しなかった。{log.name} を見る")
        if get_stats(port, 0.5)[0] is not None:
            return server, cmd
        time.sleep(0.2)
    stop_server(server)
    fail(f"uvicorn が 15 秒たっても応答しない。{log.name} を見る")


def stop_server(server):
    """/blocking の後は SIGTERM では止まらない（ループが止まっている）ので、待って SIGKILL"""
    try:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(5)
            except subprocess.TimeoutExpired:
                pass
        os.killpg(server.pid, signal.SIGKILL)  # 残っているもの（--workers の子プロセスなど）ごと止める
    except ProcessLookupError:
        pass
    server.wait()


def is_idle(row, idle_tasks):
    return (
        row.get("tasks") is not None
        and row["tasks"] <= idle_tasks
        and row.get("sync_checked_out") == 0
        and row.get("async_checked_out") == 0
        and not row.get("pg_active")
    )


def run_once(args, run_dir, meta, probe, uvicorn_args, live):
    """1 回分を回して results/<name>/<i>/ に保存する。meta（stop_reason 付き）と負荷中の最大値を返す"""
    run_dir.mkdir(parents=True)
    url = meta["url"]
    hey_cmd = [shutil.which("hey"), "-n", str(args.n), "-c", str(args.c), "-t", str(args.t), "-o", "csv", url]
    meta["hey"] = " ".join(["hey", *hey_cmd[1:]])
    tag = f"（{meta['repeat']}/{args.repeat}）" if args.repeat > 1 else ""
    server_log = open(run_dir / "server.log", "w")
    probe_log = CsvLog(run_dir / "probe.csv")
    hey_out = open(run_dir / "hey.csv", "w")
    server = hey = None
    peaks = dict.fromkeys(["qlen", "threads", "tasks", "sync_checked_out", "async_checked_out", "pg_active"])
    stop_reason = "interrupted"
    try:
        server, server_cmd = start_server(args.port, uvicorn_args, server_log)
        meta["uvicorn"] = " ".join(["uvicorn", *server_cmd[3:]])
        print(f"起動: {meta['uvicorn']}")
        phase = "pre"  # pre → load → drain
        started = time.monotonic()
        idle_tasks = 0
        idle_count = 0
        last_ok = started
        for _ in ticks(args.interval):
            row = probe.sample()
            probe_log.write(row)
            print(format_row(row), flush=True)
            if live:
                live.add(row)
            now = time.monotonic()
            for key, peak in peaks.items():
                # 負荷をかけ始めてからの最大値。/stats が一度も返らなければ None のまま（B）
                if phase != "pre" and row.get(key) is not None and (peak is None or row[key] > peak):
                    peaks[key] = row[key]
            if row.get("tasks") is not None:
                last_ok = now
            if server.poll() is not None:
                stop_reason = "server_exited"
                print(f"--- uvicorn が終了した（{server_log.name} を見る）")
                break

            if phase == "pre":
                idle_tasks = max(idle_tasks, row.get("tasks") or 0)
                if now - started >= args.pre:
                    print(f"--- hey 開始{tag}: {args.n} リクエスト / {args.c} 並列 / タイムアウト {args.t} 秒 → {url}")
                    meta["hey_start"] = time.time()
                    hey = subprocess.Popen(hey_cmd, stdout=hey_out)
                    phase = "load"
                    if live:
                        live.event(f"hey 開始{tag}")
            elif phase == "load":
                if hey.poll() is not None:
                    meta["hey_end"] = time.time()
                    took = meta["hey_end"] - meta["hey_start"]
                    print(f"--- hey 終了（{took:.1f} 秒）。サーバー側の後処理を見届ける（Ctrl+C で打ち切り）")
                    phase = "drain"
                    drain_started = now
                    if live:
                        live.event(f"hey 終了{tag}")
            if phase == "drain":
                idle_count = idle_count + 1 if is_idle(row, idle_tasks) else 0
                if idle_count >= 2:
                    stop_reason = "idle"
                    print("--- サーバーがアイドルに戻った")
                    break
                if now - last_ok >= STUCK_AFTER:
                    stop_reason = "unresponsive"
                    print(f"--- /stats が {STUCK_AFTER} 秒返らないので打ち切り（ループが止まったまま）")
                    break
                if now - drain_started >= args.max_drain:
                    stop_reason = "max_drain"
                    print(f"--- {args.max_drain:.0f} 秒見届けたので打ち切り（--max-drain）")
                    break
    except KeyboardInterrupt:
        print("--- Ctrl+C で打ち切り")
    finally:
        meta["drain_end"] = time.time()
        meta["stop_reason"] = stop_reason
        if hey and hey.poll() is None:
            hey.kill()
            hey.wait()
        if server:
            stop_server(server)
        for f in (probe_log, hey_out, server_log):
            f.close()
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    return meta, peaks


def outcome(run_dir, n):
    """hey.csv から 200 / 500 / その他のステータス / 応答なし の件数と、200 の応答時間（昇順）"""
    with open(run_dir / "hey.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    codes = Counter(int(r["status-code"]) for r in rows)
    ok = sorted(float(r["response-time"]) for r in rows if r["status-code"] == "200")
    counts = {"200": codes[200], "500": codes[500], "その他": len(rows) - codes[200] - codes[500], "応答なし": n - len(rows)}
    return counts, ok


def print_run(run_dir, n, counts, ok, peaks):
    print(f"\n== {run_dir.parent.name}/{run_dir.name}: {n} リクエスト ==")
    print("  " + "   ".join(f"{k}: {v}" for k, v in counts.items() if v or k in ("200", "応答なし")))
    if ok:
        pct = lambda p: ok[min(len(ok) - 1, int(len(ok) * p / 100))]
        print(f"  200 の応答時間  p50 {pct(50):.1f}s  p90 {pct(90):.1f}s  最大 {ok[-1]:.1f}s")
    print("  最大値  " + "  ".join(f"{k} {'-' if v is None else v}" for k, v in peaks.items()))


def print_repeats(group, all_counts):
    keys = [k for k in all_counts[0] if any(c[k] for c in all_counts) or k in ("200", "応答なし")]

    def line(label, values):
        print(f"  {label}  " + "   ".join(f"{k}: {v}" for k, v in zip(keys, values)))

    print(f"\n== {group}: {len(all_counts)} 回のまとめ ==")
    for i, c in enumerate(all_counts, 1):
        line(f"{i:>2} 回目", [c[k] for k in keys])
    line("中央値", [f"{statistics.median(c[k] for c in all_counts):g}" for k in keys])
    line("  幅  ", [f"{min(c[k] for c in all_counts)}-{max(c[k] for c in all_counts)}" for k in keys])


def shown(path):
    """画面に出すパス。リポジトリの中なら相対パスにする"""
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def main():
    # -- の後ろは uvicorn に渡す。argparse に任せると --name などの後ろで取りこぼすので、先に切り分ける
    argv = sys.argv[1:]
    uvicorn_args = argv[argv.index("--") + 1:] if "--" in argv else []
    argv = argv[:argv.index("--")] if "--" in argv else argv

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="python bench.py {%s} [options] [-- uvicorn の引数 ...]" % ",".join(ENDPOINTS),
    )
    parser.add_argument("endpoint", choices=ENDPOINTS)
    parser.add_argument("--name", help="保存先 results/<name>/（省略時はエンドポイント名）")
    parser.add_argument("--repeat", type=int, default=1, help="同じ条件で何回回すか")
    parser.add_argument("-n", type=int, default=1000, help="リクエスト数")
    parser.add_argument("-c", type=int, default=1000, help="並列数")
    parser.add_argument("-t", type=int, default=int(os.environ.get("HEY_TIMEOUT", 60)),
                        help="クライアント側のタイムアウト（秒）。既定は環境変数 HEY_TIMEOUT か 60（今は %(default)s）")
    parser.add_argument("--interval", type=float, default=0.5, help="観測の間隔（秒）")
    parser.add_argument("--pre", type=float, default=3, help="負荷をかける前に観測しておく秒数")
    parser.add_argument("--max-drain", type=float, default=600, help="hey の後、後処理を見届ける最大秒数")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--live", action="store_true", help="観測値をブラウザでグラフにしてリアルタイムに見る")
    parser.add_argument("--live-port", type=int, default=8001, help="ライブ表示のポート")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat は 1 以上")

    # 端末を閉じたときや kill されたときも Ctrl+C と同じ後片付け（uvicorn を止める）をする
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.default_int_handler)
    if not shutil.which("hey"):
        fail("hey が見つからない（macOS: brew install hey / Linux: go install github.com/rakyll/hey@latest）")
    nofile = raise_nofile()
    probe = Probe(args.port)
    preflight(args.port, probe.pg)

    group = args.name or args.endpoint
    group_dir = RESULTS / group
    old = sorted(d for d in group_dir.glob("*") if d.is_dir() and d.name.isdigit()) if group_dir.exists() else []
    if old:
        print(f"前回の結果（{shown(group_dir)}/ の {len(old)} 回分）を消して取り直す")
        for d in old:
            shutil.rmtree(d)
    live = LiveServer(args.live_port) if args.live else None
    base = {
        "endpoint": args.endpoint,
        "group": group,
        "repeats": args.repeat,
        "url": f"http://127.0.0.1:{args.port}/{args.endpoint}",
        "requests": args.n,
        "concurrency": args.c,
        "client_timeout": args.t,
        "pool": POOL,
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "container": Path("/.dockerenv").exists(),
        "python": platform.python_version(),
        "packages": packages(),
        "somaxconn": somaxconn(),
        "nofile": nofile,
    }

    all_counts = []
    try:
        for i in range(1, args.repeat + 1):
            if i > 1:
                preflight(args.port, probe.pg)
            if args.repeat > 1:
                print(f"\n=== {i}/{args.repeat} 回目 ===")
            run_dir = group_dir / str(i)
            meta, peaks = run_once(args, run_dir, {**base, "repeat": i}, probe, uvicorn_args, live)
            if "hey_end" in meta:
                counts, ok = outcome(run_dir, args.n)
                print_run(run_dir, args.n, counts, ok, peaks)
                all_counts.append(counts)
            if meta["stop_reason"] in ("interrupted", "server_exited"):
                break
        if live:
            time.sleep(1.5)  # 開いているページが最後のサンプルと出来事を取りに来るのを待ってから終わる
    except KeyboardInterrupt:
        print("--- Ctrl+C で打ち切り")
    if len(all_counts) > 1:
        print_repeats(group, all_counts)
    print(f"\n保存先: {shown(group_dir)}/")


if __name__ == "__main__":
    main()
