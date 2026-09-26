"""
観測プローブ: ①②③ と DB 側を一定間隔で測り、画面に出しつつ CSV に書く

  ① accept queue            macOS: netstat -Lan / Linux: ss -ltn（なければ /proc/net/tcp）
  ②③ threads・tasks・プール  GET /stats（返ってこなければ空欄。/blocking 中はそれ自体が観測結果）
  DB 側                      pg_stat_activity（アプリのプールとは別に、専用の接続を1本張って数える）

使い方:
  python probe.py                          # 画面に出すだけ（T2 の観測用）
  python probe.py results/sync/probe.csv   # CSV にも書く。Ctrl+C で止める
"""
import argparse
import csv
import http.client
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psycopg

from app import DSN

FIELDS = [
    "timestamp",          # UNIX 時刻（秒）
    "qlen", "qmax",       # ① accept queue の今の長さ / 上限（ss がない Linux では上限は空欄）
    "threads", "tasks",   # ② /stats
    "sync_checked_out", "async_checked_out",  # ③ /stats
    "stats_ms",           # /stats の応答時間。空欄は timeout 秒以内に返らなかった
    "pg_active",          # DB 側: pg_sleep を実行中の接続数
    "pg_conns",           # DB 側: クライアント接続の総数（プローブ自身は除く）
]

# 環境変数の HTTP_PROXY などを無視して localhost に直接つなぐ
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PG_SQL = """
select count(*) filter (where state = 'active' and query ilike '%pg_sleep%'),
       count(*)
from pg_stat_activity
where backend_type = 'client backend' and pid <> pg_backend_pid()
"""


def parse_netstat_listen(text, port):
    """macOS の netstat -Lan の出力から (qlen, maxqlen) を取り出す

    Listen         Local Address
    128/0/128      127.0.0.1.8000
    """
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)/(\d+)/(\d+)\s+(\S+)", line)
        if m and m[4].endswith(f".{port}"):
            return int(m[1]), int(m[3])
    return None, None


def accept_queue(port):
    """① (今の長さ, 上限)。取れなければ (None, None)"""
    if sys.platform == "darwin":
        out = subprocess.run(["netstat", "-Lan"], capture_output=True, text=True).stdout
        return parse_netstat_listen(out, port)
    if shutil.which("ss"):
        # LISTEN ソケットでは Recv-Q が今の長さ、Send-Q が上限
        out = subprocess.run(["ss", "-ltnH", f"sport = :{port}"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            fields = line.split()
            return int(fields[1]), int(fields[2])
        return None, None
    # ss がない Linux: LISTEN(0A) 行の rx_queue が今の長さ。上限はここには出ない
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(path).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if fields[3] == "0A" and fields[1].endswith(f":{port:04X}"):
                return int(fields[4].split(":")[1], 16), None
    return None, None


def get_stats(port, timeout):
    """(/stats の中身, 応答時間 ms)。timeout 秒以内に返らなければ (None, None)"""
    start = time.perf_counter()
    try:
        with _opener.open(f"http://127.0.0.1:{port}/stats", timeout=timeout) as res:
            stats = json.load(res)
    except (OSError, http.client.HTTPException, ValueError):
        return None, None
    return stats, (time.perf_counter() - start) * 1000


class PgCounter:
    """DB 側を数える。つながらなければ (None, None) を返し、次の回につなぎ直す"""

    def __init__(self):
        self.dsn = DSN.replace("+psycopg", "")  # SQLAlchemy の URL から libpq の形に
        self.conn = None

    def sample(self):
        try:
            if self.conn is None or self.conn.closed:
                self.conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=2)
            return self.conn.execute(PG_SQL).fetchone()
        except psycopg.Error:
            self.conn = None
            return None, None


class Probe:
    def __init__(self, port=8000, timeout=1.0):
        self.port = port
        self.timeout = timeout
        self.pg = PgCounter()

    def sample(self):
        # /stats は /blocking 中に timeout まで待たされるので最後に測る
        row = {"timestamp": round(time.time(), 3)}
        row["qlen"], row["qmax"] = accept_queue(self.port)
        row["pg_active"], row["pg_conns"] = self.pg.sample()
        stats, ms = get_stats(self.port, self.timeout)
        if stats is not None:
            for key in ("threads", "tasks", "sync_checked_out", "async_checked_out"):
                row[key] = stats[key]
            row["stats_ms"] = round(ms, 1)
        return row


def ticks(interval):
    """interval 秒ごとに回す。測定が遅れたら詰めて取り返さず、そこから数え直す"""
    next_t = time.monotonic()
    while True:
        yield
        next_t += interval
        delay = next_t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.monotonic()


def format_row(row):
    def v(key, width):
        x = row.get(key)
        return f"{'-' if x is None else x:>{width}}"

    stats = "timeout" if row.get("stats_ms") is None else f"{row['stats_ms']:.0f}ms"
    return (
        time.strftime("%H:%M:%S", time.localtime(row["timestamp"]))
        + f"  ①accept {v('qlen', 4)}/{v('qmax', 4)}"
        + f"  ②threads {v('threads', 3)} tasks {v('tasks', 4)}"
        + f"  ③sync {v('sync_checked_out', 2)} async {v('async_checked_out', 2)}"
        + f"  DB pg_sleep {v('pg_active', 3)} conns {v('pg_conns', 3)}"
        + f"  /stats {stats:>7}"
    )


class CsvLog:
    """1行ずつ flush する。途中で止めてもそこまでのデータは残る"""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "w", newline="")
        self.writer = csv.DictWriter(self.file, FIELDS)
        self.writer.writeheader()

    def write(self, row):
        self.writer.writerow(row)
        self.file.flush()

    def close(self):
        self.file.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", nargs="?", help="書き出す CSV のパス（省略すると画面に出すだけ）")
    parser.add_argument("--interval", type=float, default=0.5, help="測定間隔（秒）")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    probe = Probe(args.port)
    log = CsvLog(args.csv) if args.csv else None
    try:
        for _ in ticks(args.interval):
            row = probe.sample()
            print(format_row(row), flush=True)
            if log:
                log.write(row)
    except KeyboardInterrupt:
        pass
    finally:
        if log:
            log.close()


if __name__ == "__main__":
    main()
