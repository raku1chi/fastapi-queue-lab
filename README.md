# fastapi-queue-lab

> 記事: 「FastAPIに1000並列を叩いて、どこが詰まるかを見た」（公開したらここにリンクを貼る）

FastAPI（Uvicorn）のサーバー1台に 1000 並列でリクエストを投げ、どこで待ちが生じ、どう壊れるかを手元で再現する実験。

```
クライアント → LB → [① accept queue] → [② イベントループ／スレッドプール] → [③ DBコネクションプール] → DB
```

- **① accept queue**: カーネルが TCP ハンドシェイクを終えた接続を、アプリが `accept()` するまで並べておく場所
- **② イベントループ／スレッドプール**: Uvicorn が受け取ったリクエストが処理を待つ場所
- **③ コネクションプール**: SQLAlchemy のプール（デフォルトの 5 + オーバーフロー 10 = 15）に空きがなく、接続を借りるのを待つ場所

DB の遅さは `pg_sleep(5)` で作り、`hey` で 1000 並列を叩く。エンドポイントを 4 本用意して、詰まる場所を1つずつ切り替える。

| | エンドポイント | 中身 | 見たいもの |
|---|---|---|---|
| A | `/baseline` | `async def` + `await asyncio.sleep(5)`。DB に触らない | 詰まらない基準線 |
| B | `/blocking` | `async def` の中で `time.sleep(5)` | ① |
| C | `/sync` | `def` + 同期 SQLAlchemy で `pg_sleep(5)` | ② と ③ が同時に詰まる |
| D | `/async` | `async def` + 非同期 SQLAlchemy で `pg_sleep(5)` | ③ だけが詰まる |
| | `/stats` | スレッド数、ループ上のタスク数、プールの状態を返す | 負荷中の観測用 |

## ファイル

| ファイル | 役割 |
|---|---|
| `app.py` | 実験対象のサーバー（上の 5 本） |
| `probe.py` | 観測。①②③ と DB 側を 0.5 秒ごとに測って画面に出し、CSV にも書く |
| `live.html` | `--live` を付けたときにブラウザで開く、リアルタイムのグラフ |
| `bench.py` | 1 本分のデータ取り。uvicorn を起動し、probe で観測しながら hey で負荷をかけ、`results/<name>/` に保存する |
| `plot.py` | `results/` からグラフ（`figures/`）と、予想と実測の対照表を作る |
| `compose.yaml` | Postgres 16 |
| `results/` | 生データ |

## 準備

Python 3.11 以上、Docker、[hey](https://github.com/rakyll/hey) を使う。

```bash
brew install hey          # ab は -c 1000 で接続エラーを出しやすいので hey を使う
docker compose up -d
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**macOS 固有の前提**（ここで詰まると実験が始まらない）

- ファイルディスクリプタ: `ulimit -n` のデフォルトは 256 で、1000 並列は開けない。uvicorn と hey を手で動かすときは、**両方**のシェルで `ulimit -n 10240`（`bench.py` は自分で上げる）
- accept queue の上限: `sysctl kern.ipc.somaxconn` は 128。uvicorn の `--backlog 2048`（デフォルト）もここで頭打ちになる

**Linux の場合**

- hey は `go install github.com/rakyll/hey@latest` などで入れる
- somaxconn は 4096 前後で、uvicorn のデフォルトの backlog 2048 がそのまま効く。1000 本なら全部 ① に収まってあふれないので、B は `python bench.py blocking --name blocking-backlog128 -- --backlog 128` のように backlog を 128 にすると macOS と同じ条件になる
- ① は `ss -ltn 'sport = :8000'` で見る（LISTEN ソケットの Recv-Q が今の長さ、Send-Q が上限）。`probe.py` は `ss` がなければ `/proc/net/tcp` から今の長さだけを読む

**その他**

- ローカルで Postgres がすでに 5432 番を使っているなら、`compose.yaml` のポートを `5433:5432` にして `app.py` の `DSN` も合わせる（`probe.py` と `bench.py` も `app.py` の `DSN` を使う）

## 観察する（ターミナル3枚）

まずは目で見る。

```bash
# T1: サーバー
ulimit -n 10240
uvicorn app:app --port 8000 --no-access-log

# T2: 観測（①②③ と DB 側を 0.5 秒ごとに表示し続ける）
python probe.py

# T3: 負荷。サマリ（エラーの内訳が出る）は残しておく
ulimit -n 10240
mkdir -p results/sync
hey -n 1000 -c 1000 -t 60 http://127.0.0.1:8000/sync | tee results/sync/hey.txt
```

`-t 60` はクライアント側のタイムアウト。デフォルトの 20 秒だと、③ の 30 秒タイムアウトを見る前にクライアントが諦めてしまう。

A → B → C → D の順に、1 本ごとに T1 の uvicorn を起動し直して回す。

- **B の後は Ctrl+C では止まらない。** シグナルは届くが、それを処理するイベントループが `time.sleep` で止まっているため。`Ctrl+\`（SIGQUIT）で強制終了する。放っておくと、受け付けたリクエストを 5 秒に 1 件ずつ何十分も処理し続ける
- **C は hey が終わった後も T1/T2 を見続ける。** クライアントはもういないのに、サーバーは数分間処理を続ける

### グラフをリアルタイムに見る

T2 を `python probe.py --live` にすると、ブラウザが開き（http://127.0.0.1:8001/）、①②③ と DB 側のグラフが 0.5 秒ごとに伸びていく。

- 網かけは `/stats` が返らなかった区間。B の間ずっと広がっていく
- グラフにポインタを置くと、その時点の値が全パネルの右上に出る
- `bench.py` にも `--live` を付けられる。そのときは hey の開始と終了が縦線で入る
- probe を起動し直すと、開いたままのページも最初から描き直す
- グラフの描画には uPlot を CDN（jsDelivr）から読み込むので、ネットワークにつながっている必要がある

`probe.py` がやっていることを手で見るなら:

```bash
curl -s localhost:8000/stats          # ②③
netstat -Lan | grep '\.8000'          # ① macOS: qlen/incqlen/maxqlen
ss -ltn 'sport = :8000'               # ① Linux: Recv-Q / Send-Q
docker compose exec -T db psql -U exp -tAc \
  "select count(*) from pg_stat_activity where state='active' and query ilike '%pg_sleep%' and pid<>pg_backend_pid()"
```

## データを取る

1 本ずつ `bench.py` で回す。uvicorn の起動と停止も `bench.py` がやるので、T1 の uvicorn は止めておく。

```bash
python bench.py baseline   # 15 秒ほど
python bench.py blocking   # 1 分半ほど
python bench.py sync       # hey は 60 秒で終わるが、サーバーの後処理を見届けるので 6 分ほど
python bench.py async      # 1 分ほど
```

`--live` を付けると、データを取りながらブラウザでグラフを見られる（[グラフをリアルタイムに見る](#グラフをリアルタイムに見る)）。

1 本の流れ:

1. uvicorn を起動する（出力は `server.log` へ）
2. probe で 3 秒観測してから、`hey -n 1000 -c 1000 -t 60 -o csv` で負荷をかける
3. hey が終わっても、サーバーがアイドルに戻るまで観測を続ける。`/stats` が 10 秒返らなければ打ち切る（B はこうなる）
4. uvicorn を止める。B は SIGTERM では止まらないので、5 秒待って SIGKILL

`results/<name>/` にできるもの:

| ファイル | 中身 |
|---|---|
| `hey.csv` | `hey -o csv` の出力。応答が返ったリクエストだけが並ぶ。タイムアウトや接続エラーの行はないので、リクエスト数との差が「応答なし」 |
| `probe.csv` | 観測値の時系列（列は下の表） |
| `server.log` | uvicorn の出力。D の `TimeoutError` のトレースバックもここに残る |
| `meta.json` | 実行条件（コマンド、プール設定、OS、ライブラリのバージョン、somaxconn）と時刻。`hey_start` がグラフの時間軸の 0 秒 |

| `probe.csv` の列 | 中身 |
|---|---|
| `timestamp` | UNIX 時刻（秒） |
| `qlen` / `qmax` | ① accept queue の今の長さ / 上限（`ss` のない Linux では上限は空欄） |
| `threads` / `tasks` | ② スレッド数 / ループ上のタスク数（`/stats`） |
| `sync_checked_out` / `async_checked_out` | ③ プールから貸し出し中の接続（`/stats`） |
| `stats_ms` | `/stats` の応答時間。空欄は 1 秒以内に返らなかった |
| `pg_active` | DB 側で `pg_sleep` を実行中の接続数 |
| `pg_conns` | DB 側のクライアント接続の総数（probe 自身の接続は除く） |

hey のサマリ（エラーの内訳）は `-o csv` と同時に出せないので、[観察](#観察するターミナル3枚)のときに `hey.txt` として残しておく。

## グラフと対照表

```bash
python plot.py
```

`figures/` に次のグラフを書き出し、予想と実測の対照表を Markdown で画面に出す。

- `status.png`: A〜D の結果の内訳（200 / 500 / 応答なし）
- `responses.png`: クライアントが応答を受け取ったタイミング（累積）
- `timeline-<name>.png`: run ごとの時系列。クライアントが受け取った応答と、①②③ の観測値を同じ時間軸で並べる

## 発展

`--name` で保存先を分け、`--` の後ろに書いた引数は uvicorn に渡す。

```bash
python bench.py sync --name sync-workers4 -- --workers 4           # プロセスごとにプールもスレッドプールも独立する
python bench.py blocking --name blocking-backlog16 -- --backlog 16 # ① をさらに小さくする
python bench.py sync -n 100 -c 100 --name sync-c100               # 並列数を落として、詰まらなくなる閾値を探す
```

- **プールを広げる**: `app.py` の `POOL` を `pool_size=100, max_overflow=200` にして `python bench.py async --name async-pool300`。③ をゆるめたとき、次に詰まるのが DB そのもの（Postgres の `max_connections=100`）かを見る
- **1 行直す**: B の `time.sleep(5)` を `await asyncio.sleep(5)` に変えて、A と同じ挙動になるかを見る

## 測り方の注意

- `--workers` を付けると、`/stats` はどれか 1 プロセスの値しか返さない。DB 側の `pg_active` / `pg_conns` は全プロセスの合計
- probe の `/stats` リクエストも accept queue を通る。backlog に余裕がある Linux で B を回すと、返ってこない probe の接続が毎秒 1 本ずつ ① に積まれる
- `tasks` には uvicorn 自身のタスク（アイドル時で 3 本）も入る
- Python 3.11 では `asyncio.wait_for` が待つたびに内部でタスクを 1 本作るので、D の `tasks` はリクエスト数の約 2 倍になる（3.12 以降は作らない）。Python のバージョンは `meta.json` に残る
