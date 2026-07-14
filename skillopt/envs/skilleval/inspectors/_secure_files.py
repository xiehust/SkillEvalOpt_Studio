"""Stable descriptor handoff for untrusted evidence and rendered files."""
from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from typing import Iterator

from ._scratch import ScratchGuard
from .base import (
    InspectionError,
    _absolute_path,
    _open_child,
    _open_real_directory,
    bounded_diagnostic,
    validate_logical_path,
)

_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_COPY_CHUNK_BYTES = 1024 * 1024


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _stable_file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _derived_parts(scratch_dir: str, derived_path: str) -> tuple[str, ...]:
    scratch = _absolute_path(scratch_dir, "scratch root")
    if (
        not isinstance(derived_path, str)
        or not derived_path
        or "\x00" in derived_path
        or "\\" in derived_path
    ):
        raise InspectionError(
            "derived path must be non-empty and contain no backslash or NUL"
        )
    if not os.path.isabs(derived_path):
        return validate_logical_path(derived_path)
    if os.path.normpath(derived_path) != derived_path:
        raise InspectionError("derived path must be normalized")
    candidate = os.path.abspath(derived_path)
    try:
        relative = os.path.relpath(candidate, scratch)
    except ValueError as exc:
        raise InspectionError("derived path is outside scratch") from exc
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise InspectionError("derived path is outside scratch")
    return validate_logical_path(relative.replace(os.sep, "/"))


def open_scratch_file(scratch_dir: str, derived_path: str) -> int:
    """Open one regular scratch file without a validate/reopen gap."""
    parts = _derived_parts(scratch_dir, derived_path)
    descriptor = _open_real_directory(scratch_dir, "scratch root")
    try:
        for part in parts[:-1]:
            child = _open_child(
                descriptor,
                part,
                directory=True,
                label=f"derived path {derived_path!r}",
            )
            os.close(descriptor)
            descriptor = child
        output = _open_child(
            descriptor,
            parts[-1],
            directory=False,
            label=f"derived path {derived_path!r}",
        )
        if not stat.S_ISREG(os.fstat(output).st_mode):
            os.close(output)
            raise InspectionError("derived path must be a regular file")
        return output
    finally:
        os.close(descriptor)


def _open_evidence_file(evidence_dir: str, logical_path: str) -> int:
    parts = validate_logical_path(logical_path)
    descriptor = _open_real_directory(evidence_dir, "evidence root")
    try:
        for part in parts[:-1]:
            child = _open_child(
                descriptor,
                part,
                directory=True,
                label=f"artifact path {logical_path!r}",
            )
            os.close(descriptor)
            descriptor = child
        artifact = _open_child(
            descriptor,
            parts[-1],
            directory=False,
            label=f"artifact path {logical_path!r}",
        )
        if not stat.S_ISREG(os.fstat(artifact).st_mode):
            os.close(artifact)
            raise InspectionError(
                f"artifact must be a regular file: {logical_path!r}"
            )
        return artifact
    finally:
        os.close(descriptor)


def _create_stage_directory(scratch_descriptor: int) -> tuple[str, int]:
    for _attempt in range(16):
        name = f".skillopt-input-{secrets.token_hex(12)}"
        try:
            os.mkdir(name, 0o700, dir_fd=scratch_descriptor)
        except FileExistsError:
            continue
        try:
            descriptor = os.open(
                name,
                _DIRECTORY_FLAGS,
                dir_fd=scratch_descriptor,
            )
        except Exception:
            os.rmdir(name, dir_fd=scratch_descriptor)
            raise
        return name, descriptor
    raise InspectionError("could not allocate a scratch staging directory")


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise InspectionError("staged evidence copy could not be written")
        view = view[written:]


def _copy_stable(
    source: int,
    destination: int,
    *,
    remaining_bytes: int,
    logical_path: str,
) -> None:
    before = os.fstat(source)
    if not stat.S_ISREG(before.st_mode):
        raise InspectionError(
            f"artifact must be a regular file: {logical_path!r}"
        )
    if before.st_size > remaining_bytes:
        raise InspectionError(
            "scratch byte budget exceeded while staging evidence"
        )
    copied = 0
    while copied < before.st_size:
        chunk_size = min(_COPY_CHUNK_BYTES, before.st_size - copied)
        if hasattr(os, "pread"):
            chunk = os.pread(source, chunk_size, copied)
        else:  # pragma: no cover - supported production targets expose pread
            os.lseek(source, copied, os.SEEK_SET)
            chunk = os.read(source, chunk_size)
        if not chunk:
            raise InspectionError(
                f"artifact changed while being staged: {logical_path!r}"
            )
        if copied + len(chunk) > remaining_bytes:
            raise InspectionError(
                "scratch byte budget exceeded while staging evidence"
            )
        _write_all(destination, chunk)
        copied += len(chunk)
    after = os.fstat(source)
    if (
        copied != before.st_size
        or _stable_file_identity(before) != _stable_file_identity(after)
    ):
        raise InspectionError(
            f"artifact changed while being staged: {logical_path!r}"
        )
    if os.fstat(destination).st_size != copied:
        raise InspectionError("staged evidence copy has an invalid size")


@contextmanager
def staged_evidence_path(
    evidence_dir: str,
    logical_path: str,
    scratch_dir: str,
    scratch_guard: ScratchGuard,
) -> Iterator[str]:
    """Yield a controlled read-only copy made from a stable evidence fd."""
    parts = validate_logical_path(logical_path)
    source = _open_evidence_file(evidence_dir, logical_path)
    stage_name = ""
    stage_descriptor = -1
    destination = -1
    basename = parts[-1]
    stage_info: os.stat_result | None = None
    try:
        remaining = scratch_guard.remaining_bytes()
        stage_name, stage_descriptor = _create_stage_directory(
            scratch_guard.descriptor
        )
        stage_info = os.fstat(stage_descriptor)
        try:
            destination = os.open(
                basename,
                _CREATE_FLAGS,
                0o600,
                dir_fd=stage_descriptor,
            )
        except OSError as exc:
            raise InspectionError(
                "staged evidence file could not be created: "
                f"{bounded_diagnostic(exc)}"
            ) from exc
        _copy_stable(
            source,
            destination,
            remaining_bytes=remaining,
            logical_path=logical_path,
        )
        os.fchmod(destination, 0o400)
        os.close(destination)
        destination = -1
        yield os.path.join(
            _absolute_path(scratch_dir, "scratch root"),
            stage_name,
            basename,
        )
    finally:
        if destination >= 0:
            os.close(destination)
        os.close(source)
        cleanup_error: Exception | None = None
        if stage_descriptor >= 0:
            try:
                try:
                    os.unlink(basename, dir_fd=stage_descriptor)
                except FileNotFoundError:
                    pass
            except OSError as exc:
                cleanup_error = exc
            os.close(stage_descriptor)
        if stage_name and stage_info is not None:
            try:
                current = os.stat(
                    stage_name,
                    dir_fd=scratch_guard.descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(current.st_mode) or not _same_object(
                    stage_info,
                    current,
                ):
                    raise InspectionError(
                        "scratch staging directory changed during inspection"
                    )
                os.rmdir(stage_name, dir_fd=scratch_guard.descriptor)
            except FileNotFoundError:
                cleanup_error = cleanup_error or InspectionError(
                    "scratch staging directory disappeared during inspection"
                )
            except (OSError, InspectionError) as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise InspectionError(
                "scratch staging cleanup failed: "
                f"{bounded_diagnostic(cleanup_error)}"
            ) from cleanup_error
