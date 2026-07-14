"""Bounded subprocess execution for trusted inspector adapters."""
from __future__ import annotations

import math
import os
import signal
import subprocess
import threading

from .base import (
    MAX_COMMAND_OUTPUT_CHARS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    InspectionError,
    _absolute_path,
    _open_real_directory,
    bounded_diagnostic,
)


def _bounded_pipe_reader(
    pipe,
    chunks: list[bytes],
    totals: list[int],
    errors: list[BaseException],
) -> None:
    try:
        while True:
            chunk = pipe.read(8_192)
            if not chunk:
                break
            totals[0] += len(chunk)
            remaining = MAX_COMMAND_OUTPUT_CHARS - sum(map(len, chunks))
            if remaining > 0:
                chunks.append(chunk[:remaining])
    except BaseException as exc:
        errors.append(exc)
    finally:
        pipe.close()


def _decode_output(chunks: list[bytes], total: int) -> str:
    captured = b"".join(chunks).decode("utf-8", errors="replace")
    if total > MAX_COMMAND_OUTPUT_CHARS:
        captured += (
            f"\n...[truncated {total - MAX_COMMAND_OUTPUT_CHARS} bytes]..."
        )
    return captured


def safe_run(
    command: list[str],
    *,
    timeout: int | float = 120,
    cwd: str,
    home: str,
) -> subprocess.CompletedProcess[str]:
    """Run a fixed argv with a minimal environment and bounded diagnostics."""
    if (
        not isinstance(command, list)
        or not command
        or any(
            not isinstance(part, str) or not part or "\x00" in part
            for part in command
        )
    ):
        raise InspectionError("invalid inspector command")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > MAX_COMMAND_TIMEOUT_SECONDS
    ):
        raise InspectionError("command timeout must be positive and bounded")
    cwd = _absolute_path(cwd, "command cwd")
    home = _absolute_path(home, "command home")
    cwd_fd = _open_real_directory(cwd, "command cwd")
    home_fd = _open_real_directory(home, "command home")
    os.close(home_fd)
    os.close(cwd_fd)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": home,
        "LANG": "C.UTF-8",
    }
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise InspectionError(
            "inspector command could not start: "
            f"{type(exc).__name__}: {bounded_diagnostic(exc)}"
        ) from exc

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_total = [0]
    stderr_total = [0]
    reader_errors: list[BaseException] = []
    readers = [
        threading.Thread(
            target=_bounded_pipe_reader,
            args=(
                process.stdout,
                stdout_chunks,
                stdout_total,
                reader_errors,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=_bounded_pipe_reader,
            args=(
                process.stderr,
                stderr_chunks,
                stderr_total,
                reader_errors,
            ),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        returncode = None
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            try:
                returncode = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
    for reader in readers:
        reader.join(timeout=2)
    if any(reader.is_alive() for reader in readers):
        raise InspectionError(
            "inspector command output reader did not finish"
        )
    if reader_errors:
        raise InspectionError(
            "inspector command output could not be read: "
            f"{bounded_diagnostic(reader_errors[0])}"
        )
    stdout = _decode_output(stdout_chunks, stdout_total[0])
    stderr = _decode_output(stderr_chunks, stderr_total[0])

    if timed_out:
        raise InspectionError(
            f"inspector command timed out after {timeout} seconds"
        )
    completed = subprocess.CompletedProcess(
        command,
        int(returncode),
        stdout=stdout,
        stderr=stderr,
    )
    if returncode != 0:
        diagnostic = stderr or stdout or "(no output)"
        raise InspectionError(
            f"inspector command exited {returncode}: "
            f"{bounded_diagnostic(diagnostic, MAX_COMMAND_OUTPUT_CHARS)}"
        )
    return completed
