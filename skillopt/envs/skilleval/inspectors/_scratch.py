"""Request-local scratch transactions with bounded rollback."""
from __future__ import annotations

import contextvars
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

from .base import (
    InspectionError,
    _absolute_path,
    _open_real_directory,
    validate_logical_path,
)

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_current_transaction: contextvars.ContextVar[ScratchTransaction | None] = (
    contextvars.ContextVar("artifact_scratch_transaction", default=None)
)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


@dataclass(frozen=True)
class ScratchLimits:
    max_bytes: int
    max_entries: int
    max_depth: int


@dataclass(frozen=True)
class ScratchUsage:
    bytes: int = 0
    entries: int = 0
    depth: int = 0

    def plus(self, other: ScratchUsage) -> ScratchUsage:
        return ScratchUsage(
            bytes=self.bytes + other.bytes,
            entries=self.entries + other.entries,
            depth=max(self.depth, other.depth),
        )


@dataclass
class _RemovalFrame:
    descriptor: int
    names: list[str]
    parent_descriptor: int | None = None
    name: str = ""
    identity: os.stat_result | None = None
    owned: bool = False
    index: int = 0


def _raise_if_over(usage: ScratchUsage, limits: ScratchLimits) -> None:
    if usage.bytes > limits.max_bytes:
        raise InspectionError(
            "scratch byte budget exceeded: "
            f"{usage.bytes} > {limits.max_bytes}"
        )
    if usage.entries > limits.max_entries:
        raise InspectionError(
            "scratch entry budget exceeded: "
            f"{usage.entries} > {limits.max_entries}"
        )
    if usage.depth > limits.max_depth:
        raise InspectionError(
            "scratch depth budget exceeded: "
            f"{usage.depth} > {limits.max_depth}"
        )


def _scan_tree(
    descriptor: int,
    limits: ScratchLimits,
    *,
    depth: int = 0,
    logical: str = "",
) -> ScratchUsage:
    with os.scandir(descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    usage = ScratchUsage()
    for name in names:
        path = f"{logical}/{name}" if logical else name
        before = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        entry_depth = depth + 1
        child_usage = ScratchUsage(entries=1, depth=entry_depth)
        _raise_if_over(usage.plus(child_usage), limits)
        if stat.S_ISLNK(before.st_mode):
            raise InspectionError(f"scratch contains a symlink: {path}")
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if not _same_file(before, opened):
                    raise InspectionError(
                        f"scratch directory changed while scanning: {path}"
                    )
                child_usage = child_usage.plus(
                    _scan_tree(
                        child,
                        limits,
                        depth=entry_depth,
                        logical=path,
                    )
                )
            finally:
                os.close(child)
        elif stat.S_ISREG(before.st_mode):
            child = os.open(name, _FILE_FLAGS, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if not _same_file(before, opened):
                    raise InspectionError(
                        f"scratch file changed while scanning: {path}"
                    )
                child_usage = ScratchUsage(
                    bytes=opened.st_size,
                    entries=1,
                    depth=entry_depth,
                )
            finally:
                os.close(child)
        else:
            raise InspectionError(
                f"scratch contains a non-regular entry: {path}"
            )
        usage = usage.plus(child_usage)
        _raise_if_over(usage, limits)
    return usage


def _scan_descriptor(
    descriptor: int,
    limits: ScratchLimits,
) -> ScratchUsage:
    scan = os.open(".", _DIRECTORY_FLAGS, dir_fd=descriptor)
    try:
        usage = _scan_tree(scan, limits)
    finally:
        os.close(scan)
    _raise_if_over(usage, limits)
    return usage


def scratch_bytes(scratch_dir: str, maximum: int) -> int:
    """Return bounded regular-file bytes under a stable scratch root."""
    descriptor = _open_real_directory(scratch_dir, "scratch root")
    try:
        usage = _scan_descriptor(
            descriptor,
            ScratchLimits(
                max_bytes=maximum,
                max_entries=100_000,
                max_depth=64,
            ),
        )
        return usage.bytes
    finally:
        os.close(descriptor)


def _remove_tree(descriptor: int) -> None:
    with os.scandir(descriptor) as entries:
        root_names = sorted(entry.name for entry in entries)
    stack = [_RemovalFrame(descriptor, root_names)]
    try:
        while stack:
            frame = stack[-1]
            if frame.index < len(frame.names):
                name = frame.names[frame.index]
                frame.index += 1
                info = os.stat(
                    name,
                    dir_fd=frame.descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(
                    info.st_mode
                ):
                    child = os.open(
                        name,
                        _DIRECTORY_FLAGS,
                        dir_fd=frame.descriptor,
                    )
                    opened = os.fstat(child)
                    if not _same_file(info, opened):
                        os.close(child)
                        raise InspectionError(
                            "scratch entry changed during rollback"
                        )
                    with os.scandir(child) as entries:
                        child_names = sorted(
                            entry.name for entry in entries
                        )
                    stack.append(
                        _RemovalFrame(
                            child,
                            child_names,
                            parent_descriptor=frame.descriptor,
                            name=name,
                            identity=opened,
                            owned=True,
                        )
                    )
                else:
                    os.unlink(name, dir_fd=frame.descriptor)
                continue
            if frame.parent_descriptor is not None:
                current = os.stat(
                    frame.name,
                    dir_fd=frame.parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    frame.identity is None
                    or not _same_file(frame.identity, current)
                ):
                    raise InspectionError(
                        "scratch directory changed during rollback"
                    )
                os.rmdir(frame.name, dir_fd=frame.parent_descriptor)
                os.close(frame.descriptor)
                frame.owned = False
            stack.pop()
    except BaseException:
        for frame in reversed(stack):
            if frame.owned:
                try:
                    os.close(frame.descriptor)
                except OSError:
                    pass
                frame.owned = False
        raise


def _open_relative_parent(
    root_descriptor: int,
    parts: tuple[str, ...],
) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts:
            child = os.open(
                part,
                _DIRECTORY_FLAGS,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


@dataclass
class ScratchFile:
    logical_path: str
    descriptor: int

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


class ScratchTransaction:
    """One isolated request tree that is removed unless outputs are committed."""

    def __init__(
        self,
        scratch_dir: str,
        *,
        max_bytes: int,
        max_entries: int,
        max_depth: int,
    ) -> None:
        self.root_path = _absolute_path(scratch_dir, "scratch root")
        self.requested_limits = ScratchLimits(
            max_bytes=max_bytes,
            max_entries=max_entries,
            max_depth=max_depth,
        )
        self.limits = self.requested_limits
        self.root_descriptor = -1
        self.descriptor = -1
        self.name = ""
        self._root_identity: os.stat_result | None = None
        self._transaction_identity: os.stat_result | None = None
        self._committed_name = ""
        self._committed_identity: os.stat_result | None = None
        self._token: contextvars.Token[ScratchTransaction | None] | None = None

    @property
    def proc_path(self) -> str:
        if self.descriptor < 0:
            raise InspectionError("scratch transaction is not active")
        return f"/proc/self/fd/{self.descriptor}"

    def __enter__(self) -> ScratchTransaction:
        try:
            self.root_descriptor = _open_real_directory(
                self.root_path,
                "scratch root",
            )
            self._root_identity = os.fstat(self.root_descriptor)
            baseline = _scan_descriptor(
                self.root_descriptor,
                self.requested_limits,
            )
            self.limits = ScratchLimits(
                max_bytes=self.requested_limits.max_bytes - baseline.bytes,
                max_entries=(
                    self.requested_limits.max_entries - baseline.entries
                ),
                max_depth=self.requested_limits.max_depth,
            )
            if self.limits.max_bytes < 0 or self.limits.max_entries < 1:
                raise InspectionError("scratch budget has no request capacity")
            for _attempt in range(16):
                name = f".skillopt-txn-{secrets.token_hex(12)}"
                try:
                    os.mkdir(name, 0o700, dir_fd=self.root_descriptor)
                except FileExistsError:
                    continue
                self.name = name
                try:
                    self.descriptor = os.open(
                        name,
                        _DIRECTORY_FLAGS,
                        dir_fd=self.root_descriptor,
                    )
                except Exception:
                    os.rmdir(name, dir_fd=self.root_descriptor)
                    self.name = ""
                    raise
                self._transaction_identity = os.fstat(self.descriptor)
                self._token = _current_transaction.set(self)
                return self
            raise InspectionError("could not allocate scratch transaction")
        except Exception:
            try:
                if self.descriptor >= 0:
                    os.close(self.descriptor)
                    self.descriptor = -1
                if self.name and self.root_descriptor >= 0:
                    try:
                        os.rmdir(self.name, dir_fd=self.root_descriptor)
                    except FileNotFoundError:
                        pass
                    self.name = ""
            finally:
                if self.root_descriptor >= 0:
                    os.close(self.root_descriptor)
                    self.root_descriptor = -1
            raise

    def check(self) -> ScratchUsage:
        if self.descriptor < 0:
            raise InspectionError("scratch transaction is not active")
        return _scan_descriptor(self.descriptor, self.limits)

    def remaining_bytes(self) -> int:
        return self.limits.max_bytes - self.check().bytes

    def validate_root_identity(self) -> None:
        if self._root_identity is None:
            raise InspectionError("scratch transaction is not active")
        current = _open_real_directory(self.root_path, "scratch root")
        try:
            if not _same_file(
                self._root_identity,
                os.fstat(current),
            ):
                raise InspectionError(
                    "scratch root changed during artifact operation"
                )
        finally:
            os.close(current)

    def _output_parts(self, path: str) -> tuple[str, ...]:
        if not isinstance(path, str) or not path:
            raise InspectionError("renderer returned an invalid path")
        prefix = self.proc_path + "/"
        if path.startswith(prefix):
            return validate_logical_path(path[len(prefix):])
        if os.path.isabs(path):
            if os.path.normpath(path) != path:
                raise InspectionError("derived path must be normalized")
            raise InspectionError("derived path is outside scratch transaction")
        return validate_logical_path(path)

    def open_output(self, path: str) -> ScratchFile:
        parts = self._output_parts(path)
        parent = _open_relative_parent(self.descriptor, parts[:-1])
        try:
            descriptor = os.open(
                parts[-1],
                _FILE_FLAGS,
                dir_fd=parent,
            )
        except OSError as exc:
            raise InspectionError(
                f"rendered output could not be opened: {exc}"
            ) from exc
        finally:
            os.close(parent)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise InspectionError("rendered output must be a regular file")
        return ScratchFile("/".join(parts), descriptor)

    def open_outputs(self, paths: list[str]) -> list[ScratchFile]:
        if len(set(paths)) != len(paths):
            raise InspectionError("renderer returned duplicate paths")
        opened: list[ScratchFile] = []
        try:
            for path in paths:
                opened.append(self.open_output(path))
            return opened
        except Exception:
            for output in opened:
                output.close()
            raise

    def commit_outputs(self, paths: list[str]) -> list[str]:
        if self._committed_name:
            raise InspectionError("scratch transaction already committed outputs")
        self.validate_root_identity()
        self.check()
        if paths and self.requested_limits.max_depth < 2:
            raise InspectionError(
                "scratch depth budget is too small to commit rendered output"
            )
        outputs = self.open_outputs(paths)
        if not outputs:
            return []
        output_name = f".skillopt-output-{secrets.token_hex(12)}"
        output_descriptor = -1
        output_created = False
        try:
            os.mkdir(output_name, 0o700, dir_fd=self.root_descriptor)
            output_created = True
            output_descriptor = os.open(
                output_name,
                _DIRECTORY_FLAGS,
                dir_fd=self.root_descriptor,
            )
            committed: list[str] = []
            for index, output in enumerate(outputs):
                parts = PurePosixPath(output.logical_path).parts
                parent = _open_relative_parent(
                    self.descriptor,
                    parts[:-1],
                )
                try:
                    named = os.stat(
                        parts[-1],
                        dir_fd=parent,
                        follow_symlinks=False,
                    )
                    if not _same_file(named, os.fstat(output.descriptor)):
                        raise InspectionError(
                            "rendered output changed before commit"
                        )
                    destination = f"{index:03d}-{parts[-1]}"
                    os.rename(
                        parts[-1],
                        destination,
                        src_dir_fd=parent,
                        dst_dir_fd=output_descriptor,
                    )
                finally:
                    os.close(parent)
                committed.append(
                    os.path.join(
                        self.root_path,
                        output_name,
                        destination,
                    )
                )
            self.validate_root_identity()
            self._committed_name = output_name
            self._committed_identity = os.fstat(output_descriptor)
            return committed
        except Exception:
            if output_descriptor >= 0:
                _remove_tree(output_descriptor)
                os.close(output_descriptor)
                output_descriptor = -1
            if output_created:
                try:
                    os.rmdir(output_name, dir_fd=self.root_descriptor)
                except FileNotFoundError:
                    pass
            raise
        finally:
            if output_descriptor >= 0:
                os.close(output_descriptor)
            for output in outputs:
                output.close()

    def _rollback_committed(self) -> None:
        if not self._committed_name:
            return
        descriptor = os.open(
            self._committed_name,
            _DIRECTORY_FLAGS,
            dir_fd=self.root_descriptor,
        )
        try:
            if (
                self._committed_identity is None
                or not _same_file(
                    self._committed_identity,
                    os.fstat(descriptor),
                )
            ):
                raise InspectionError(
                    "committed scratch output changed during rollback"
                )
            _remove_tree(descriptor)
        finally:
            os.close(descriptor)
        current = os.stat(
            self._committed_name,
            dir_fd=self.root_descriptor,
            follow_symlinks=False,
        )
        if not _same_file(self._committed_identity, current):
            raise InspectionError(
                "committed scratch output changed during rollback"
            )
        os.rmdir(self._committed_name, dir_fd=self.root_descriptor)
        self._committed_name = ""
        self._committed_identity = None

    def _rollback(self) -> None:
        if self.descriptor < 0:
            return
        _remove_tree(self.descriptor)
        current = os.stat(
            self.name,
            dir_fd=self.root_descriptor,
            follow_symlinks=False,
        )
        if (
            self._transaction_identity is None
            or not _same_file(self._transaction_identity, current)
        ):
            raise InspectionError(
                "scratch transaction changed during rollback"
            )
        os.rmdir(self.name, dir_fd=self.root_descriptor)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._token is not None:
            _current_transaction.reset(self._token)
            self._token = None
        pending: BaseException | None = None
        if exc_type is None:
            try:
                self.check()
                self.validate_root_identity()
            except BaseException as check_error:
                pending = check_error
        try:
            try:
                self._rollback()
            except BaseException as cleanup_error:
                pending = pending or cleanup_error
            if exc_type is not None or pending is not None:
                try:
                    self._rollback_committed()
                except BaseException as cleanup_error:
                    pending = pending or cleanup_error
        finally:
            if self.descriptor >= 0:
                os.close(self.descriptor)
                self.descriptor = -1
            if self.root_descriptor >= 0:
                os.close(self.root_descriptor)
                self.root_descriptor = -1
        if pending is not None:
            raise pending
        return False


def current_scratch_transaction() -> ScratchTransaction | None:
    """Return the active transaction for safe_run supervision."""
    return _current_transaction.get()


def scratch_transaction(
    scratch_dir: str,
    *,
    max_bytes: int,
    max_entries: int,
    max_depth: int,
) -> ScratchTransaction:
    return ScratchTransaction(
        scratch_dir,
        max_bytes=max_bytes,
        max_entries=max_entries,
        max_depth=max_depth,
    )


def enforce_scratch_budget(
    scratch_dir: str,
    maximum: int,
) -> ScratchTransaction:
    """Compatibility wrapper for byte-only callers."""
    return scratch_transaction(
        scratch_dir,
        max_bytes=maximum,
        max_entries=100_000,
        max_depth=64,
    )
