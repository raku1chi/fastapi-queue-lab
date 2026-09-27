"""
FastAPI 詰まり実験

クライアント → LB → [① accept queue] → [② イベントループ／スレッドプール] → [③ DBコネクションプール] → DB

4本のエンドポイントで、詰まる場所を1つずつ切り替えて見る。
"""
import asyncio
import threading
import time

from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

DSN = "postgresql+psycopg://exp:exp@127.0.0.1:5432/exp"

# SQLAlchemyのデフォルト値をそのまま明示。同時15接続、空きを30秒待って諦める
POOL = dict(pool_size=5, max_overflow=10, pool_timeout=30)

engine = create_engine(DSN, **POOL)
aengine = create_async_engine(DSN, **POOL)

app = FastAPI()


@app.get("/baseline")
async def baseline():
    """基準線: ループを止めず、DBも触らない。1000並列でも全件5秒で返る"""
    await asyncio.sleep(5)
    return {"ok": True}


@app.get("/blocking")
async def blocking():
    """①を見る: async def の中で time.sleep → イベントループが止まり accept() が呼ばれない"""
    time.sleep(5)
    return {"ok": True}


@app.get("/sync")
def sync_db():
    """②③を見る: def なので anyio のスレッドプール(上限40)で実行 → その先で③プール(15)待ち"""
    with engine.connect() as conn:
        conn.execute(text("select pg_sleep(5)"))
    return {"ok": True}


@app.get("/async")
async def async_db():
    """③を純粋に見る: 1000本のコルーチンが一斉に③プール(15)へ殺到 → 30秒で TimeoutError"""
    async with aengine.connect() as conn:
        await conn.execute(text("select pg_sleep(5)"))
    return {"ok": True}


@app.get("/stats")
async def stats():
    """負荷中に別ターミナルから叩く。/blocking 中は応答しない(それ自体が①②の証拠)"""
    return {
        "threads": threading.active_count(),  # ②スレッドプール: /sync 中に ~40+ になる
        "tasks": len(asyncio.all_tasks()),    # ②ループ上の滞留: 受け付けたが終わっていないリクエスト数
        "sync_pool": engine.pool.status(),    # ③ Checked out が 15 で張り付く
        "async_pool": aengine.pool.status(),
        # probe.py が CSV に書く数値（status() の文字列と同じ値）
        "sync_checked_out": engine.pool.checkedout(),
        "async_checked_out": aengine.pool.checkedout(),
    }
