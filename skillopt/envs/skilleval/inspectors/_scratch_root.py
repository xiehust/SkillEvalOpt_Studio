"""Locked scratch-root accounting, snapshots, and delta cleanup."""
from __future__ import annotations

import fcntl
import os
import stat
import threading
from dataclasses import dataclass

from .base import InspectionError, _open_real_directory

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}


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


EntryIdentity = tuple[int, ...]


@dataclass(frozen=True)
class ScratchSnapshot:
    usage: ScratchUsage
    entries: dict[tuple[str, ...], EntryIdentity]


@dataclass
class _RemovalFrame:
    descriptor: int
    names: list[str]
    parent_descriptor: int | None = None
    name: str = ""
    identity: os.stat_result | None = None
    owned: bool = False
    index: int = 0


@dataclass
class ScratchRootLease:
    path: str
    descriptor: int
    identity: os.stat_result
    limits: ScratchLimits
    baseline: ScratchSnapshot
    thread_lock: threading.Lock
    active: bool = True

    def release(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
            self.descriptor = -1
            self.thread_lock.release()


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


def _entry_identity(info: os.stat_result) -> EntryIdentity:
    common = (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
    )
    if stat.S_ISREG(info.st_mode):
        return (
            *common,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_nlink,
        )
    return common


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
    snapshot: dict[tuple[str, ...], EntryIdentity],
    *,
    depth: int = 0,
    parts: tuple[str, ...] = (),
) -> ScratchUsage:
    with os.scandir(descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    usage = ScratchUsage()
    for name in names:
        child_parts = (*parts, name)
        before = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        entry_depth = depth + 1
        child_usage = ScratchUsage(entries=1, depth=entry_depth)
        _raise_if_over(usage.plus(child_usage), limits)
        if stat.S_ISLNK(before.st_mode):
            raise InspectionError(
                f"scratch contains a symlink: {'/'.join(child_parts)}"
            )
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if not _same_object(before, opened):
                    raise InspectionError(
                        "scratch directory changed while scanning: "
                        f"{'/'.join(child_parts)}"
                    )
                snapshot[child_parts] = _entry_identity(opened)
                child_usage = child_usage.plus(
                    _scan_tree(
                        child,
                        limits,
                        snapshot,
                        depth=entry_depth,
                        parts=child_parts,
                    )
                )
            finally:
                os.close(child)
        elif stat.S_ISREG(before.st_mode):
            child = os.open(name, _FILE_FLAGS, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if not _same_object(before, opened):
                    raise InspectionError(
                        "scratch file changed while scanning: "
                        f"{'/'.join(child_parts)}"
                    )
                snapshot[child_parts] = _entry_identity(opened)
                child_usage = ScratchUsage(
                    bytes=opened.st_size,
                    entries=1,
                    depth=entry_depth,
                )
            finally:
                os.close(child)
        else:
            raise InspectionError(
                "scratch contains a non-regular entry: "
                f"{'/'.join(child_parts)}"
            )
        usage = usage.plus(child_usage)
        _raise_if_over(usage, limits)
    return usage


def _snapshot_descriptor(
    descriptor: int,
    limits: ScratchLimits,
) -> ScratchSnapshot:
    scan = os.open(".", _DIRECTORY_FLAGS, dir_fd=descriptor)
    entries: dict[tuple[str, ...], EntryIdentity] = {}
    try:
        usage = _scan_tree(scan, limits, entries)
    finally:
        os.close(scan)
    _raise_if_over(usage, limits)
    return ScratchSnapshot(usage, entries)


def _scan_descriptor(
    descriptor: int,
    limits: ScratchLimits,
) -> ScratchUsage:
    return _snapshot_descriptor(descriptor, limits).usage


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
                    if not _same_object(info, opened):
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
                    or not _same_object(frame.identity, current)
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


def _remove_named_entry(
    parent_descriptor: int,
    name: str,
    info: os.stat_result,
) -> None:
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        child = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        try:
            opened = os.fstat(child)
            if not _same_object(info, opened):
                raise InspectionError(
                    "scratch entry changed during rollback"
                )
            _remove_tree(child)
        finally:
            os.close(child)
        current = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_object(info, current):
            raise InspectionError(
                "scratch directory changed during rollback"
            )
        os.rmdir(name, dir_fd=parent_descriptor)
    else:
        os.unlink(name, dir_fd=parent_descriptor)


def cleanup_new_entries(
    descriptor: int,
    baseline: ScratchSnapshot,
    *,
    preserve_top_level: frozenset[str] = frozenset(),
) -> None:
    """Remove every current entry not owned by the locked baseline."""
    changed: list[str] = []

    def clean(
        directory: int,
        parts: tuple[str, ...] = (),
    ) -> None:
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            child_parts = (*parts, name)
            if len(child_parts) == 1 and name in preserve_top_level:
                continue
            info = os.stat(
                name,
                dir_fd=directory,
                follow_symlinks=False,
            )
            expected = baseline.entries.get(child_parts)
            if expected is None:
                _remove_named_entry(directory, name, info)
                continue
            current_identity = _entry_identity(info)
            same_object = current_identity[:3] == expected[:3]
            if not same_object:
                _remove_named_entry(directory, name, info)
                changed.append("/".join(child_parts))
                continue
            if current_identity != expected:
                changed.append("/".join(child_parts))
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name,
                    _DIRECTORY_FLAGS,
                    dir_fd=directory,
                )
                try:
                    opened = os.fstat(child)
                    if not _same_object(info, opened):
                        raise InspectionError(
                            "scratch entry changed during rollback"
                        )
                    clean(child, child_parts)
                finally:
                    os.close(child)

    clean(descriptor)
    if changed:
        raise InspectionError(
            "scratch baseline changed during artifact operation: "
            f"{changed[0]}"
        )


def verify_baseline(
    current: ScratchSnapshot,
    baseline: ScratchSnapshot,
) -> None:
    for path, expected in baseline.entries.items():
        if current.entries.get(path) != expected:
            raise InspectionError(
                "scratch baseline changed during artifact operation: "
                f"{'/'.join(path)}"
            )


def _thread_lock(path: str) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(path, threading.Lock())


def acquire_root_lease(
    path: str,
    limits: ScratchLimits,
) -> ScratchRootLease:
    descriptor = _open_real_directory(path, "scratch root")
    thread_lock = _thread_lock(path)
    thread_lock.acquire()
    locked = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        identity = os.fstat(descriptor)
        current = _open_real_directory(path, "scratch root")
        try:
            if not _same_object(identity, os.fstat(current)):
                raise InspectionError(
                    "scratch root changed before artifact operation"
                )
        finally:
            os.close(current)
        baseline = _snapshot_descriptor(descriptor, limits)
        return ScratchRootLease(
            path=path,
            descriptor=descriptor,
            identity=identity,
            limits=limits,
            baseline=baseline,
            thread_lock=thread_lock,
        )
    except BaseException:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            thread_lock.release()
        raise
