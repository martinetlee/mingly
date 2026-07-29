import asyncio
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.config import DATA_DIR
from backend.llm.base import LLMError
from backend.llm.config import (
    LLMInstanceConfig,
    LLMRegistryConfig,
    load_registry_config,
    save_registry_config,
)
from backend.llm.registry import get_registry
from backend.tasks import spawn
from backend.llm.ollama import OllamaProvider

router = APIRouter()

# Where the app-managed Ollama binary lives
_OLLAMA_BIN = DATA_DIR / "bin" / "ollama"
# Ollama home — models and config kept inside the app's data dir
_OLLAMA_HOME = DATA_DIR / "ollama"

# ---------------------------------------------------------------------------
# Pinned Ollama version and trusted SHA-256 checksums
# ---------------------------------------------------------------------------
_OLLAMA_VERSION = "v0.17.7"
_OLLAMA_SHA256: dict[str, str] = {
    "ollama-darwin.tgz": "a87a5d78825f91aee334020c868fba6c470da4e2bf21578d2ae1e36bb184ef35",
    "ollama-linux-amd64.tar.zst": "51ca1e0e9f66fa2493ced77debe8d9b9336ffc06271e6dcdde8b9f42b6c80a1d",
    "ollama-linux-arm64.tar.zst": "b39fff6bf90a799816794ef94695a978fee82912edc93ce6e3d85323f342ecb7",
}

# In-process job tracking (pull + install share the same dict)
_jobs: dict[int, dict] = {}
_job_counter = 0


def _next_job_id() -> int:
    global _job_counter
    _job_counter += 1
    return _job_counter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_ollama(base_url: str = "http://localhost:11434") -> OllamaProvider:
    """Return an OllamaProvider pointed at the given base URL (for admin ops)."""
    return OllamaProvider(base_url=f"{base_url.rstrip('/')}/v1", model="")


def _current_config() -> LLMRegistryConfig:
    """Rebuild config from live registry + persisted orchestrator settings."""
    registry = get_registry()
    persisted = load_registry_config()
    instances = [
        LLMInstanceConfig(**inst)
        for inst in registry.list_instances()
    ]
    return LLMRegistryConfig(
        instances=instances,
        orchestrator_name=persisted.orchestrator_name,
        orchestrator_prompt=persisted.orchestrator_prompt,
    )


# ---------------------------------------------------------------------------
# Registry — named instances
# ---------------------------------------------------------------------------

@router.get("/instances")
async def list_instances():
    """List all registered LLM instances with health status."""
    registry = get_registry()
    instances = registry.list_instances()
    health = await registry.health_check_all()
    for inst in instances:
        inst["healthy"] = health.get(inst["name"], False)
    return {"instances": instances}


class AddInstanceRequest(BaseModel):
    name: str
    provider: str
    model: str
    base_url: str = "http://localhost:11434/v1"
    system_prompt: str = ""


@router.post("/instances", status_code=201)
async def add_instance(req: AddInstanceRequest):
    """Register a new named LLM instance."""
    registry = get_registry()
    cfg = LLMInstanceConfig(
        name=req.name,
        provider=req.provider,
        model=req.model,
        base_url=req.base_url,
        system_prompt=req.system_prompt,
    )
    try:
        registry.add(cfg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    save_registry_config(_current_config())
    return {"name": cfg.name, "provider": cfg.provider, "model": cfg.model}


@router.delete("/instances/{name}")
async def remove_instance(name: str):
    """Remove a named LLM instance."""
    registry = get_registry()
    if name not in [i["name"] for i in registry.list_instances()]:
        raise HTTPException(status_code=404, detail=f"Instance {name!r} not found")
    registry.remove(name)
    save_registry_config(_current_config())
    return {"removed": name}


@router.get("/instances/{name}/health")
async def instance_health(name: str):
    """Health check for a single named instance."""
    registry = get_registry()
    try:
        instance = registry.get(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Instance {name!r} not found")
    healthy = await instance.health_check()
    return {"name": name, "healthy": healthy}


# ---------------------------------------------------------------------------
# Orchestrator config
# ---------------------------------------------------------------------------

class OrchestratorRequest(BaseModel):
    orchestrator_name: str
    orchestrator_prompt: str = ""


@router.get("/orchestrator")
async def get_orchestrator():
    """Get current orchestrator configuration."""
    cfg = load_registry_config()
    return {
        "orchestrator_name": cfg.orchestrator_name,
        "orchestrator_prompt": cfg.orchestrator_prompt,
    }


@router.put("/orchestrator")
async def set_orchestrator(req: OrchestratorRequest):
    """Set which instance acts as orchestrator and its routing prompt."""
    registry = get_registry()
    if req.orchestrator_name and req.orchestrator_name not in [
        i["name"] for i in registry.list_instances()
    ]:
        raise HTTPException(
            status_code=400,
            detail=f"Instance {req.orchestrator_name!r} not registered",
        )
    cfg = _current_config()
    cfg.orchestrator_name = req.orchestrator_name
    cfg.orchestrator_prompt = req.orchestrator_prompt
    save_registry_config(cfg)
    return {"orchestrator_name": cfg.orchestrator_name, "orchestrator_prompt": cfg.orchestrator_prompt}


# ---------------------------------------------------------------------------
# Ollama setup helpers
# ---------------------------------------------------------------------------

def _find_ollama_binary() -> Optional[str]:
    """Return the path to the ollama binary, or None if not found.
    Checks app-managed binary first, then system PATH, then macOS app bundle."""
    # App-managed binary (downloaded by install endpoint)
    if _OLLAMA_BIN.exists():
        return str(_OLLAMA_BIN)
    # System PATH
    found = shutil.which("ollama")
    if found:
        return found
    # macOS app bundle (may not be on PATH)
    mac_app = Path("/Applications/Ollama.app/Contents/Resources/ollama")
    if sys.platform == "darwin" and mac_app.exists():
        return str(mac_app)
    return None


def _ollama_asset() -> tuple[str, str, str]:
    """Return (download_url, archive_format, asset_name) for this platform.
    archive_format is 'tgz' or 'tar.zst'.
    """
    machine = platform.machine().lower()
    base = f"https://github.com/ollama/ollama/releases/download/{_OLLAMA_VERSION}"
    if sys.platform == "darwin":
        name = "ollama-darwin.tgz"
        return f"{base}/{name}", "tgz", name
    elif sys.platform.startswith("linux"):
        suffix = "arm64" if ("arm" in machine or "aarch64" in machine) else "amd64"
        name = f"ollama-linux-{suffix}.tar.zst"
        return f"{base}/{name}", "tar.zst", name
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def _verify_checksum(path: Path, asset_name: str) -> None:
    """Verify the SHA-256 digest of a downloaded archive against the pinned value.
    Raises RuntimeError if the digest does not match or the asset is unknown."""
    expected = _OLLAMA_SHA256.get(asset_name)
    if expected is None:
        raise RuntimeError(
            f"No trusted checksum for asset {asset_name!r}. "
            "Update _OLLAMA_SHA256 before installing on this platform."
        )
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            sha256.update(chunk)
    actual = sha256.hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"Checksum mismatch for {asset_name}: "
            f"expected {expected}, got {actual}. "
            "The downloaded file may be corrupted or tampered with."
        )


def _extract_ollama_binary(archive_path: Path, dest: Path, fmt: str = "tgz") -> None:
    """Extract the 'ollama' binary from the downloaded archive to dest."""
    if fmt == "tgz":
        with tarfile.open(archive_path, "r:gz") as tf:
            # Find the ollama binary (may be at root or in bin/)
            member = next(
                (m for m in tf.getmembers()
                 if m.name.rstrip("/").endswith("ollama") and not m.isdir()),
                None,
            )
            if not member:
                raise RuntimeError("ollama binary not found in archive")
            src = tf.extractfile(member)
            dest.write_bytes(src.read())
    else:
        # tar.zst — use zstd | tar pipeline (avoids --wildcards / --use-compress-program portability issues)
        dest.parent.mkdir(parents=True, exist_ok=True)
        zstd = subprocess.run(["zstd", "-d", "--stdout", str(archive_path)], capture_output=True)
        if zstd.returncode != 0:
            raise RuntimeError(f"zstd decompression failed: {zstd.stderr.decode()}")
        with tarfile.open(fileobj=io.BytesIO(zstd.stdout), mode="r|") as tf:
            member = next(
                (m for m in tf.getmembers()
                 if m.name.rstrip("/").endswith("ollama") and not m.isdir()),
                None,
            )
            if not member:
                raise RuntimeError("ollama binary not found in archive")
            src = tf.extractfile(member)
            dest.write_bytes(src.read())
    dest.chmod(0o755)


@router.get("/ollama/install-status")
async def ollama_install_status():
    """
    Returns:
      binary_found  — ollama binary exists on the system
      server_running — ollama API is reachable at localhost:11434
    """
    binary = _find_ollama_binary()
    running = await _get_ollama().health_check()
    return {
        "binary_found": binary is not None,
        "binary_path": binary,
        "server_running": running,
    }


@router.post("/ollama/install")
async def install_ollama():
    """
    Download the Ollama binary from GitHub releases into DATA_DIR/bin/ollama.
    Returns a job_id — stream progress via GET /api/llm/jobs/{job_id}.
    """
    if _OLLAMA_BIN.exists():
        return {"status": "already_installed", "path": str(_OLLAMA_BIN)}

    try:
        url, fmt, asset_name = _ollama_asset()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_id = _next_job_id()
    _jobs[job_id] = {"status": "downloading", "downloaded": 0, "total": 0}

    async def _download_task():
        _OLLAMA_BIN.parent.mkdir(parents=True, exist_ok=True)
        archive_tmp = _OLLAMA_BIN.parent / "ollama-archive.tmp"
        try:
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    _jobs[job_id]["total"] = total
                    downloaded = 0
                    with archive_tmp.open("wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            _jobs[job_id]["downloaded"] = downloaded

            # Verify download integrity before extraction (fail closed)
            _jobs[job_id]["status"] = "verifying"
            await asyncio.get_event_loop().run_in_executor(
                None, _verify_checksum, archive_tmp, asset_name
            )

            _jobs[job_id]["status"] = "extracting"
            # Run extraction in a thread so we don't block the event loop
            await asyncio.get_event_loop().run_in_executor(
                None, _extract_ollama_binary, archive_tmp, _OLLAMA_BIN, fmt
            )
            archive_tmp.unlink(missing_ok=True)
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["path"] = str(_OLLAMA_BIN)
        except Exception as e:
            archive_tmp.unlink(missing_ok=True)
            # If binary was partially written during a failed verification,
            # remove it so the system never runs an unverified binary.
            _OLLAMA_BIN.unlink(missing_ok=True)
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)

    def _mark_failed(exc: BaseException) -> None:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)

    spawn(_download_task(), name=f"ollama_download_{job_id}", on_error=_mark_failed)
    return {"job_id": job_id}


@router.post("/ollama/start")
async def start_ollama():
    """
    Start 'ollama serve' as a background subprocess.
    Returns immediately; poll GET /ollama/install-status until server_running=true.
    Models and config are stored in DATA_DIR/ollama/ (self-contained).
    """
    binary = _find_ollama_binary()
    if not binary:
        raise HTTPException(
            status_code=404,
            detail="Ollama binary not found. Call POST /api/llm/ollama/install first.",
        )
    running = await _get_ollama().health_check()
    if running:
        return {"status": "already_running"}

    _OLLAMA_HOME.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "OLLAMA_HOME": str(_OLLAMA_HOME)}

    subprocess.Popen(
        [binary, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    return {"status": "started"}


@router.get("/ollama/status")
async def ollama_status():
    """Check that the Ollama process is reachable and the orchestrator's model is installed.

    missing_model is the orchestrator's configured model name when the server is
    up but that model is absent; null otherwise (including when the orchestrator
    is not an Ollama instance).
    """
    provider = _get_ollama()
    running = await provider.health_check()
    missing_model: Optional[str] = None
    if running:
        cfg = _current_config()
        orch = next(
            (
                inst for inst in cfg.instances
                if inst.name == cfg.orchestrator_name and inst.provider == "ollama"
            ),
            None,
        )
        if orch and orch.model:
            try:
                names = [m.get("name", "") for m in await provider.list_models()]
                if not OllamaProvider._model_installed(orch.model, names):
                    missing_model = orch.model
            except LLMError:
                pass
    return {"running": running, "missing_model": missing_model}


@router.post("/ollama/stop")
async def stop_ollama():
    """Kill the Ollama server process."""
    running = await _get_ollama().health_check()
    if not running:
        return {"status": "not_running"}

    try:
        result = subprocess.run(
            ["pkill", "-f", "ollama serve"],
            capture_output=True,
            timeout=5,
        )
        # pkill returns 0 if it killed something, 1 if no process found
        if result.returncode in (0, 1):
            return {"status": "stopped"}
        raise HTTPException(status_code=500, detail=result.stderr.decode() or "pkill failed")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Timed out trying to stop Ollama")


@router.post("/ollama/restart")
async def restart_ollama():
    """Stop the Ollama server and start it again."""
    # Stop (best-effort)
    try:
        subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True, timeout=5)
        await asyncio.sleep(1.5)
    except Exception:
        pass

    # Start fresh
    binary = _find_ollama_binary()
    if not binary:
        raise HTTPException(
            status_code=404,
            detail="Ollama binary not found. Install it first.",
        )

    _OLLAMA_HOME.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "OLLAMA_HOME": str(_OLLAMA_HOME)}
    subprocess.Popen(
        [binary, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    return {"status": "restarting"}


@router.get("/ollama/models")
async def list_ollama_models():
    """List locally installed Ollama models."""
    try:
        models = await _get_ollama().list_models()
        return {"models": models}
    except LLMError as e:
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {e}")


class PullRequest(BaseModel):
    model: str  # e.g. "qwen2.5:7b"


@router.post("/ollama/pull")
async def start_pull(req: PullRequest):
    """
    Start a background model pull. Returns a job_id.
    Stream progress via GET /api/llm/jobs/{job_id}.
    """
    job_id = _next_job_id()
    _jobs[job_id] = {"status": "started", "type": "pull"}

    async def _pull_task():
        ollama = _get_ollama()
        try:
            async for progress in ollama.pull_model_stream(req.model):
                _jobs[job_id].update(progress)
                _jobs[job_id]["status"] = progress.get("status", "pulling")
        except LLMError as e:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)
        else:
            _jobs[job_id]["status"] = "done"

    def _mark_failed(exc: BaseException) -> None:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)

    spawn(_pull_task(), name=f"ollama_pull_{job_id}", on_error=_mark_failed)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
async def job_progress(job_id: int):
    """SSE stream of progress for any background job (install or pull)."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream():
        while True:
            job = _jobs.get(job_id, {})
            yield f"data: {json.dumps(job)}\n\n"
            if job.get("status") in ("done", "error"):
                break
            await asyncio.sleep(0.25)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
