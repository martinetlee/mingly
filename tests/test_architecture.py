"""Architecture fitness tests: the backend's module layering.

Every ``backend.*`` import, including function-level ones, must point at the
same or a lower layer, and the module graph must stay acyclic. A violation
means a lower layer reached up (e.g. the data layer calling business rules,
or a feature importing an HTTP router) — fix the dependency, don't add an
exception here.

Layers, lowest first (a top-level package/module belongs to exactly one):
"""

import ast
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "backend"

LAYERS = [
    # Configuration, process plumbing, secrets, preferences, sessions.
    ("core", {"config", "errors", "fsutil", "aio", "tasks", "jobs", "features", "validators",
              "serialization", "instance", "prefs", "secrets", "secret_prefs", "sessions"}),
    # SQLAlchemy models, sessions, crud queries, migrations.
    ("data", {"db"}),
    # Building blocks every higher layer shares: JSON shapes, the WebSocket
    # broadcaster, native notifications, media storage.
    ("shared", {"serializers", "ws", "notify", "media"}),
    ("llm", {"llm"}),
    # Channel-neutral domain: people / identity linking, the ingest pipeline.
    ("domain", {"people", "ingest"}),
    # Channel connectors (the Connector contract, manager, each channel).
    ("channels", {"connectors", "telegram"}),
    # Features built on the channels and the LLM.
    ("features", {"briefing", "briefing_scheduler", "catchup_summary_scheduler", "scam", "ai",
                  "org_context", "user_orgs"}),
    # HTTP / WebSocket / MCP surface and the composition root.
    ("api", {"api", "wiring"}),
    ("entry", {"main"}),
]

# Third-party web framework types belong to the transport edges only.
WEB_FRAMEWORKS = ("fastapi", "starlette")
WEB_ALLOWED = {"api", "ws", "main"}

# A channel's client library and session internals stay inside its package:
# everything else goes through the connector (``connectors.roles``).
CHANNEL_INTERNALS = {
    "telegram": ("telethon", "backend.telegram.client", "backend.telegram.runtime"),
}


def _rank() -> dict:
    rank = {}
    for i, (_, members) in enumerate(LAYERS):
        for m in members:
            assert m not in rank, f"{m} is in two layers"
            rank[m] = i
    return rank


def _modules():
    """(module name, top-level part, parsed tree) for every backend module."""
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT.parent).with_suffix("")
        parts = rel.parts
        if "tests" in parts or "__pycache__" in parts:
            continue
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        if name == "backend":
            continue
        yield name, parts[1].removesuffix(".py") if len(parts) > 1 else parts[0], ast.parse(path.read_text())


def _imports(tree):
    """Every imported module name, at any depth of the file."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, None
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for a in node.names:
                yield node.module, a.name


def _backend_target(module: str, name):
    if module == "backend":
        return f"backend.{name}"
    return module if module.startswith("backend.") else None


def test_every_backend_module_is_in_a_layer():
    rank = _rank()
    missing = sorted({top for _, top, _ in _modules()} - set(rank))
    assert missing == [], f"assign these to a layer in LAYERS: {missing}"


def test_imports_never_point_to_a_higher_layer():
    rank = _rank()
    offenders = []
    for name, top, tree in _modules():
        for module, imported in _imports(tree):
            target = _backend_target(module, imported)
            if target is None:
                continue
            target_top = target.split(".")[1]
            if rank[target_top] > rank[top]:
                offenders.append(f"{name} ({LAYERS[rank[top]][0]}) -> {target} ({LAYERS[rank[target_top]][0]})")
    assert offenders == []


def test_module_graph_is_acyclic():
    known = {name for name, _, _ in _modules()}
    graph = collections.defaultdict(set)
    for name, _, tree in _modules():
        for module, imported in _imports(tree):
            target = _backend_target(module, imported)
            if target is None:
                continue
            candidate = f"{target}.{imported}" if imported else target
            target = candidate if candidate in known else target
            while target not in known and "." in target:
                target = target.rsplit(".", 1)[0]
            if target in known and target != name:
                graph[name].add(target)

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    index, low, stack, on_stack, cycles, counter = {}, {}, [], set(), [], [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                cycles.append(sorted(component))

    for v in sorted(known):
        if v not in index:
            visit(v)
    assert cycles == []


def test_web_framework_stays_at_the_transport_edges():
    offenders = sorted(
        f"{name} imports {module}"
        for name, top, tree in _modules()
        for module, _ in _imports(tree)
        if module.split(".")[0] in WEB_FRAMEWORKS and top not in WEB_ALLOWED
    )
    assert offenders == []


def test_channel_client_libraries_stay_in_their_package():
    offenders = sorted(
        f"{name} imports {module}"
        for name, top, tree in _modules()
        for module, imported in _imports(tree)
        for package, internals in CHANNEL_INTERNALS.items()
        if top != package and any(
            module == m or module.startswith(m + ".")
            or (module == "backend.telegram" and f"backend.telegram.{imported}" in internals)
            for m in internals
        )
    )
    assert offenders == []
