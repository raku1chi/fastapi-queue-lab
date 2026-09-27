# 実験（uvicorn・probe・hey）とグラフ描き（plot.py）を動かすための実行環境。README「Docker で回す」
# ソース（app.py など）はイメージに入れず、compose.yaml でリポジトリごとマウントする

# hey はソースからビルドする（Linux の arm64 向けのバイナリは配布されていないので、Apple Silicon でも動くように）
FROM golang:1.24 AS hey
RUN go install github.com/rakyll/hey@v0.1.5

# 依存ライブラリは uv.lock のとおりに /opt/venv へ入れる（マウントしたリポジトリの .venv は手元用なので使わない）
FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock .python-version /tmp/lab/
RUN cd /tmp/lab && uv sync --frozen --no-cache

FROM python:3.12-slim
# iproute2: ss で ① accept queue の今の長さと上限を見る / fonts-noto-cjk: グラフの日本語
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deps /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# matplotlib のフォント一覧を先に作っておく（起動のたびに作り直さないように）
RUN python -c "import matplotlib.font_manager"
COPY --from=hey /go/bin/hey /usr/local/bin/hey
WORKDIR /lab
