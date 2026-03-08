import asyncio
import threading
import time

from backend.api.router import create_app
from backend.config import API_HOST, API_PORT
from backend.db.database import init_db
from backend.telegram.client import init_client
from backend.telegram.sync import sync_dialogs, register_event_handlers

app = create_app()


@app.on_event("startup")
async def on_startup():
    await init_db()
    authenticated = await init_client()
    if authenticated:
        await sync_dialogs()
        await register_event_handlers()


if __name__ == "__main__":
    import uvicorn
    import webview

    def run_server():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        config = uvicorn.Config(app, host=API_HOST, port=API_PORT, log_level="info")
        server = uvicorn.Server(config)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    time.sleep(1.5)  # Give uvicorn time to start

    webview.create_window(
        "Mingly",
        f"http://{API_HOST}:{API_PORT}",
        width=1280,
        height=800,
        min_size=(900, 600),
    )
    webview.start()
