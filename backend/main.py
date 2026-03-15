import asyncio
import socket
import threading
import time

from backend.api.router import create_app
from backend.config import API_HOST, API_PORT
from backend.db.database import init_db
from backend.telegram.client import has_credentials, init_client
from backend.telegram.sync import sync_dialogs, bulk_sync, register_event_handlers
from backend.llm.registry import get_registry
from backend.llm.config import load_registry_config
from backend.briefing_scheduler import start_briefing_scheduler
from backend.catchup_summary_scheduler import start_catchup_summary_scheduler

app = create_app()


@app.on_event("startup")
async def on_startup():
    await init_db()

    # Restore LLM registry from persisted config
    cfg = load_registry_config()
    registry = get_registry()
    for instance_cfg in cfg.instances:
        try:
            registry.add(instance_cfg)
        except Exception:
            pass  # unknown provider etc — skip silently
    # Start the background briefing scheduler (runs regardless of Telegram auth)
    start_briefing_scheduler()
    # Start the catch-up summary scheduler
    start_catchup_summary_scheduler()

    if not has_credentials():
        return
    authenticated = await init_client()
    if authenticated:
        await sync_dialogs()
        await register_event_handlers()
        asyncio.create_task(bulk_sync(n_chats=50, messages_per_chat=30))


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

    # Wait until the server is accepting connections (up to 15s)
    for _ in range(50):
        try:
            with socket.create_connection((API_HOST, API_PORT), timeout=0.5):
                break
        except OSError:
            time.sleep(0.3)

    webview.create_window(
        "Mingly",
        f"http://{API_HOST}:{API_PORT}",
        width=1280,
        height=800,
        min_size=(900, 600),
    )
    webview.start()
