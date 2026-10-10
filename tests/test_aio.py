"""asyncio primitives vs. the packaged app's two event loops.

``backend/main.py`` imports the backend on the main thread and serves on a
new loop in another thread. On Python 3.9 a Lock / Event / Semaphore / Queue
built at import time (or cached on a first loop) raises "got Future attached
to a different loop" at its first contended wait on the server loop. These
tests reproduce that layout: the singletons were created when the backend
was imported, and the waits run on a fresh loop in a thread.
"""

import ast
import asyncio
import threading
from pathlib import Path

import pytest

PRIMITIVES = {"Lock", "Event", "Condition", "Semaphore", "BoundedSemaphore", "Queue", "PriorityQueue", "LifoQueue"}
BACKEND = Path(__file__).resolve().parent.parent / "backend"


def _on_server_loop(make_coro):
    """Run ``make_coro()`` on a new loop in another thread, like main.py."""
    out: dict = {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            out["value"] = loop.run_until_complete(asyncio.wait_for(make_coro(), 5))
        except BaseException as e:  # re-raised on the test thread
            out["error"] = e
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    if "error" in out:
        raise out["error"]
    return out.get("value")


async def _contend(lock) -> None:
    async def hold():
        async with lock:
            await asyncio.sleep(0.01)
    await asyncio.gather(hold(), hold(), hold())


def test_import_time_singleton_locks_work_on_the_server_loop():
    from backend.telegram import client as tg_client
    from backend.telegram.coordinator import coordinator
    from backend.telegram.runtime import runtime

    for lock in (runtime._lock, tg_client.connect_lock, coordinator.limiter._lock):
        _on_server_loop(lambda: _contend(lock))
        _on_server_loop(lambda: _contend(lock))  # and again on a later loop


def test_archive_wake_event_cached_on_one_loop_works_on_the_next():
    from backend.telegram.archive import archive

    async def wake_archive():
        ev = archive._event()
        ev.clear()
        waiter = asyncio.ensure_future(ev.wait())
        await asyncio.sleep(0)
        archive._poke()
        await waiter

    _on_server_loop(wake_archive)
    _on_server_loop(wake_archive)


def test_briefing_replace_lock_works_on_every_loop():
    from backend.briefing import pipeline

    _on_server_loop(lambda: _contend(pipeline._replace_lock))
    _on_server_loop(lambda: _contend(pipeline._replace_lock))


def test_gmail_fetch_semaphore_and_connector_lifecycle_follow_the_loop():
    from backend.connectors.gmail.connector import GmailConnector

    c = GmailConnector(1)

    async def use():
        await _contend(c._sem.get())
        await _contend(c._lifecycle())

    _on_server_loop(use)
    _on_server_loop(use)


def test_loop_lock_reports_locked_only_on_its_loop():
    from backend.aio import LoopLock

    lock = LoopLock()
    assert not lock.locked()  # no loop: nothing built

    async def check():
        async with lock:
            assert lock.locked()
        return lock.locked()

    assert _on_server_loop(check) is False


# ── guard: no asyncio primitive may be built at import time ─────────────────────

def _is_primitive_call(node: ast.AST, bare: set) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in PRIMITIVES and isinstance(f.value, ast.Name) and f.value.id == "asyncio":
        return True
    return isinstance(f, ast.Name) and f.id in bare


def _calls_in(nodes, bare) -> list:
    found = []
    for n in nodes:
        for sub in ast.walk(n):
            if _is_primitive_call(sub, bare):
                found.append(sub.lineno)
    return found


def _import_time_code(tree: ast.Module):
    """Statements that run on import: module level (minus function bodies),
    class bodies, decorators and default arguments."""
    out = []

    def visit(body):
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.extend(stmt.decorator_list)
                out.extend(d for d in stmt.args.defaults + stmt.args.kw_defaults if d is not None)
            elif isinstance(stmt, ast.ClassDef):
                out.extend(stmt.decorator_list)
                visit(stmt.body)
            else:
                out.append(stmt)
    visit(tree.body)
    return out


def _init_calls(classes: dict, name: str, bare: set, seen: set) -> list:
    """Primitive constructions in ``name.__init__`` and in the ``__init__`` of
    classes of the same module it instantiates."""
    if name in seen or name not in classes:
        return []
    seen.add(name)
    init = next((s for s in classes[name].body if isinstance(s, ast.FunctionDef) and s.name == "__init__"), None)
    if init is None:
        return []
    found = [f"{name}.__init__:{ln}" for ln in _calls_in(init.body, bare)]
    for sub in ast.walk(init):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            found += _init_calls(classes, sub.func.id, bare, seen)
    return found


def test_no_asyncio_primitive_is_built_at_import_time():
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        bare = {a.asname or a.name for s in ast.walk(tree) if isinstance(s, ast.ImportFrom) and s.module == "asyncio"
                for a in s.names if a.name in PRIMITIVES}
        rel = path.relative_to(BACKEND.parent)
        offenders += [f"{rel}:{ln}" for ln in _calls_in(_import_time_code(tree), bare)]
        classes = {s.name: s for s in ast.walk(tree) if isinstance(s, ast.ClassDef)}
        for stmt in tree.body:  # module-level singletons: ``x = SomeClass(...)``
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Name):
                offenders += [f"{rel} {hit}" for hit in _init_calls(classes, stmt.value.func.id, bare, set())]
    assert offenders == [], "use backend.aio.LoopLocal / LoopLock instead:\n" + "\n".join(offenders)


def test_the_guard_catches_a_singleton_lock(tmp_path):
    src = (
        "import asyncio\n"
        "class Limiter:\n    def __init__(self):\n        self._lock = asyncio.Lock()\n"
        "class Runtime:\n    def __init__(self):\n        self.limiter = Limiter()\n"
        "runtime = Runtime()\n"
    )
    tree = ast.parse(src)
    classes = {s.name: s for s in ast.walk(tree) if isinstance(s, ast.ClassDef)}
    assert _init_calls(classes, "Runtime", set(), set()) == ["Limiter.__init__:4"]
    assert _calls_in(_import_time_code(ast.parse("import asyncio\nLOCK = asyncio.Lock()\n")), set()) == [2]


@pytest.mark.asyncio
async def test_loop_local_builds_once_per_loop():
    from backend.aio import LoopLocal

    made = []
    cell = LoopLocal(lambda: made.append(1) or object())
    assert cell.current() is None
    first = cell.get()
    assert cell.get() is first and cell.current() is first and len(made) == 1
