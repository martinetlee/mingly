"""signal-cli's ``jsonRpc`` mode: JSON-RPC 2.0 over the child's stdio.

Requests and responses are single JSON lines; incoming messages arrive as
``receive`` notifications (``{"method": "receive", "params": {"envelope":
…, "account": …}}``). One ``JsonRpcProcess`` is one child process; the
connector's supervisor creates a new one after a crash.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from backend.connectors.signal.cli import STREAM_LIMIT, child_env

logger = logging.getLogger(__name__)

CALL_TIMEOUT = 120


class RpcError(Exception):
    """signal-cli answered a request with an error."""

    def __init__(self, message: str, code: Optional[int] = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class ProcessGone(RpcError):
    """The child exited before answering."""


Notification = Callable[[str, dict], Awaitable[None]]


class JsonRpcProcess:
    def __init__(self, argv: list[str], on_notification: Notification) -> None:
        self._argv = argv
        self._on_notification = on_notification
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._readers: list[asyncio.Task] = []
        self.stderr_tail: collections.deque = collections.deque(maxlen=20)

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
            limit=STREAM_LIMIT,
        )
        self._readers = [
            asyncio.ensure_future(self._read_stdout()),
            asyncio.ensure_future(self._read_stderr()),
        ]

    async def wait(self) -> int:
        """Until the child exited and its output is consumed; the exit code."""
        assert self._proc is not None
        code = await self._proc.wait()
        await asyncio.gather(*self._readers, return_exceptions=True)
        self._fail_pending(ProcessGone(f"signal-cli exited with code {code}"))
        return code

    def last_error(self) -> Optional[str]:
        lines = [line for line in self.stderr_tail if line.strip()]
        return lines[-1] if lines else None

    async def call(self, method: str, params: Optional[dict] = None, timeout: float = CALL_TIMEOUT) -> Any:
        if self._proc is None or self._proc.returncode is not None or self._proc.stdin is None:
            raise ProcessGone("signal-cli is not running")
        self._next_id += 1
        req_id = str(self._next_id)
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        line = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": req_id})
        try:
            async with self._write_lock:
                self._proc.stdin.write(line.encode() + b"\n")
                await self._proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout)
        except (BrokenPipeError, ConnectionResetError) as e:
            raise ProcessGone(f"signal-cli is not running ({e})") from e
        finally:
            self._pending.pop(req_id, None)

    async def stop(self, timeout: float = 5.0) -> None:
        """Close stdin (signal-cli exits cleanly on EOF), then terminate and
        finally kill a child that doesn't."""
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
        for signal_child in (None, proc.terminate, proc.kill):
            if proc.returncode is not None:
                break
            if signal_child is not None:
                try:
                    signal_child()
                except ProcessLookupError:
                    break
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                continue
        await asyncio.gather(*self._readers, return_exceptions=True)
        self._fail_pending(ProcessGone("signal-cli stopped"))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            try:
                raw = await self._proc.stdout.readline()
            except ValueError:  # a line over STREAM_LIMIT: skip what's buffered
                logger.warning("signal-cli sent an oversized line; skipped")
                continue
            if not raw:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                logger.debug("signal-cli stdout (not JSON): %r", raw[:200])
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                fut = self._pending.get(str(msg["id"]))
                if fut is None or fut.done():
                    continue
                err = msg.get("error")
                if err:
                    fut.set_exception(RpcError(str(err.get("message") or err), err.get("code"), err.get("data")))
                else:
                    fut.set_result(msg.get("result"))
            elif msg.get("method"):
                try:
                    await self._on_notification(msg["method"], msg.get("params") or {})
                except Exception:
                    logger.exception("Signal notification handler failed")

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            try:
                raw = await self._proc.stderr.readline()
            except ValueError:
                continue
            if not raw:
                return
            line = raw.decode(errors="replace").rstrip()
            self.stderr_tail.append(line)
            logger.debug("signal-cli: %s", line)
