import asyncio
import logging
import socket
import threading
import time

from backend.api.router import create_app
from backend.config import API_HOST, API_PORT
from backend.db.database import init_db
from backend.telegram.client import has_credentials, init_client
from backend.telegram.sync import sync_dialogs, bulk_sync, register_event_handlers, sync_global_mentions, scan_unscanned_messages
from backend.llm.registry import get_registry
from backend.llm.config import load_registry_config
from backend.briefing_scheduler import start_briefing_scheduler
from backend.catchup_summary_scheduler import start_catchup_summary_scheduler
from backend.scam.client import ExternalScamAPIClient, LocalPipelineClient
from backend.scam.pipeline import init_pipeline_manager
from backend.scam.service import init_scam_service, get_scam_service
from backend.tasks import spawn

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

    # Initialize tool registry, pipeline, and cloud sync
    import backend.scam.tools  # noqa: F401 — triggers auto-registration of tools
    pipeline_mgr = init_pipeline_manager()

    from backend.scam.cloud_sync import init_cloud_sync_manager
    cloud_sync_mgr = init_cloud_sync_manager()

    # Background cloud sync: on launch + daily
    async def _cloud_sync_loop():
        # Initial sync on launch (non-blocking, small delay to let app settle)
        await asyncio.sleep(5)
        if cloud_sync_mgr.is_configured:
            await cloud_sync_mgr.sync()
        # Daily sync
        while True:
            await asyncio.sleep(86400)
            if cloud_sync_mgr.is_configured:
                await cloud_sync_mgr.sync()

    spawn(_cloud_sync_loop(), name="cloud_sync_loop")

    from backend.api.settings import get_scam_detection_settings
    scam_settings = get_scam_detection_settings()
    if scam_settings["mode"] == "external" and scam_settings["api_url"]:
        scam_client = ExternalScamAPIClient(
            base_url=scam_settings["api_url"],
            api_key=scam_settings["api_key"],
        )
    else:
        max_tokens = scam_settings.get("max_tokens", 200)
        scam_client = LocalPipelineClient(pipeline_mgr.get_config(), max_tokens=max_tokens)
    scam_svc = init_scam_service(scam_client)
    spawn(scam_svc.resume_pending_requests(), name="resume_pending_scam_requests")

    if not has_credentials():
        return
    authenticated = await init_client()
    if authenticated:
        try:
            await sync_dialogs()
        except Exception:
            # Never let a rate limit or network blip at launch kill startup —
            # the periodic dialog sync below covers the gap.
            logging.getLogger(__name__).warning("Startup dialog sync failed", exc_info=True)
        await register_event_handlers()
        # Catch up on updates missed while disconnected (must be after handlers are registered)
        from backend.telegram.client import get_client
        tg_client = get_client()
        if tg_client:
            try:
                await tg_client.catch_up()
            except Exception:
                pass  # Not fatal — periodic sync will cover gaps
        async def _sync_then_scan():
            await bulk_sync(n_chats=50, messages_per_chat=30)
            await scan_unscanned_messages()

        spawn(_sync_then_scan(), name="startup_sync_then_scan")
        spawn(sync_global_mentions(days=30), name="startup_mention_sync")

        # Periodic dialog re-sync to catch missed messages/chats + contact requests
        async def _periodic_dialog_sync():
            while True:
                await asyncio.sleep(120)  # every 2 minutes
                try:
                    await sync_dialogs(limit=100, include_archived=True)
                except Exception:
                    pass
        spawn(_periodic_dialog_sync(), name="periodic_dialog_sync")


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

    # Expose Python functions to JavaScript via window.pywebview.api
    import webbrowser

    class JsApi:
        def open_url(self, url):
            """Open an external URL in the system's default browser."""
            if url and (url.startswith("http://") or url.startswith("https://")):
                webbrowser.open(url)

        def save_file_dialog(self, temp_path, suggested_name):
            """Open a native save dialog, copy temp file to user-chosen location."""
            import shutil
            try:
                result = window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=suggested_name,
                )
                if result:
                    dest = result if isinstance(result, str) else result[0]
                    shutil.copy2(temp_path, dest)
                    return dest
            except Exception as e:
                print(f"Save dialog error: {e}")
            return None

    window = webview.create_window(
        "Common Defense",
        f"http://{API_HOST}:{API_PORT}",
        width=1280,
        height=800,
        min_size=(900, 600),
        js_api=JsApi(),
    )

    # Register the window so notification clicks can navigate to chats
    from backend.telegram.sync import set_webview_window
    set_webview_window(window)

    webview.start()
