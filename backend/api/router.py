import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.features import features_payload, require_shield
from backend import instance
from backend import secrets as vault
from backend.config import DATA_DIR
from backend.ws.manager import ws_manager
from backend.api import (
    accounts, admin, ai_conversations, archive, auth, catchup, compose, conversations, dashboard, intelligence, llm,
    mcp, media, organizations, people, person_notes, scam, search, session, settings, tags, telegram, translate,
    workspaces,
)
from backend.api.dependencies import HostOriginMiddleware, is_valid_session_token, require_session_token, validate_ws_origin

logger = logging.getLogger(__name__)


# ── Error format: every error body is {"detail": "<string>", ...extras} ─────

def _loc_label(loc) -> str:
    parts = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
    return ".".join(parts) or ".".join(str(p) for p in loc)


def _validation_message(errors) -> str:
    msgs = []
    for err in errors:
        label = _loc_label(err.get("loc", ()))
        msg = err.get("msg", "Invalid value")
        msgs.append(f"{label}: {msg}" if label else msg)
    return "; ".join(msgs) or "Invalid request"


async def _validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    return JSONResponse(
        status_code=422,
        content={"detail": _validation_message(errors), "errors": jsonable_encoder(errors)},
    )


async def _http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    content: dict
    if isinstance(detail, str):
        content = {"detail": detail}
    elif isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail")
        content = {**detail, "detail": message if isinstance(message, str) else json.dumps(detail, default=str)}
    else:
        content = {"detail": json.dumps(detail, default=str) if detail is not None else "Error"}
    return JSONResponse(status_code=exc.status_code, content=content, headers=getattr(exc, "headers", None))


async def _vault_corrupt_handler(_request: Request, exc: vault.VaultCorruptError) -> JSONResponse:
    logger.error("Vault is corrupt: %s", exc)
    return JSONResponse(
        status_code=409,
        content={
            "detail": "The vault file is damaged and cannot be opened. Reset the vault to start over.",
            "code": "vault_corrupt",
        },
    )


async def _unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Runs every registered startup handler (including ones main.py adds
    after create_app), then reports ready; on shutdown closes sockets, runs
    shutdown handlers and disposes the DB engine."""
    from backend.db.database import dispose_db

    await app.router.startup()
    instance.mark_ready()
    try:
        yield
    finally:
        instance.mark_not_ready()
        await ws_manager.close_all(code=1001, reason="Server shutting down")
        try:
            await app.router.shutdown()
        finally:
            await dispose_db()


def create_app() -> FastAPI:
    app = FastAPI(title="Mingly API")
    app.router.lifespan_context = _lifespan

    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(vault.VaultCorruptError, _vault_corrupt_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)

    # Vault-held API keys reach the services configured while it was locked.
    vault.add_unlock_listener(settings.on_vault_unlocked)

    # Unauthenticated bootstrap: session check, health/readiness, focus.
    app.include_router(session.router, prefix="/api/session", tags=["session"])
    app.include_router(session.health_router, prefix="/api", tags=["health"])

    # Auth routes do NOT require a session token (they issue one)
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])

    # All other API routes require a valid session token
    protected = [Depends(require_session_token)]
    app.include_router(accounts.router, prefix="/api/accounts", tags=["accounts"], dependencies=protected)
    app.include_router(conversations.router, prefix="/api/conversations", tags=["conversations"], dependencies=protected)
    app.include_router(conversations.messages_router, prefix="/api/messages", tags=["messages"], dependencies=protected)
    app.include_router(telegram.router, prefix="/api/telegram", tags=["telegram"], dependencies=protected)
    app.include_router(admin.router, prefix="/api/admin", tags=["admin"], dependencies=protected)
    app.include_router(archive.router, prefix="/api/archive", tags=["archive"], dependencies=protected)
    app.include_router(compose.router, prefix="/api/compose", tags=["compose"], dependencies=protected)
    app.include_router(people.router, prefix="/api/people", tags=["people"], dependencies=protected)
    app.include_router(person_notes.router, prefix="/api/people", tags=["people"], dependencies=protected)
    app.include_router(ai_conversations.router, prefix="/api/ai/conversations", tags=["ai"], dependencies=protected)
    app.include_router(organizations.router, prefix="/api/organizations", tags=["organizations"], dependencies=protected)
    app.include_router(settings.router, prefix="/api/settings", tags=["settings"], dependencies=protected)
    app.include_router(llm.router, prefix="/api/llm", tags=["llm"], dependencies=protected)
    app.include_router(tags.router, prefix="/api", tags=["tags"], dependencies=protected)
    app.include_router(intelligence.router, prefix="/api", tags=["intelligence"], dependencies=protected)
    app.include_router(catchup.router, prefix="/api/catchup", tags=["catchup"], dependencies=protected)
    app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"], dependencies=protected)
    app.include_router(search.router, prefix="/api/search", tags=["search"], dependencies=protected)
    app.include_router(media.router, prefix="/api/media-download", tags=["media"], dependencies=protected)
    app.include_router(scam.router, prefix="/api/scam", tags=["scam"], dependencies=protected + [Depends(require_shield)])
    app.include_router(translate.router, prefix="/api/translate", tags=["translate"], dependencies=protected)
    app.include_router(workspaces.router, prefix="/api/workspaces", tags=["workspaces"], dependencies=protected)

    # MCP endpoint has its own auth (vault-stored bearer key), NOT the session
    # token — external MCP clients can't obtain the ephemeral session token.
    app.include_router(mcp.router, tags=["mcp"])

    @app.get("/api/features")
    async def get_features(_token: str = Depends(require_session_token)):
        """Feature switches the UI reads to decide what to show."""
        return features_payload()

    @app.post("/api/open-url")
    async def open_external_url(
        body: dict,
        _token: str = Depends(require_session_token),
    ):
        """Open a URL in the system's default browser."""
        import webbrowser
        url = body.get("url", "")
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(status_code=400, detail="Invalid URL")
        webbrowser.open(url)
        return {"status": "ok"}

    @app.get("/api/media/{file_path:path}")
    async def serve_media(
        file_path: str,
        token: Optional[str] = Query(None),
        x_session_token: Optional[str] = Header(None),
    ):
        """Serve media files with auth via header OR query parameter.

        Browser <img> tags cannot send custom headers, so this endpoint
        also accepts the session token as a ``?token=`` query parameter.
        """
        if not is_valid_session_token(x_session_token or token, touch=True):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session token")
        full_path = (DATA_DIR / file_path).resolve()
        # Containment check: reject any path that escapes DATA_DIR
        if not full_path.is_relative_to(DATA_DIR.resolve()):
            raise HTTPException(status_code=403)
        if not full_path.exists() or not full_path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(full_path))

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket, token: str = Query(default="")):
        # Validate Origin header to prevent cross-origin WebSocket hijacking
        # (also enforced by HostOriginMiddleware).
        if not validate_ws_origin(websocket):
            await websocket.close(code=4003, reason="Origin not allowed")
            return

        # Validate session token (passed as query param since WS can't use headers).
        # The manager remembers it so revoking/expiring the token closes the socket.
        if not is_valid_session_token(token):
            await websocket.close(code=4001, reason="Invalid or missing session token")
            return

        await ws_manager.connect(websocket, token)
        try:
            while True:
                await websocket.receive_text()  # keep-alive
        except Exception:
            pass  # client went away, or the server closed the socket
        finally:
            ws_manager.disconnect(websocket)

    # Unknown /api/* paths get a JSON 404 instead of falling through to the SPA.
    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def api_not_found(path: str, request: Request):
        # This route matches every method, so restore 405 for known paths.
        for route in app.router.routes:
            if isinstance(route, APIRoute) and route.endpoint is not api_not_found and route.path.startswith("/api/") \
                    and route.path_regex.match(request.url.path):
                raise HTTPException(status_code=405, detail="Method not allowed")
        raise HTTPException(status_code=404, detail=f"Not found: /api/{path}")

    # Serve the built React frontend in production
    if getattr(sys, "frozen", False):
        static_dir = Path(sys._MEIPASS) / "frontend" / "dist"  # type: ignore[attr-defined]
    else:
        static_dir = Path(__file__).parent.parent.parent / "frontend" / "dist"
    if static_dir.exists():
        assets_dir = static_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/{full_path:path}")
        async def serve_frontend(full_path: str):
            return FileResponse(str(static_dir / "index.html"))
    else:
        # Development: allow Vite dev server on localhost:5173
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Outermost: reject non-loopback Host (DNS rebinding) and foreign Origins.
    app.add_middleware(HostOriginMiddleware)

    return app
