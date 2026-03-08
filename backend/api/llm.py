import asyncio
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
from backend.llm.ollama import OllamaProvider

router = APIRouter()

# Where the app-managed Ollama binary lives
_OLLAMA_BIN = DATA_DIR / "bin" / "ollama"
# Ollama home — models and config kept inside the app's data dir
_OLLAMA_HOME = DATA_DIR / "ollama"

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


def _ollama_asset() -> tuple[str, str]:
    """Return (download_url, archive_format) for this platform.
    archive_format is 'tgz' or 'tar.zst'.
    """
    machine = platform.machine().lower()
    base = "https://github.com/ollama/ollama/releases/latest/download"
    if sys.platform == "darwin":
        return f"{base}/ollama-darwin.tgz", "tgz"
    elif sys.platform.startswith("linux"):
        suffix = "arm64" if ("arm" in machine or "aarch64" in machine) else "amd64"
        return f"{base}/ollama-linux-{suffix}.tar.zst", "tar.zst"
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


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
        url, fmt = _ollama_asset()
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
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)

    asyncio.create_task(_download_task())
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
    """Check if the Ollama process is reachable."""
    running = await _get_ollama().health_check()
    return {"running": running}


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

    asyncio.create_task(_pull_task())
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
