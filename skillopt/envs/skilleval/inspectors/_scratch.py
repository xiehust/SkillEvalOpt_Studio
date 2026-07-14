"""Scratch-tree byte accounting for inspector operations."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .base import InspectionError, _open_real_directory

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _tree_bytes(descriptor: int, maximum: int, logical: str = "") -> int:
    with os.scandir(descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    total = 0
    for name in names:
        path = f"{logical}/{name}" if logical else name
        before = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
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
                total += _tree_bytes(child, maximum - total, path)
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
                total += opened.st_size
            finally:
                os.close(child)
        else:
            raise InspectionError(
                f"scratch contains a non-regular entry: {path}"
            )
        if total > maximum:
            raise InspectionError(
                f"scratch byte budget exceeded: {total} > {maximum}"
            )
    return total


def scratch_bytes(scratch_dir: str, maximum: int) -> int:
    """Return bounded total regular-file bytes under scratch."""
    descriptor = _open_real_directory(scratch_dir, "scratch root")
    try:
        return _tree_bytes(descriptor, maximum)
    finally:
        os.close(descriptor)


@dataclass
class ScratchGuard:
    """Stable scratch root identity and byte accounting for one operation."""

    path: str
    descriptor: int
    maximum: int

    def _current_bytes(self) -> int:
        scan = os.open(".", _DIRECTORY_FLAGS, dir_fd=self.descriptor)
        try:
            return _tree_bytes(scan, self.maximum)
        finally:
            os.close(scan)

    def check(self) -> int:
        return self._current_bytes()

    def remaining_bytes(self) -> int:
        return self.maximum - self._current_bytes()

    def validate_root_identity(self) -> None:
        current = _open_real_directory(self.path, "scratch root")
        try:
            if not _same_file(
                os.fstat(self.descriptor),
                os.fstat(current),
            ):
                raise InspectionError(
                    "scratch root changed during artifact operation"
                )
        finally:
            os.close(current)


@contextmanager
def enforce_scratch_budget(
    scratch_dir: str,
    maximum: int,
) -> Iterator[ScratchGuard]:
    """Fail before or after an operation if scratch exceeds *maximum*."""
    descriptor = _open_real_directory(scratch_dir, "scratch root")
    guard = ScratchGuard(scratch_dir, descriptor, maximum)
    try:
        guard.check()
        try:
            yield guard
        finally:
            guard.validate_root_identity()
            guard.check()
    finally:
        os.close(descriptor)
