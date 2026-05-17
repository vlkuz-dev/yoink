from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_STDERR_CAP_BYTES = 64 * 1024


class SubprocessTimeoutError(TimeoutError):
    """Raised when a subprocess exceeds its allowed wall-clock duration."""

    def __init__(self, cmd: list[str], timeout_s: float) -> None:
        super().__init__(f"subprocess timed out after {timeout_s}s: {cmd!r}")
        self.cmd = list(cmd)
        self.timeout_s = timeout_s


@dataclass(slots=True, frozen=True, kw_only=True)
class SubprocessResult:
    returncode: int
    stdout: bytes
    stderr: str
    duration_s: float


def _truncate_stderr(raw: bytes, cap: int) -> str:
    if not raw:
        return ""
    if len(raw) <= cap:
        return raw.decode("utf-8", errors="replace")
    head = raw[:cap].decode("utf-8", errors="replace")
    return head + f"\n[...stderr truncated at {cap} bytes...]"


async def run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    env: dict[str, str] | None = None,
    stderr_cap_bytes: int = _DEFAULT_STDERR_CAP_BYTES,
) -> SubprocessResult:
    if not cmd:
        raise ValueError("cmd must be a non-empty list")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")

    loop = asyncio.get_running_loop()
    start = loop.time()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        raise SubprocessTimeoutError(cmd, timeout_s) from None
    except asyncio.CancelledError:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        raise

    duration = loop.time() - start
    return SubprocessResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout,
        stderr=_truncate_stderr(stderr, stderr_cap_bytes),
        duration_s=duration,
    )
