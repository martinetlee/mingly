import sys
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import DATA_DIR
from backend.ws.manager import ws_manager
from backend.api import admin, auth, catchup, chats, compose, contacts, conversations, dashboard, media, organizations, scam, search, settings, llm, tags, intelligence, profiles
from backend.api.dependencies import require_session_token, validate_ws_origin, get_session_token


def create_app() -> FastAPI:
    app = FastAPI(title="Mingly API")

    # Auth routes do NOT require a session token (they issue one)
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])

    # All other API routes require a valid session token
    protected = [Depends(require_session_token)]
    app.include_router(admin.router, prefix="/api/admin", tags=["admin"], dependencies=protected)
    app.include_router(chats.router, prefix="/api/chats", tags=["chats"], dependencies=protected)
    app.include_router(compose.router, prefix="/api/compose", tags=["compose"], dependencies=protected)
    app.include_router(contacts.router, prefix="/api/contacts", tags=["contacts"], dependencies=protected)
    app.include_router(conversations.router, prefix="/api/conversations", tags=["conversations"], dependencies=protected)
    app.include_router(organizations.router, prefix="/api/organizations", tags=["organizations"], dependencies=protected)
    app.include_router(settings.router, prefix="/api/settings", tags=["settings"], dependencies=protected)
    app.include_router(llm.router, prefix="/api/llm", tags=["llm"], dependencies=protected)
    app.include_router(tags.router, prefix="/api", tags=["tags"], dependencies=protected)
    app.include_router(intelligence.router, prefix="/api", tags=["intelligence"], dependencies=protected)
    app.include_router(profiles.router, prefix="/api/profiles", tags=["profiles"], dependencies=protected)
    app.include_router(catchup.router, prefix="/api/catchup", tags=["catchup"], dependencies=protected)
    app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"], dependencies=protected)
    app.include_router(search.router, prefix="/api/search", tags=["search"], dependencies=protected)
    app.include_router(media.router, prefix="/api/media-download", tags=["media"], dependencies=protected)
    app.include_router(scam.router, prefix="/api/scam", tags=["scam"], dependencies=protected)

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
        session_token = get_session_token()
        provided = x_session_token or token
        if session_token is None or provided != session_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
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
        if not validate_ws_origin(websocket):
            await websocket.close(code=4003, reason="Origin not allowed")
            return

        # Validate session token (passed as query param since WS can't use headers)
        current_token = get_session_token()
        if current_token is None or token != current_token:
            await websocket.close(code=4001, reason="Invalid or missing session token")
            return

        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()  # keep-alive
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

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

    return app
