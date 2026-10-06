"""Shared fixtures: an isolated SQLite DB and an authenticated API client.

The app's ``AsyncSessionLocal`` is a single sessionmaker imported by every
module, so rebinding it here points the whole backend at a temp database —
tests can never touch the real one.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from backend.api.dependencies import clear_session_token, generate_session_token
from backend.db import database


def _import_all_backend_modules():
    """Import every backend module up front so _isolate_data_dir can rewrite
    their DATA_DIR-derived constants before any test touches them."""
    import importlib
    import pkgutil

    import backend

    failures = []
    for info in pkgutil.walk_packages(backend.__path__, "backend."):
        if ".tests" in info.name or info.name.rsplit(".", 1)[-1].startswith("test_"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # a module that can't import is a bug, not a skip
            failures.append(f"{info.name}: {type(exc).__name__}: {exc}")
    if failures:
        raise ImportError("Backend modules failed to import:\n" + "\n".join(failures))


_import_all_backend_modules()


def pytest_configure(config):
    config.addinivalue_line("markers", "shield: run with the Shield (scam detection) switch on")


@pytest.fixture(autouse=True)
def _shield_switch(request, monkeypatch):
    """Shield is off by default (backend.config.SHIELD_ENABLED). Its own test
    modules (test_scam_*) and tests marked @pytest.mark.shield run with it on."""
    import backend.config as config

    module = request.module.__name__.rsplit(".", 1)[-1]
    on = module.startswith("test_scam_") or request.node.get_closest_marker("shield") is not None
    monkeypatch.setattr(config, "SHIELD_ENABLED", on)


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Redirect every module-level path under the real DATA_DIR (prefs, vault,
    LLM config, media, exports, …) into a per-test temp dir, so no test can
    read or write the user's real app data."""
    import sys
    from pathlib import Path

    import backend.config as config

    real = Path(config.DATA_DIR).resolve()
    fake = tmp_path / "appdata"
    fake.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", fake)
    for name, mod in list(sys.modules.items()):
        if not name.startswith("backend") or mod is None:
            continue
        for attr, val in list(vars(mod).items()):
            if not isinstance(val, Path):
                continue
            try:
                rel = val.resolve().relative_to(real)
            except (ValueError, OSError):
                continue
            monkeypatch.setattr(mod, attr, fake / rel)
    prefs = sys.modules.get("backend.prefs")
    if prefs is not None and hasattr(prefs, "invalidate_cache"):
        prefs.invalidate_cache()
    yield fake


@pytest_asyncio.fixture
async def db_engine(tmp_path):
    from backend.db.migrations.runner import run_migrations
    from backend.db import models  # noqa: F401  (registers tables)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    event.listen(engine.sync_engine, "connect", database._set_sqlite_pragmas)
    async with engine.connect() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
        await conn.commit()
        await run_migrations(conn, fresh=True)

    original_bind = database.AsyncSessionLocal.kw.get("bind")
    database.AsyncSessionLocal.configure(bind=engine)
    try:
        yield engine
    finally:
        database.AsyncSessionLocal.configure(bind=original_bind)
        await engine.dispose()


@pytest_asyncio.fixture
async def db(db_engine):
    async with database.AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def api(db_engine):
    """Authenticated client against the real app (startup hooks not run)."""
    from backend.api.router import create_app

    app = create_app()
    token = generate_session_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers={"X-Session-Token": token},
    ) as client:
        yield client
    clear_session_token()
