"""Shared contracts and security helpers for artifact inspectors."""
from __future__ import annotations

import errno
import json
import math
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, TypeAlias, cast

MAX_RENDER_PIXELS = 500_000_000
DEFAULT_RESPONSE_BYTES = 1_000_000
# Leaves room for the smallest trusted CLI error or MCP envelope as valid JSON.
MIN_RESPONSE_BYTES = 512
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_EXTRACT_CHARS = 500_000
MAX_EXTRACT_CHARS = 4_000_000
DEFAULT_SCRATCH_BYTES = 512 * 1024 * 1024
MAX_SCRATCH_BYTES = 4 * 1024 * 1024 * 1024
MAX_COMMAND_OUTPUT_CHARS = 64_000
MAX_SELECTORS = 256
MAX_SELECTOR_CHARS = 256
MAX_COMMAND_TIMEOUT_SECONDS = 3_600
MAX_LOGICAL_PATH_CHARS = 4_096
MAX_LOGICAL_PATH_BYTES = 4_096
MAX_LOGICAL_COMPONENTS = 64
MAX_LOGICAL_COMPONENT_BYTES = 255

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class InspectionError(RuntimeError):
    """Raised when an artifact cannot be inspected within the trust contract."""


def _positive_int(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InspectionError(f"{name} budget must be a positive integer")
    if value > maximum:
        raise InspectionError(f"{name} budget exceeds maximum {maximum}")
    return value


@dataclass(frozen=True)
class RenderBudget:
    """Cumulative pixel budget for one render request."""

    max_pixels: int = MAX_RENDER_PIXELS
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES

    def __post_init__(self) -> None:
        _positive_int(self.max_pixels, "render pixel", MAX_RENDER_PIXELS)
        _positive_int(
            self.max_scratch_bytes,
            "scratch byte",
            MAX_SCRATCH_BYTES,
        )


@dataclass(frozen=True)
class ResponseBudget:
    """Bounds JSON serialization and extracted artifact-controlled text."""

    max_bytes: int = DEFAULT_RESPONSE_BYTES
    max_extract_chars: int = DEFAULT_EXTRACT_CHARS
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES

    def __post_init__(self) -> None:
        _positive_int(self.max_bytes, "response byte", MAX_RESPONSE_BYTES)
        if self.max_bytes < MIN_RESPONSE_BYTES:
            raise InspectionError(
                f"response byte budget must be at least {MIN_RESPONSE_BYTES}"
            )
        _positive_int(
            self.max_extract_chars,
            "extraction character",
            MAX_EXTRACT_CHARS,
        )
        _positive_int(
            self.max_scratch_bytes,
            "scratch byte",
            MAX_SCRATCH_BYTES,
        )


class Inspector(Protocol):
    """Contract implemented by format-specific inspectors."""

    def inspect(
        self,
        path: str,
        scratch_dir: str,
        *,
        response_budget: ResponseBudget,
    ) -> JSONValue: ...

    def render(
        self,
        path: str,
        scratch_dir: str,
        selectors: list[str],
        budget: RenderBudget,
    ) -> list[str]: ...

    def extract(
        self,
        path: str,
        scratch_dir: str,
        selectors: list[str],
        *,
        response_budget: ResponseBudget,
    ) -> JSONValue: ...


def bounded_diagnostic(value: object, max_chars: int = 1_000) -> str:
    """Return a single bounded diagnostic string."""
    text = str(value).replace("\x00", "\\0")
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}...[truncated {omitted} chars]"


def normalize_selectors(selectors: object | None) -> list[str]:
    """Validate and copy an inspector selector list."""
    if selectors is None:
        return []
    if not isinstance(selectors, list):
        raise InspectionError("selectors must be a list of strings")
    if len(selectors) > MAX_SELECTORS:
        raise InspectionError(f"selector count exceeds maximum {MAX_SELECTORS}")
    normalized: list[str] = []
    for selector in selectors:
        if (
            not isinstance(selector, str)
            or not selector
            or "\x00" in selector
            or len(selector) > MAX_SELECTOR_CHARS
        ):
            raise InspectionError(
                "each selector must be a non-empty bounded string without NUL"
            )
        normalized.append(selector)
    return normalized


def _validate_json_value(value: object) -> int:
    if value is None or isinstance(value, (str, bool)):
        return len(value) if isinstance(value, str) else 0
    if isinstance(value, int):
        return 0
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InspectionError("inspector result must contain finite numbers")
        return 0
    if isinstance(value, list):
        return sum(_validate_json_value(item) for item in value)
    if isinstance(value, dict):
        total = 0
        for key, child in value.items():
            if not isinstance(key, str):
                raise InspectionError(
                    "inspector result must use string object keys"
                )
            total += len(key) + _validate_json_value(child)
        return total
    raise InspectionError(
        f"inspector result is not JSON-compatible: {type(value).__name__}"
    )


def validate_json_result(
    value: object,
    budget: ResponseBudget,
    *,
    enforce_extract_chars: bool = False,
) -> JSONValue:
    """Validate recursive JSON types and serialized response size."""
    text_chars = _validate_json_value(value)
    if enforce_extract_chars and text_chars > budget.max_extract_chars:
        raise InspectionError(
            "extraction character budget exceeded: "
            f"{text_chars} > {budget.max_extract_chars}"
        )
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > budget.max_bytes:
        raise InspectionError(
            f"response byte budget exceeded: {len(encoded)} > {budget.max_bytes}"
        )
    return cast(JSONValue, value)


def _absolute_path(raw_path: str, label: str) -> str:
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or "\x00" in raw_path
    ):
        raise InspectionError(f"{label} must be a non-empty path without NUL")
    return os.path.abspath(raw_path)


def _open_real_directory(path: str, label: str) -> int:
    absolute = _absolute_path(path, label)
    try:
        descriptor = os.open(os.path.abspath(os.sep), _DIRECTORY_FLAGS)
    except OSError as exc:  # pragma: no cover - a broken POSIX runtime
        raise InspectionError(
            f"{label} root could not be opened: {bounded_diagnostic(exc)}"
        ) from exc
    try:
        for part in absolute.split(os.sep)[1:]:
            if not part:
                continue
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    detail = "contains a symlink or non-directory component"
                elif exc.errno == errno.ENOENT:
                    detail = "does not exist"
                else:
                    detail = f"could not be opened: {bounded_diagnostic(exc)}"
                raise InspectionError(f"{label} {detail}: {absolute}") from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def validate_roots(evidence_dir: str, scratch_dir: str) -> tuple[str, str]:
    """Validate real, disjoint evidence and scratch directory roots."""
    evidence = _absolute_path(evidence_dir, "evidence root")
    scratch = _absolute_path(scratch_dir, "scratch root")
    evidence_fd = _open_real_directory(evidence, "evidence root")
    scratch_fd = _open_real_directory(scratch, "scratch root")
    try:
        common = os.path.commonpath((evidence, scratch))
        if common in {evidence, scratch}:
            raise InspectionError(
                "evidence and scratch roots must not overlap"
            )
    finally:
        os.close(scratch_fd)
        os.close(evidence_fd)
    return evidence, scratch


def validate_logical_path(logical_path: object) -> tuple[str, ...]:
    """Validate a bounded normalized POSIX-relative artifact path."""
    if (
        not isinstance(logical_path, str)
        or not logical_path
        or "\x00" in logical_path
        or "\\" in logical_path
    ):
        raise InspectionError(
            "artifact path must be a non-empty logical POSIX relative path"
        )
    if len(logical_path) > MAX_LOGICAL_PATH_CHARS:
        raise InspectionError(
            "artifact path character length exceeds maximum "
            f"{MAX_LOGICAL_PATH_CHARS}"
        )
    try:
        encoded = logical_path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InspectionError("artifact path must be valid UTF-8") from exc
    if len(encoded) > MAX_LOGICAL_PATH_BYTES:
        raise InspectionError(
            "artifact path UTF-8 length exceeds maximum "
            f"{MAX_LOGICAL_PATH_BYTES} bytes"
        )
    logical = PurePosixPath(logical_path)
    parts = logical.parts
    if (
        logical.is_absolute()
        or not parts
        or logical.as_posix() != logical_path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise InspectionError(
            "artifact path must be a normalized logical POSIX relative path"
        )
    if len(parts) > MAX_LOGICAL_COMPONENTS:
        raise InspectionError(
            "artifact path component count exceeds maximum "
            f"{MAX_LOGICAL_COMPONENTS}"
        )
    for part in parts:
        if len(part.encode("utf-8")) > MAX_LOGICAL_COMPONENT_BYTES:
            raise InspectionError(
                "artifact path component UTF-8 length exceeds maximum "
                f"{MAX_LOGICAL_COMPONENT_BYTES} bytes"
            )
    return parts


def _open_child(
    parent_descriptor: int,
    name: str,
    *,
    directory: bool,
    label: str,
) -> int:
    flags = _DIRECTORY_FLAGS if directory else _FILE_FLAGS
    try:
        return os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            detail = "contains a symlink component"
        elif exc.errno == errno.ENOENT:
            detail = "does not exist"
        else:
            detail = f"could not be opened: {bounded_diagnostic(exc)}"
        raise InspectionError(f"{label} {detail}") from exc


def resolve_evidence_path(evidence_dir: str, logical_path: str) -> str:
    """Resolve a logical artifact path beneath evidence without symlinks."""
    evidence = _absolute_path(evidence_dir, "evidence root")
    parts = validate_logical_path(logical_path)
    descriptor = _open_real_directory(evidence, "evidence root")
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
        artifact_fd = _open_child(
            descriptor,
            parts[-1],
            directory=False,
            label=f"artifact path {logical_path!r}",
        )
        try:
            if not stat.S_ISREG(os.fstat(artifact_fd).st_mode):
                raise InspectionError(
                    f"artifact must be a regular file: {logical_path!r}"
                )
        finally:
            os.close(artifact_fd)
    finally:
        os.close(descriptor)
    return os.path.join(evidence, *parts)


def resolve_scratch_path(
    scratch_dir: str,
    derived_path: str,
    *,
    must_exist: bool = True,
) -> str:
    """Resolve and validate an inspector-derived path beneath scratch."""
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
    if os.path.isabs(derived_path):
        if os.path.normpath(derived_path) != derived_path:
            raise InspectionError("derived path must be normalized")
        candidate = os.path.abspath(derived_path)
        try:
            relative = os.path.relpath(candidate, scratch)
        except ValueError as exc:
            raise InspectionError("derived path is outside scratch") from exc
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            raise InspectionError("derived path is outside scratch")
        logical = relative.replace(os.sep, "/")
        parts = validate_logical_path(logical)
    else:
        parts = validate_logical_path(derived_path)
        candidate = os.path.join(scratch, *parts)

    descriptor = _open_real_directory(scratch, "scratch root")
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
        if not must_exist:
            try:
                info = os.stat(
                    parts[-1],
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return candidate
            if stat.S_ISLNK(info.st_mode):
                raise InspectionError("derived path contains a symlink")
            if not stat.S_ISREG(info.st_mode):
                raise InspectionError("derived path must be a regular file")
            return candidate
        output_fd = _open_child(
            descriptor,
            parts[-1],
            directory=False,
            label=f"derived path {derived_path!r}",
        )
        try:
            if not stat.S_ISREG(os.fstat(output_fd).st_mode):
                raise InspectionError("derived path must be a regular file")
        finally:
            os.close(output_fd)
    finally:
        os.close(descriptor)
    return candidate


def safe_run(
    command: list[str],
    *,
    timeout: int | float = 120,
    cwd: str,
    home: str,
) -> subprocess.CompletedProcess[str]:
    """Run a command through the focused process-isolation implementation."""
    from ._process import safe_run as _safe_run

    return _safe_run(command, timeout=timeout, cwd=cwd, home=home)


__all__ = [
    "DEFAULT_EXTRACT_CHARS",
    "DEFAULT_RESPONSE_BYTES",
    "DEFAULT_SCRATCH_BYTES",
    "InspectionError",
    "Inspector",
    "JSONValue",
    "MAX_COMMAND_OUTPUT_CHARS",
    "MAX_EXTRACT_CHARS",
    "MAX_LOGICAL_COMPONENTS",
    "MAX_LOGICAL_COMPONENT_BYTES",
    "MAX_LOGICAL_PATH_BYTES",
    "MAX_LOGICAL_PATH_CHARS",
    "MAX_RENDER_PIXELS",
    "MAX_RESPONSE_BYTES",
    "MAX_SCRATCH_BYTES",
    "MIN_RESPONSE_BYTES",
    "RenderBudget",
    "ResponseBudget",
    "bounded_diagnostic",
    "normalize_selectors",
    "resolve_evidence_path",
    "resolve_scratch_path",
    "safe_run",
    "validate_json_result",
    "validate_logical_path",
    "validate_roots",
]
