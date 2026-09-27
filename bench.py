"""
1本分のデータ取り: uvicorn を起動 → probe で観測しながら hey で負荷 → 後処理が終わるまで見届けて保存

  python bench.py sync                                                  # results/sync/ に保存
  python bench.py blocking --name blocking-backlog128 -- --backlog 128  # -- の後ろは uvicorn に渡す
  python bench.py async -n 100 -c 100 --name async-c100                 # 100 本を一度に
  python bench.py sync --live                                           # ブラウザでグラフをリアルタイムに見ながら

保存するもの（results/<name>/）
  hey.csv     hey -o csv の出力。応答が返ったリクエストだけが並ぶ（タイムアウトや接続エラーの行はない）
  probe.csv   probe.py の観測値
  server.log  uvicorn の出力（/async の TimeoutError のトレースバックなど）
  meta.json   実行条件と時刻（hey_start でグラフの時間軸をそろえる）

uvicorn は毎回起動し直す。/blocking の後は処理待ちが何十分も残り、/sync の後も数分は処理が続くので、
前の実験の残りを次の実験に持ち込まないため。T1 で uvicorn を動かしているなら止めてから実行する。
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
import subprocess
import sys
import time
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from app import POOL
from probe import CsvLog, LiveServer, PgCounter, Probe, format_row, get_stats, ticks

ROOT = Path(__file__).resolve().parent
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


def preflight(port):
    hey = shutil.which("hey")
    if not hey:
        fail("hey が見つからない（macOS: brew install hey / Linux: go install github.com/rakyll/hey@latest）")
    if port_in_use(port):
        fail(f"ポート {port} は使用中。T1 の uvicorn や前回の残りを止めてから実行する")
    pg = PgCounter()
    active, _ = pg.sample()
    if active is None:
        fail("Postgres につながらない。docker compose up -d を実行する（DSN は app.py）")
    # 前の実験で強制終了したサーバーの pg_sleep(5) が DB 側に残っていることがある
    deadline = time.monotonic() + 15
    while active and time.monotonic() < deadline:
        print(f"前の実験の pg_sleep が {active} 本残っているので終わるのを待つ")
        time.sleep(1)
        active, _ = pg.sample()
    return hey


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


def summarize(run_dir, n, peaks):
    with open(run_dir / "hey.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    codes = Counter(int(r["status-code"]) for r in rows)
    ok = sorted(float(r["response-time"]) for r in rows if r["status-code"] == "200")
    parts = [f"{code}: {count}" for code, count in sorted(codes.items())]
    parts.append(f"応答なし: {n - len(rows)}")
    print(f"\n== {run_dir.name}: {n} リクエスト ==")
    print("  " + "   ".join(parts))
    if ok:
        pct = lambda p: ok[min(len(ok) - 1, int(len(ok) * p / 100))]
        print(f"  200 の応答時間  p50 {pct(50):.1f}s  p90 {pct(90):.1f}s  最大 {ok[-1]:.1f}s")
    print(
        "  最大値  "
        + "  ".join(f"{k} {'-' if v is None else v}" for k, v in peaks.items())
    )


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
    parser.add_argument("-n", type=int, default=1000, help="リクエスト数")
    parser.add_argument("-c", type=int, default=1000, help="並列数")
    parser.add_argument("-t", type=int, default=60, help="クライアント側のタイムアウト（秒）")
    parser.add_argument("--interval", type=float, default=0.5, help="観測の間隔（秒）")
    parser.add_argument("--pre", type=float, default=3, help="負荷をかける前に観測しておく秒数")
    parser.add_argument("--max-drain", type=float, default=600, help="hey の後、後処理を見届ける最大秒数")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--live", action="store_true", help="観測値をブラウザでグラフにしてリアルタイムに見る")
    parser.add_argument("--live-port", type=int, default=8001, help="ライブ表示のポート")
    args = parser.parse_args(argv)

    # 端末を閉じたときや kill されたときも Ctrl+C と同じ後片付け（uvicorn を止める）をする
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.default_int_handler)
    nofile = raise_nofile()
    hey_path = preflight(args.port)
    run_dir = ROOT / "results" / (args.name or args.endpoint)
    run_dir.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{args.port}/{args.endpoint}"
    hey_cmd = [hey_path, "-n", str(args.n), "-c", str(args.c), "-t", str(args.t), "-o", "csv", url]

    live = LiveServer(args.live_port) if args.live else None
    server_log = open(run_dir / "server.log", "w")
    server, server_cmd = start_server(args.port, uvicorn_args, server_log)

    probe = Probe(args.port)
    probe_log = CsvLog(run_dir / "probe.csv")
    hey_out = open(run_dir / "hey.csv", "w")
    hey = None
    meta = {
        "endpoint": args.endpoint,
        "url": url,
        "requests": args.n,
        "concurrency": args.c,
        "client_timeout": args.t,
        "hey": " ".join(["hey", *hey_cmd[1:]]),
        "uvicorn": " ".join(["uvicorn", *server_cmd[3:]]),
        "pool": POOL,
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
        "packages": packages(),
        "somaxconn": somaxconn(),
        "nofile": nofile,
    }
    print(f"起動: {meta['uvicorn']}")
    peaks = dict.fromkeys(["qlen", "threads", "tasks", "sync_checked_out", "async_checked_out", "pg_active"])
    phase = "pre"  # pre → load → drain
    started = time.monotonic()
    idle_tasks = 0
    idle_count = 0
    last_ok = started
    stop_reason = "interrupted"
    try:
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
                    print(f"--- hey 開始: {args.n} リクエスト / {args.c} 並列 / タイムアウト {args.t} 秒 → {url}")
                    meta["hey_start"] = time.time()
                    hey = subprocess.Popen(hey_cmd, stdout=hey_out)
                    phase = "load"
                    if live:
                        live.event("hey 開始")
            elif phase == "load":
                if hey.poll() is not None:
                    meta["hey_end"] = time.time()
                    took = meta["hey_end"] - meta["hey_start"]
                    print(f"--- hey 終了（{took:.1f} 秒）。サーバー側の後処理を見届ける（Ctrl+C で打ち切り）")
                    phase = "drain"
                    drain_started = now
                    if live:
                        live.event("hey 終了")
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
        stop_server(server)
        for f in (probe_log, hey_out, server_log):
            f.close()
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")

    if live:
        time.sleep(1.5)  # 開いているページが最後のサンプルと出来事を取りに来るのを待ってから終わる
    if "hey_end" in meta:
        summarize(run_dir, args.n, peaks)
    print(f"\n保存先: {run_dir.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
