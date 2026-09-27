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
| `bench.py` | データ取り。uvicorn を起動し、probe で観測しながら hey で負荷をかけ、`results/<name>/<何回目>/` に保存する |
| `plot.py` | `results/` からグラフ（`figures/`）と、予想と実測の対照表を作る |
| `pyproject.toml` / `uv.lock` | 依存ライブラリ。`uv.lock` が間接的な依存まで含めてバージョンを固定する（手元も Docker も同じもの） |
| `stable.env` | ぶれない設定（[ぶれない設定](#ぶれない設定)） |
| `compose.yaml` | Postgres 16 と、実験を Docker の中で回す `lab` / `lab-stable`、グラフを描く `plot`（[Docker で回す](#docker-で回す)） |
| `Dockerfile` | `lab` と `plot` の実行環境（Python 3.12、`uv.lock` の依存ライブラリ、hey、ss、日本語フォント） |
| `results/` | 生データ。記事の数字の元データとしてコミットする（`server.log` は除く） |

回し方は 2 通りある。

- **手元で回す**: Mac の上で uvicorn と hey を直接動かす（DB だけ Docker）。macOS 固有の条件（`ulimit -n` 256、somaxconn 128）ごと観察できる
- **Docker で回す**: uvicorn・probe・hey を 1 つのコンテナに入れ、somaxconn やファイルディスクリプタの上限、CPU 数を compose.yaml で固定する。別のマシンでも同じ条件で再現できる

## 準備（手元で回す）

[uv](https://docs.astral.sh/uv/)、Docker、[hey](https://github.com/rakyll/hey) を使う。

```bash
brew install uv hey       # ab は -c 1000 で接続エラーを出しやすいので hey を使う
docker compose up -d
uv sync                   # .venv に Python 3.12 と uv.lock のとおりの依存ライブラリを入れる（Python がなければ uv が入れる）
```

以下のコマンドは `uv run` を付けて動かす（`.venv` を有効にしなくてよい）。

**macOS 固有の前提**（ここで詰まると実験が始まらない）

- ファイルディスクリプタ: `ulimit -n` のデフォルトは 256 で、1000 並列は開けない。uvicorn と hey を手で動かすときは、**両方**のシェルで `ulimit -n 10240`（`bench.py` は自分で上げる）
- accept queue の上限: `sysctl kern.ipc.somaxconn` は 128。uvicorn の `--backlog 2048`（デフォルト）もここで頭打ちになる

**Linux の場合**

- hey は `go install github.com/rakyll/hey@latest` などで入れる
- somaxconn は 4096 前後で、uvicorn のデフォルトの backlog 2048 がそのまま効く。1000 本なら全部 ① に収まってあふれないので、B は `uv run bench.py blocking --name blocking-backlog128 -- --backlog 128` のように backlog を 128 にすると macOS と同じ条件になる（Docker で回すなら compose.yaml が 128 に固定する）
- ① は `ss -ltn 'sport = :8000'` で見る（LISTEN ソケットの Recv-Q が今の長さ、Send-Q が上限）。`probe.py` は `ss` がなければ `/proc/net/tcp` から今の長さだけを読む

**その他**

- ローカルで Postgres がすでに 5432 番を使っているなら、`compose.yaml` のポートを `5433:5432` にして、環境変数 `DSN`（または `app.py` の既定値）も合わせる

## 観察する（ターミナル3枚）

まずは目で見る。

```bash
# T1: サーバー
ulimit -n 10240
uv run uvicorn app:app --port 8000 --no-access-log

# T2: 観測（①②③ と DB 側を 0.5 秒ごとに表示し続ける）
uv run probe.py

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

T2 を `uv run probe.py --live` にすると、ブラウザが開き（http://127.0.0.1:8001/）、①②③ と DB 側のグラフが 0.5 秒ごとに伸びていく。

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

`bench.py` で回す。uvicorn の起動と停止も `bench.py` がやるので、T1 の uvicorn は止めておく。

```bash
uv run bench.py baseline   # 15 秒ほど
uv run bench.py blocking   # 1 分半ほど
uv run bench.py sync       # hey は 60 秒で終わるが、サーバーの後処理を見届けるので 6 分ほど
uv run bench.py async      # 1 分ほど
```

`--repeat 3` のように付けると同じ条件で 3 回回し、最後に回ごとの件数と中央値・幅を出す。uvicorn は 1 回ごとに起動し直す。`--live` を付けると、データを取りながらブラウザでグラフを見られる（[グラフをリアルタイムに見る](#グラフをリアルタイムに見る)）。

1 回の流れ:

1. uvicorn を起動する（出力は `server.log` へ）
2. probe で 3 秒観測してから、`hey -n 1000 -c 1000 -t 60 -o csv` で負荷をかける
3. hey が終わっても、サーバーがアイドルに戻るまで観測を続ける。`/stats` が 10 秒返らなければ打ち切る（B はこうなる）
4. uvicorn を止める。B は SIGTERM では止まらないので、5 秒待って SIGKILL

結果は `results/<name>/<何回目>/` にできる（`<name>` は `--name`、省略時はエンドポイント名）。同じ名前で回し直すと、前回の `<何回目>/` は消して取り直す。

| ファイル | 中身 |
|---|---|
| `hey.csv` | `hey -o csv` の出力。応答が返ったリクエストだけが並ぶ。タイムアウトや接続エラーの行はないので、リクエスト数との差が「応答なし」 |
| `probe.csv` | 観測値の時系列（列は下の表） |
| `server.log` | uvicorn の出力。D の `TimeoutError` のトレースバックもここに残る。大きくなり、手元のパスも入るのでコミットしない（`.gitignore`） |
| `meta.json` | 実行条件（コマンド、プール設定、OS、コンテナの中か、ライブラリのバージョン、somaxconn）と時刻。`hey_start` がグラフの時間軸の 0 秒 |

| `probe.csv` の列 | 中身 |
|---|---|
| `timestamp` | UNIX 時刻（秒） |
| `qlen` / `qmax` | ① accept queue の今の長さ / 上限（`ss` のない Linux では上限は空欄） |
| `threads` / `tasks` | ② スレッド数 / ループ上のタスク数（`/stats`） |
| `sync_checked_out` / `async_checked_out` | ③ プールから貸し出し中の接続（`/stats`） |
| `stats_ms` | `/stats` の応答時間。空欄は 1 秒以内に返らなかった |
| `pg_active` | DB 側で `pg_sleep` を実行中の接続数 |
| `pg_conns` | DB 側のクライアント接続の総数（probe 自身の接続は除く） |

hey のサマリ（エラーの内訳）は `-o csv` と同時に出せないので、[観察](#観察するターミナル3枚)のときに `results/<name>/hey.txt` として残しておく。

## ぶれない設定

既定の設定（計画どおりの値）では、200 が返る件数が回ごとにぶれる。原因は 2 つある。

- **締め切りが 5 秒ごとの区切りにちょうど重なる。** 処理は 5 秒ごとに 1 回分（B は 1 件、C と D は 15 件）ずつ進むので、60 秒と 30 秒はちょうど 12 回目・7 回目の区切りに当たる。最後の 1 回分が締め切りに間に合うかどうかは、わずかな遅れの積み重なりでその時々に決まる
- **D ではプールが途中で縮む。** 既定のプール（常駐 5 + 追加 10）は、接続がまとめて返ってきた瞬間に、追加分の接続を閉じることがある。そのとき空きを待っているコルーチンは起こされないので、実質のプールが 15 本より少なくなる。どこまで縮むかはタイミング次第

`stable.env` はこの 2 つを取り除く。

| | 既定 | `stable.env` |
|---|---|---|
| プール | 常駐 5 + 追加 10、`pool_timeout` 30 秒 | 常駐 15 + 追加 0（縮まない）、`pool_timeout` 28 秒 |
| hey のタイムアウト | 60 秒 | 62 秒 |
| 結果の保存先 | `results/` | `results/stable/` |

締め切りを区切りからずらしたので、200 の件数は B が 12 回分、C が 12 回分（15 × 12 = 180）、D が 6 回分（15 × 6 = 90）に決まる。計画の予想（約 12・約 180・約 90）は、この 2 つのぶれがないときの値に当たる。

```bash
# 手元で回す
uv run --env-file stable.env bench.py async --repeat 3
uv run --env-file stable.env plot.py          # results/stable/ を読んで figures/stable/ に書く

# Docker で回す
docker compose run --rm lab-stable python bench.py async --repeat 3
docker compose run --rm plot --results results/stable
```

既定の設定のまま記事に載せるなら、`--repeat` で数回回して中央値と幅で書く。既定の設定と `stable.env` の結果を並べると、ぶれの原因がこの 2 つだったことを示せる。

## Docker で回す

uvicorn・probe・hey を 1 つのコンテナで動かし、条件を compose.yaml で固定する。グラフも `plot` で Docker の中で描けるので、手元には Docker だけあればよい（uv も Python も要らない）。

| 固定するもの | 値 | 手元で回すときとの違い |
|---|---|---|
| Python とライブラリ | Python 3.12、`uv.lock` のバージョン | 同じ（手元も uv で 3.12 と `uv.lock` を使う） |
| ① の上限（somaxconn） | 128 | macOS と同じ。Linux の既定（4096）には左右されない |
| ファイルディスクリプタの上限 | 10240 | `ulimit -n` を打たなくてよい |
| CPU | 2 つ分 | 手元はマシン全体 |

```bash
docker compose build lab                               # 初回と、pyproject.toml / uv.lock を変えたとき（lab・lab-stable・plot で共通のイメージ）
docker compose run --rm lab python bench.py sync       # results/sync/ に出る
docker compose run --rm --service-ports lab python bench.py sync --live   # ライブ表示つき。http://127.0.0.1:8001/ を開く
docker compose run --rm lab-stable python bench.py async --repeat 3       # ぶれない設定。results/stable/ に出る
docker compose run --rm plot                           # グラフと対照表。figures/ に出る
```

- **負荷はコンテナの中からかける。** uvicorn だけをコンテナに入れてポートを公開し、ホストの hey から叩くと、Docker のポート転送のプロセスが先に接続を受け取ってしまう。hey からはすべての接続がすぐにつながったように見え、① の詰まりはコンテナの中に隠れる。`lab` は uvicorn・probe・hey を同じコンテナで動かすので、① を uvicorn のソケットで観測できる
- ソース（`app.py` など）はリポジトリごとマウントしているので、書き換えてもイメージを作り直さなくてよい
- Docker Desktop の設定で、Docker に割り当てる CPU を 4 つ以上にしておく（`lab` が 2 つ、残りを DB など）
- Mac の Docker Desktop のコンテナは、Linux の VM の中で動く。Docker で回した結果は「Linux で、上の条件に固定した」結果で、macOS 固有の条件の観察にはならない
- `lab` が公開するのはライブ表示のポート（8001）だけ。`--service-ports` を付けたときだけ公開される

## グラフと対照表

```bash
uv run plot.py                                        # results/ → figures/
uv run plot.py --results results/stable               # results/stable/ → figures/stable/
docker compose run --rm plot                          # Docker で（日本語フォント入り）
docker compose run --rm plot --results results/stable
```

日本語のフォントは、手元の Mac ではヒラギノ、Docker では Noto Sans CJK JP になる。記事の図をそろえたいなら、どちらか一方で描く。`figures/` は `results/` からいつでも作り直せるのでコミットしない（`.gitignore`）。記事に使う図はブログのリポジトリへコピーする。

次のグラフを書き出し、予想と実測の対照表を Markdown で画面に出す。`--repeat` で繰り返した条件は、200 の件数が中央値の回でグラフを描き、対照表には中央値（最小〜最大）を出す。

- `status.png`: A〜D の結果の内訳（200 / 500 / 応答なし）
- `responses.png`: クライアントが応答を受け取ったタイミング（累積）
- `timeline-<name>.png`: run ごとの時系列。クライアントが受け取った応答と、①②③ の観測値を同じ時間軸で並べる

## 発展

`--name` で保存先を分け、`--` の後ろに書いた引数は uvicorn に渡す。プールの設定は環境変数で変えられる。

```bash
uv run bench.py sync --name sync-workers4 -- --workers 4           # プロセスごとにプールもスレッドプールも独立する
uv run bench.py blocking --name blocking-backlog16 -- --backlog 16 # ① をさらに小さくする
uv run bench.py sync -n 100 -c 100 --name sync-c100                # 並列数を落として、詰まらなくなる閾値を探す
POOL_SIZE=100 MAX_OVERFLOW=200 uv run bench.py async --name async-pool300   # ③ をゆるめたとき、次に詰まるのが DB そのもの（max_connections=100）かを見る
```

- **1 行直す**: B の `time.sleep(5)` を `await asyncio.sleep(5)` に変えて、A と同じ挙動になるかを見る

Docker で回すときは `docker compose run --rm -e POOL_SIZE=100 -e MAX_OVERFLOW=200 lab python bench.py async --name async-pool300` のように `-e` で渡す。

## 環境変数

| 変数 | 既定値 | 使うところ |
|---|---|---|
| `DSN` | `postgresql+psycopg://exp:exp@127.0.0.1:5432/exp` | DB の接続先（`app.py`、`probe.py`）。Docker では compose.yaml が `db:5432` に向ける |
| `POOL_SIZE` / `MAX_OVERFLOW` / `POOL_TIMEOUT` | 5 / 10 / 30 | ③ のプール（`app.py`） |
| `HEY_TIMEOUT` | 60 | `bench.py` の `-t` の既定値 |
| `RESULTS_DIR` | `results` | `bench.py` の保存先と `plot.py` の読み込み先 |
| `LIVE_HOST` | `127.0.0.1` | ライブ表示の待ち受けアドレス。Docker では compose.yaml が `0.0.0.0` にする |

## 測り方の注意

- `--workers` を付けると、`/stats` はどれか 1 プロセスの値しか返さない。DB 側の `pg_active` / `pg_conns` は全プロセスの合計
- probe の `/stats` リクエストも accept queue を通る。backlog に余裕がある Linux で B を回すと、返ってこない probe の接続が毎秒 1 本ずつ ① に積まれる
- `tasks` には uvicorn 自身のタスク（アイドル時で 3 本）も入る
- Python 3.11 では `asyncio.wait_for` が待つたびに内部でタスクを 1 本作るので、D の `tasks` はリクエスト数の約 2 倍になる（3.12 以降は作らない）。このリポジトリは手元（uv）も Docker も 3.12 に固定している。Python のバージョンは `meta.json` に残る
- Docker で回すと DB の接続先がホスト名（`db`）になり、非同期ドライバがその名前解決をスレッドで行う。そのため D でもスレッドが数本増える
