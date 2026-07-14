"""Request-local scratch transactions over an exclusively locked root."""
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
from ._scratch_root import (
    ScratchLimits,
    ScratchRootLease,
    ScratchSnapshot,
    ScratchUsage,
    _remove_tree,
    _same_object,
    _scan_descriptor,
    _snapshot_descriptor,
    acquire_root_lease,
    cleanup_new_entries,
    verify_baseline,
)

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_current_transaction: contextvars.ContextVar[ScratchTransaction | None] = (
    contextvars.ContextVar("artifact_scratch_transaction", default=None)
)


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
    """One request holding exclusive ownership of the configured scratch root."""

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
        self._lease: ScratchRootLease | None = None
        self._baseline: ScratchSnapshot | None = None
        self._committed_name = ""
        self._token: contextvars.Token[ScratchTransaction | None] | None = None

    @property
    def proc_path(self) -> str:
        if self.descriptor < 0:
            raise InspectionError("scratch transaction is not active")
        return f"/proc/self/fd/{self.descriptor}"

    def __enter__(self) -> ScratchTransaction:
        try:
            self._lease = acquire_root_lease(
                self.root_path,
                self.requested_limits,
            )
            self.root_descriptor = self._lease.descriptor
            self._baseline = self._lease.baseline
            baseline = self._baseline.usage
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
                self.check()
                self._token = _current_transaction.set(self)
                return self
            raise InspectionError("could not allocate scratch transaction")
        except BaseException:
            self._close_failed_entry()
            raise

    def _close_failed_entry(self) -> None:
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
            if self._lease is not None:
                self._lease.release()
                self._lease = None
            self.root_descriptor = -1

    def check(self) -> ScratchUsage:
        if self.root_descriptor < 0:
            raise InspectionError("scratch transaction is not active")
        return _scan_descriptor(
            self.root_descriptor,
            self.requested_limits,
        )

    def remaining_bytes(self) -> int:
        return self.requested_limits.max_bytes - self.check().bytes

    def validate_root_identity(self) -> None:
        if self._lease is None:
            raise InspectionError("scratch transaction is not active")
        current = _open_real_directory(self.root_path, "scratch root")
        try:
            if not _same_object(
                self._lease.identity,
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
                    if not _same_object(named, os.fstat(output.descriptor)):
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
            self.check()
            self.validate_root_identity()
            self._committed_name = output_name
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

    def _restore_root(self, preserve: frozenset[str]) -> None:
        if self._baseline is None:
            raise InspectionError("scratch transaction has no baseline")
        cleanup_new_entries(
            self.root_descriptor,
            self._baseline,
            preserve_top_level=preserve,
        )
        current = _snapshot_descriptor(
            self.root_descriptor,
            self.requested_limits,
        )
        verify_baseline(current, self._baseline)

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
        preserve = (
            frozenset({self._committed_name})
            if exc_type is None and pending is None and self._committed_name
            else frozenset()
        )
        try:
            try:
                self._restore_root(preserve)
            except BaseException as cleanup_error:
                pending = pending or cleanup_error
                if preserve:
                    try:
                        self._restore_root(frozenset())
                    except BaseException as rollback_error:
                        pending = pending or rollback_error
        finally:
            if self.descriptor >= 0:
                os.close(self.descriptor)
                self.descriptor = -1
            if self._lease is not None:
                self._lease.release()
                self._lease = None
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
