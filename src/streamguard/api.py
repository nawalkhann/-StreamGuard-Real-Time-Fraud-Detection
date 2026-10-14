from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import Settings
from .engine import Engine, SchemaError
from .ingest import run_kafka
from .model import FraudModel
from .schemas import AlertStatus, AlertUpdate, BlockEntry, Decision, Transaction
from .storage import Storage

log = logging.getLogger("streamguard")
STATIC = Path(__file__).parent / "static"


class AlertHub:
    """Fan-out of new alerts to connected WebSocket clients."""

    def __init__(self) -> None:
        self.clients: set[asyncio.Queue] = set()

    def publish(self, alert: dict) -> None:
        for q in list(self.clients):
            try:
                q.put_nowait(alert)
            except asyncio.QueueFull:
                pass  # slow consumer: drop rather than block scoring


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings()
    if not s.auth_disabled and not s.api_key_list:
        raise RuntimeError("Set STREAMGUARD_API_KEYS (or STREAMGUARD_AUTH_DISABLED=true for local development only).")
    keys = [k.encode() for k in s.api_key_list]

    def key_ok(candidate: str | None) -> bool:
        if s.auth_disabled:
            return True
        if not candidate:
            return False
        c = candidate.encode()
        return any([hmac.compare_digest(c, k) for k in keys])  # no short-circuit

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if not key_ok(x_api_key):
            raise HTTPException(401, "invalid or missing API key", headers={"WWW-Authenticate": "ApiKey"})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        if s.auth_disabled:
            log.warning("AUTH DISABLED - do not expose this instance")
        db = Storage(s.db_path)
        engine = Engine(s, db, FraudModel.load(s.model_path))
        engine.warm_start()
        hub = AlertHub()
        loop = asyncio.get_running_loop()
        engine.alert_listeners.append(lambda a: loop.call_soon_threadsafe(hub.publish, a))
        app.state.engine, app.state.hub = engine, hub
        task = asyncio.create_task(run_kafka(engine, s)) if s.kafka_bootstrap else None
        try:
            yield
        finally:
            if task:
                task.cancel()
            db.close()

    app = FastAPI(title="StreamGuard", version=__version__, lifespan=lifespan)
    auth = [Depends(require_key)]

    def eng(request: Request) -> Engine:
        return request.app.state.engine

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        if not request.url.path.startswith("/docs") and request.url.path not in ("/redoc", "/openapi.json"):
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'")
        return resp

    # ---- ops ----
    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz(request: Request) -> dict:
        e = eng(request)
        e.db.ping()
        return {"status": "ready", "model": e.model.version if e.model else None,
                "mode": "ml+rules" if e.model else "rules-only", "accounts_in_memory": len(e.store)}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(request: Request) -> str:
        e = eng(request)
        return e.metrics.render(e.model is not None, len(e.store))

    # ---- scoring ----
    @app.post("/v1/transactions/score", response_model=Decision, dependencies=auth)
    async def score(txn: Transaction, request: Request, response: Response) -> Decision:
        try:
            d = await run_in_threadpool(eng(request).score, txn)
        except SchemaError as e:
            raise HTTPException(422, str(e))
        except Exception:
            eng(request).metrics.error()
            log.exception("scoring failed for %s", txn.transaction_id)
            raise HTTPException(500, "scoring failed")
        if d.duplicate:
            response.headers["Idempotent-Replay"] = "true"
        return d

    @app.get("/v1/transactions/{transaction_id}", dependencies=auth)
    def get_transaction(transaction_id: str, request: Request) -> dict:
        r = eng(request).db.get_transaction(transaction_id)
        if r is None:
            raise HTTPException(404, "transaction not found")
        return r

    # ---- analyst workflow ----
    @app.get("/v1/alerts", dependencies=auth)
    def list_alerts(request: Request, status: AlertStatus | None = None,
                    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> list[dict]:
        return eng(request).db.list_alerts(status, limit, offset)

    @app.patch("/v1/alerts/{alert_id}", dependencies=auth)
    def update_alert(alert_id: int, body: AlertUpdate, request: Request) -> dict:
        r = eng(request).db.update_alert(alert_id, body.status, body.analyst, body.note)
        if r is None:
            raise HTTPException(404, "alert not found")
        log.info("alert %s -> %s by %s", alert_id, body.status, body.analyst)
        return r

    @app.get("/v1/stats", dependencies=auth)
    def stats(request: Request, hours: int = Query(24, ge=1, le=24 * 90)) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return eng(request).db.stats(since) | {"window_hours": hours}

    # ---- blocklist ----
    @app.get("/v1/blocklist", dependencies=auth)
    def get_blocklist(request: Request) -> list[dict]:
        return eng(request).db.blocklist()

    @app.post("/v1/blocklist", status_code=201, dependencies=auth)
    def add_block(entry: BlockEntry, request: Request) -> dict:
        e = eng(request)
        e.db.block(entry.kind, entry.value, entry.reason)
        e.reload_blocklist()
        return entry.model_dump()

    @app.delete("/v1/blocklist/{kind}/{value}", status_code=204, dependencies=auth)
    def remove_block(kind: str, value: str, request: Request) -> Response:
        e = eng(request)
        if not e.db.unblock(kind, value):
            raise HTTPException(404, "entry not found")
        e.reload_blocklist()
        return Response(status_code=204)

    # ---- live alert stream ----
    @app.websocket("/ws/alerts")
    async def ws_alerts(ws: WebSocket, api_key: str | None = None) -> None:
        if not key_ok(api_key):
            await ws.close(code=1008)
            return
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        hub: AlertHub = ws.app.state.hub
        hub.clients.add(q)
        try:
            while True:
                await ws.send_json(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.clients.discard(q)

    # ---- dashboard ----
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
