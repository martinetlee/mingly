import asyncio
import threading

from backend.api.router import create_app
from backend.config import API_HOST, API_PORT
from backend.db.database import init_db
from backend.connectors.manager import manager
from backend.llm.registry import get_registry
from backend.llm.config import load_registry_config
from backend.briefing_scheduler import start_briefing_scheduler
from backend.catchup_summary_scheduler import start_catchup_summary_scheduler
from backend.scam.pipeline import init_pipeline_manager
from backend.scam.service import init_scam_service
from backend.tasks import spawn
from backend.features import shield_enabled

app = create_app()


def _start_shield() -> None:
    """Scam detection: tool registry (loads the ML ensemble), pipeline, cloud
    sync (owns its daily loop via vault unlock/lock listeners), the detection
    client, and resuming interrupted checks. Skipped while Shield is disabled."""
    import importlib
    importlib.import_module("backend.scam.tools")  # triggers auto-registration of tools
    init_pipeline_manager()

    from backend.scam.cloud_sync import init_cloud_sync_manager
    init_cloud_sync_manager()

    from backend.scam.settings import build_scam_client
    scam_svc = init_scam_service(build_scam_client())
    spawn(scam_svc.resume_pending_requests(), name="resume_pending_scam_requests")


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

    if shield_enabled():
        _start_shield()

    # Clear media files truncated by the old non-atomic downloader before the
    # UI can be served them (local-only; no Telegram needed).
    from backend.media import audit_media_files
    spawn(audit_media_files(), name="media_audit")

    # The vault is normally still locked here (credentials unavailable), in
    # which case unlock starts the connectors. If it is already unlocked (dev
    # reload), start now — in the background so startup never blocks on it.
    from backend.secrets import vault_status
    if vault_status() == "unlocked":
        spawn(manager.start_all(), name="startup_connectors")


@app.on_event("shutdown")
async def on_shutdown():
    await manager.stop_all()

if __name__ == "__main__":
    import sys

    import uvicorn
    import webview

    from backend.instance import (
        InstanceAlreadyRunning, acquire_instance_lock, focus_existing_instance,
        set_focus_callback, wait_until_ready,
    )

    # Single instance: a second launch focuses the running window instead of
    # silently starting on another port (which would change the origin).
    try:
        acquire_instance_lock(API_PORT)
    except InstanceAlreadyRunning as e:
        focus_existing_instance(e.port)
        sys.exit(0)

    server = uvicorn.Server(uvicorn.Config(app, host=API_HOST, port=API_PORT, log_level="info"))

    def run_server():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    # Wait for the readiness signal (migrations may take a while); bail out
    # if the server thread died during startup.
    if not wait_until_ready(API_PORT, t):
        print("Mingly failed to start; see the log for details.", file=sys.stderr)
        sys.exit(1)

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

    set_focus_callback(lambda: (window.restore(), window.show()))

    # Register the window so notification clicks can navigate to chats
    from backend.notify import set_webview_window
    set_webview_window(window)

    webview.start()

    # Window closed: let uvicorn run its lifespan shutdown (connectors stop,
    # WebSocket close, engine.dispose) instead of killing the daemon thread
    # mid-write. Bounded so a stuck shutdown can't hang the quit.
    server.should_exit = True
    t.join(timeout=15)
    if t.is_alive():
        print("Mingly: server shutdown timed out; exiting anyway.", file=sys.stderr)
