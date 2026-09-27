# 実験（uvicorn・probe・hey）を 1 つのコンテナで動かすための実行環境。README「Docker で回す」
# ソース（app.py など）はイメージに入れず、compose.yaml でリポジトリごとマウントする

# hey はソースからビルドする（Linux の arm64 向けのバイナリは配布されていないので、Apple Silicon でも動くように）
FROM golang:1.24 AS hey
RUN go install github.com/rakyll/hey@v0.1.5

FROM python:3.12-slim
# ss: ① accept queue の今の長さと上限を見る
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY --from=hey /go/bin/hey /usr/local/bin/hey
WORKDIR /lab
