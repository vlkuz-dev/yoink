from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from yoink.downloader.runner import (
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)


def _py(*code_parts: str) -> list[str]:
    return [sys.executable, "-c", "\n".join(code_parts)]


class TestRunSubprocess:
    async def test_captures_stdout_on_success(self, tmp_path: Path) -> None:
        result = await run_subprocess(
            _py("import sys", "sys.stdout.write('hello')"),
            cwd=tmp_path,
            timeout_s=5.0,
        )
        assert isinstance(result, SubprocessResult)
        assert result.returncode == 0
        assert result.stdout == b"hello"
        assert result.stderr == ""
        assert result.duration_s >= 0.0

    async def test_captures_stderr_on_failure(self, tmp_path: Path) -> None:
        result = await run_subprocess(
            _py("import sys", "sys.stderr.write('boom')", "sys.exit(2)"),
            cwd=tmp_path,
            timeout_s=5.0,
        )
        assert result.returncode == 2
        assert result.stdout == b""
        assert "boom" in result.stderr

    async def test_timeout_kills_process_and_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SubprocessTimeoutError) as excinfo:
            await run_subprocess(
                _py("import time", "time.sleep(5)"),
                cwd=tmp_path,
                timeout_s=0.2,
            )
        assert excinfo.value.timeout_s == 0.2
        assert excinfo.value.cmd[0] == sys.executable

    async def test_no_shell_injection_args_are_literal(self, tmp_path: Path) -> None:
        marker = tmp_path / "should_not_be_deleted.txt"
        marker.write_text("keep me")
        # If args were ever passed through a shell, `; rm -rf ...` would execute.
        cmd = [
            *_py("import sys", "sys.stdout.write(repr(sys.argv[1:]))"),
            "; rm -rf /",
            "&& echo pwned",
        ]
        result = await run_subprocess(
            cmd,
            cwd=tmp_path,
            timeout_s=5.0,
        )
        assert result.returncode == 0
        decoded = result.stdout.decode()
        assert "; rm -rf /" in decoded
        assert "&& echo pwned" in decoded
        assert marker.exists()
        assert marker.read_text() == "keep me"

    async def test_cwd_is_respected(self, tmp_path: Path) -> None:
        result = await run_subprocess(
            _py("import os, sys", "sys.stdout.write(os.getcwd())"),
            cwd=tmp_path,
            timeout_s=5.0,
        )
        assert result.returncode == 0
        assert Path(result.stdout.decode()).resolve() == tmp_path.resolve()

    async def test_env_is_passed_through(self, tmp_path: Path) -> None:
        env = {**os.environ, "YOINK_TEST_TOKEN": "abc123"}
        result = await run_subprocess(
            _py(
                "import os, sys",
                "sys.stdout.write(os.environ.get('YOINK_TEST_TOKEN', ''))",
            ),
            cwd=tmp_path,
            timeout_s=5.0,
            env=env,
        )
        assert result.returncode == 0
        assert result.stdout == b"abc123"

    async def test_stderr_is_capped(self, tmp_path: Path) -> None:
        cap = 256
        result = await run_subprocess(
            _py(
                "import sys",
                "sys.stderr.write('x' * 4096)",
            ),
            cwd=tmp_path,
            timeout_s=5.0,
            stderr_cap_bytes=cap,
        )
        assert result.returncode == 0
        assert "stderr truncated" in result.stderr
        head_len = len(result.stderr.split("\n[...stderr truncated")[0])
        assert head_len == cap

    async def test_empty_cmd_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            await run_subprocess([], cwd=tmp_path, timeout_s=1.0)

    async def test_non_positive_timeout_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="timeout"):
            await run_subprocess([sys.executable, "-V"], cwd=tmp_path, timeout_s=0)

    async def test_cancellation_kills_subprocess(self, tmp_path: Path) -> None:
        marker = tmp_path / "child.pid"
        cmd = _py(
            "import os, sys, time",
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))",
            "sys.stdout.flush()",
            "time.sleep(30)",
        )

        task = asyncio.create_task(
            run_subprocess(cmd, cwd=tmp_path, timeout_s=30.0)
        )

        for _ in range(50):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert marker.exists(), "child did not start"
        pid = int(marker.read_text())

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError(f"subprocess {pid} still alive after cancel")
