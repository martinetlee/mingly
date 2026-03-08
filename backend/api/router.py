from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import DATA_DIR
from backend.ws.manager import ws_manager
from backend.api import auth, chats, compose, todos, contacts, organizations, scan, settings


def create_app() -> FastAPI:
    app = FastAPI(title="Mingly API")

    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(chats.router, prefix="/api/chats", tags=["chats"])
    app.include_router(compose.router, prefix="/api/compose", tags=["compose"])
    app.include_router(todos.router, prefix="/api/todos", tags=["todos"])
    app.include_router(contacts.router, prefix="/api/contacts", tags=["contacts"])
    app.include_router(organizations.router, prefix="/api/organizations", tags=["organizations"])
    app.include_router(scan.router, prefix="/api/scan", tags=["scan"])
    app.include_router(settings.router, prefix="/api/settings", tags=["settings"])

    @app.get("/api/media/{file_path:path}")
    async def serve_media(file_path: str):
        full_path = DATA_DIR / file_path
        if not full_path.exists() or not full_path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(full_path))

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()  # keep-alive
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

    # Serve the built React frontend in production
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
