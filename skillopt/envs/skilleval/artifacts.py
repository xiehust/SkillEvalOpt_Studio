"""Artifact discovery and immutable evidence snapshots for SkillEval."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import zipfile
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

_SKIP_ROOTS = {".agents", ".claude", ".codex", ".git"}
_SKIP_ROOT_FILES = {"task.md", "codex_last_message.txt"}
_SUPPORTED_KINDS = {"xlsx", "xls", "docx", "doc", "pdf", "image", "pptx", "ppt"}
_SUFFIX_KINDS = {
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".docx": "docx",
    ".doc": "doc",
    ".pdf": "pdf",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".tif": "image",
    ".tiff": "image",
    ".pptx": "pptx",
    ".ppt": "ppt",
}
_SPECIFIC_MIME_KINDS = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/msexcel": "xls",
    "application/x-msexcel": "xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/mspowerpoint": "ppt",
    "application/x-mspowerpoint": "ppt",
    "application/pdf": "pdf",
    "application/x-pdf": "pdf",
    "image/png": "image",
    "image/jpeg": "image",
    "image/jpg": "image",
    "image/pjpeg": "image",
    "image/webp": "image",
    "image/tiff": "image",
    "image/x-tiff": "image",
}
_GENERIC_MIMES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "application/zip",
    "application/x-zip",
    "application/x-zip-compressed",
    "multipart/x-zip",
    "application/x-ole-storage",
    "application/vnd.ms-office",
    "application/cdfv2",
    "application/x-cdf",
    "application/x-empty",
    "inode/x-empty",
}
_ZIP_MIMES = {
    "application/zip",
    "application/x-zip",
    "application/x-zip-compressed",
    "multipart/x-zip",
}
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OOXML_MARKERS = {
    "xlsx": b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "docx": b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "pptx": b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}
_OOXML_PREFIXES = {"xlsx": "xl/", "docx": "word/", "pptx": "ppt/"}
_HASH_CHUNK_SIZE = 1024 * 1024
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str
    mime: str
    kind: str | None


@dataclass(frozen=True)
class EvidenceSnapshot:
    evidence_dir: str
    scratch_dir: str
    tree_hash: str
    files: tuple[ManifestEntry, ...]


class EvidenceLimitError(ValueError):
    """Raised when candidate evidence exceeds its configured byte limit."""


@dataclass
class _JudgeDirs:
    root_path: str
    evidence_path: str
    scratch_path: str
    root_name: str
    parent_descriptor: int
    root_descriptor: int
    evidence_descriptor: int
    scratch_descriptor: int

    def close(self) -> None:
        for descriptor in (
            self.scratch_descriptor,
            self.evidence_descriptor,
            self.root_descriptor,
            self.parent_descriptor,
        ):
            if descriptor >= 0:
                os.close(descriptor)


def _normalized_mime(mime: str | None) -> str:
    return (mime or "").split(";", 1)[0].strip().lower()


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _file_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _mime_from_descriptor(descriptor: int) -> str:
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            proc = subprocess.run(
                ["file", "--brief", "--mime-type", "-"],
                stdin=descriptor,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return _normalized_mime(proc.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return "application/octet-stream"
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


def _safe_zip_member(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts


def _inspect_ooxml_descriptor(descriptor: int) -> str | None:
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as source:
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                if len(members) > 10000:
                    return None
                names: set[str] = set()
                content_types_info = None
                for info in members:
                    if not _safe_zip_member(info.filename) or info.filename in names:
                        return None
                    names.add(info.filename)
                    if info.filename == "[Content_Types].xml":
                        content_types_info = info
                if content_types_info is None:
                    return None
                if (
                    content_types_info.flag_bits & 0x1
                    or content_types_info.file_size > 2 * 1024 * 1024
                ):
                    return None
                content_types = archive.read(content_types_info).lower()
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)

    matches = [
        kind
        for kind, marker in _OOXML_MARKERS.items()
        if marker in content_types
        and any(name.startswith(_OOXML_PREFIXES[kind]) for name in names)
    ]
    return matches[0] if len(matches) == 1 else None


def _read_descriptor(descriptor: int, size: int, offset: int = 0) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(descriptor, size, offset)
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, offset, os.SEEK_SET)
        return os.read(descriptor, size)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


def _detect_artifact_kind_from_descriptor(
    descriptor: int,
    logical_path: str,
    mime: str,
) -> str | None:
    detected_mime = _normalized_mime(mime)
    specific_kind = _SPECIFIC_MIME_KINDS.get(detected_mime)
    if specific_kind is not None:
        return specific_kind
    if detected_mime not in _GENERIC_MIMES:
        return None

    signature = _read_descriptor(descriptor, 16)
    if signature.startswith(b"%PDF-"):
        return "pdf"
    if (
        signature.startswith(b"\x89PNG\r\n\x1a\n")
        or signature.startswith(b"\xff\xd8\xff")
        or (signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")
        or signature.startswith((b"II*\x00", b"MM\x00*"))
    ):
        return "image"

    suffix = os.path.splitext(logical_path)[1].lower()
    is_zip = signature.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    if is_zip or detected_mime in _ZIP_MIMES:
        return _inspect_ooxml_descriptor(descriptor)
    if signature.startswith(_OLE_SIGNATURE):
        return _SUFFIX_KINDS.get(suffix) if suffix in {".xls", ".doc", ".ppt"} else None
    return _SUFFIX_KINDS.get(suffix)


def detect_artifact_kind(path: str, mime: str | None = None) -> str | None:
    """Return a supported normalized artifact kind, or ``None``.

    A specific MIME type is authoritative. Generic MIME types are resolved by
    safe signature/container inspection before a supported suffix is used.
    """
    detected_mime = _normalized_mime(mime)
    if mime is not None:
        specific_kind = _SPECIFIC_MIME_KINDS.get(detected_mime)
        if specific_kind is not None:
            return specific_kind
        if detected_mime not in _GENERIC_MIMES:
            return None

    descriptor = os.open(os.fspath(path), _file_flags())
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"non-regular artifact is not allowed: {path}")
        if mime is None:
            detected_mime = _mime_from_descriptor(descriptor)
        return _detect_artifact_kind_from_descriptor(
            descriptor,
            os.fspath(path),
            detected_mime,
        )
    finally:
        os.close(descriptor)


def _hash_descriptor(descriptor: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = _read_descriptor(descriptor, _HASH_CHUNK_SIZE, size)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _open_absolute_directory(path: str) -> int:
    path = os.path.abspath(path)
    current = os.open(os.path.abspath(os.sep), _directory_flags())
    try:
        for part in path.split(os.sep)[1:]:
            try:
                next_descriptor = os.open(part, _directory_flags(), dir_fd=current)
            except OSError as exc:
                raise ValueError(f"directory path must not contain symlinks: {path}") from exc
            os.close(current)
            current = next_descriptor
        return current
    except Exception:
        os.close(current)
        raise


def _manifest_entry_from_descriptor(
    descriptor: int,
    rel: str,
    *,
    require_single_link: bool,
) -> ManifestEntry:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"non-regular artifact is not allowed: {rel}")
    if require_single_link and before.st_nlink != 1:
        raise ValueError(f"artifact must be a single-link regular file: {rel}")
    size, sha256 = _hash_descriptor(descriptor)
    mime = _mime_from_descriptor(descriptor)
    kind = _detect_artifact_kind_from_descriptor(descriptor, rel, mime)
    after = os.fstat(descriptor)
    if (
        _stat_identity(before) != _stat_identity(after)
        or size != after.st_size
        or (require_single_link and after.st_nlink != 1)
    ):
        raise ValueError(f"artifact changed while manifest was built: {rel}")
    return ManifestEntry(path=rel, size=size, sha256=sha256, mime=mime, kind=kind)


def _scan_directory(
    directory_descriptor: int,
    rel_parts: tuple[str, ...],
    rows: dict[str, ManifestEntry],
    directories: set[str],
    *,
    skip_runtime: bool,
    require_single_link: bool,
    required_file_mode: int | None,
    required_directory_mode: int | None,
) -> None:
    with os.scandir(directory_descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        rel = "/".join((*rel_parts, name))
        info = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(
                f"symlink directory or file is not allowed in artifact workspace: {rel}"
            )
        if stat.S_ISDIR(info.st_mode):
            try:
                child_descriptor = os.open(name, _directory_flags(), dir_fd=directory_descriptor)
            except OSError as exc:
                raise ValueError(f"symlink directory is not allowed: {rel}") from exc
            try:
                opened = os.fstat(child_descriptor)
                if _stat_identity(info) != _stat_identity(opened):
                    raise ValueError(f"artifact directory changed while manifest was built: {rel}")
                if (
                    required_directory_mode is not None
                    and stat.S_IMODE(opened.st_mode) != required_directory_mode
                ):
                    raise ValueError(f"artifact directory has invalid mode: {rel}")
                if not (skip_runtime and name in _SKIP_ROOTS):
                    directories.add(rel)
                    _scan_directory(
                        child_descriptor,
                        (*rel_parts, name),
                        rows,
                        directories,
                        skip_runtime=skip_runtime,
                        require_single_link=require_single_link,
                        required_file_mode=required_file_mode,
                        required_directory_mode=required_directory_mode,
                    )
                current = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(current.st_mode) or not _same_filesystem_object(
                    opened,
                    current,
                ):
                    raise ValueError(
                        f"artifact directory changed while manifest was built: {rel}"
                    )
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"non-regular artifact is not allowed: {rel}")
        if skip_runtime and (
            name in _SKIP_ROOTS
            or (not rel_parts and name in _SKIP_ROOT_FILES)
        ):
            continue
        try:
            descriptor = os.open(name, _file_flags(), dir_fd=directory_descriptor)
        except OSError as exc:
            raise ValueError(f"symlink is not allowed in artifact workspace: {rel}") from exc
        try:
            opened = os.fstat(descriptor)
            if _stat_identity(info) != _stat_identity(opened):
                raise ValueError(f"artifact changed while manifest was built: {rel}")
            if (
                required_file_mode is not None
                and stat.S_IMODE(opened.st_mode) != required_file_mode
            ):
                raise ValueError(f"artifact file has invalid mode: {rel}")
            rows[rel] = _manifest_entry_from_descriptor(
                descriptor,
                rel,
                require_single_link=require_single_link,
            )
        finally:
            os.close(descriptor)


def _scan_manifest(
    root: str,
    *,
    skip_runtime: bool,
    require_single_link: bool,
    required_file_mode: int | None = None,
    required_directory_mode: int | None = None,
) -> tuple[dict[str, ManifestEntry], set[str]]:
    root = os.path.abspath(os.fspath(root))
    root_descriptor = _open_absolute_directory(root)
    try:
        return _scan_manifest_descriptor(
            root_descriptor,
            skip_runtime=skip_runtime,
            require_single_link=require_single_link,
            required_file_mode=required_file_mode,
            required_directory_mode=required_directory_mode,
        )
    finally:
        os.close(root_descriptor)


def _scan_manifest_descriptor(
    root_descriptor: int,
    *,
    skip_runtime: bool,
    require_single_link: bool,
    required_file_mode: int | None = None,
    required_directory_mode: int | None = None,
) -> tuple[dict[str, ManifestEntry], set[str]]:
    scan_descriptor = os.open(".", _directory_flags(), dir_fd=root_descriptor)
    rows: dict[str, ManifestEntry] = {}
    directories: set[str] = set()
    try:
        if (
            required_directory_mode is not None
            and stat.S_IMODE(os.fstat(scan_descriptor).st_mode) != required_directory_mode
        ):
            raise ValueError("artifact root directory has invalid mode")
        _scan_directory(
            scan_descriptor,
            (),
            rows,
            directories,
            skip_runtime=skip_runtime,
            require_single_link=require_single_link,
            required_file_mode=required_file_mode,
            required_directory_mode=required_directory_mode,
        )
    finally:
        os.close(scan_descriptor)
    return {path: rows[path] for path in sorted(rows)}, directories


def build_manifest(root: str) -> dict[str, ManifestEntry]:
    """Build a deterministic manifest of regular, non-runtime files in *root*."""
    rows, _directories = _scan_manifest(
        root,
        skip_runtime=True,
        require_single_link=True,
    )
    return rows


def diff_manifests(
    before: dict[str, ManifestEntry],
    after: dict[str, ManifestEntry],
) -> list[dict]:
    """Return sorted created and content-modified output rows."""
    rows: list[dict] = []
    for path in sorted(after):
        old = before.get(path)
        if old is None:
            change = "created"
        elif old.sha256 != after[path].sha256:
            change = "modified"
        else:
            continue
        rows.append({**asdict(after[path]), "change": change})
    return rows


def is_binary_output(row: dict) -> bool:
    """Whether a manifest row is a supported binary artifact."""
    return row.get("kind") in _SUPPORTED_KINDS


def _tree_hash(entries: list[ManifestEntry] | tuple[ManifestEntry, ...]) -> str:
    payload = json.dumps(
        [asdict(entry) for entry in sorted(entries, key=lambda entry: entry.path)],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _manifest_directories(entries: tuple[ManifestEntry, ...]) -> set[str]:
    directories: set[str] = set()
    for entry in entries:
        parts = PurePosixPath(entry.path).parts[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    return directories


def _logical_parts(raw_path: object) -> tuple[str, ...]:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path or "\\" in raw_path:
        raise ValueError(f"invalid relative artifact path: {raw_path!r}")
    logical = PurePosixPath(raw_path)
    parts = logical.parts
    if logical.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"invalid relative artifact path: {raw_path!r}")
    if logical.as_posix() != raw_path:
        raise ValueError(f"invalid relative artifact path: {raw_path!r}")
    if any(part in _SKIP_ROOTS for part in parts) or raw_path in _SKIP_ROOT_FILES:
        raise ValueError(f"runtime path is not candidate evidence: {raw_path!r}")
    return parts


def _ensure_real_directory(path: str) -> int:
    path = os.path.abspath(path)
    current = os.open(os.path.abspath(os.sep), _directory_flags())
    try:
        for part in path.split(os.sep)[1:]:
            if not part:
                continue
            try:
                next_descriptor = os.open(part, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                try:
                    next_descriptor = os.open(part, _directory_flags(), dir_fd=current)
                except OSError as exc:
                    raise ValueError(
                        f"destination parent must be a real directory: {path}"
                    ) from exc
            except OSError as exc:
                raise ValueError(
                    f"destination parent must be a real directory: {path}"
                ) from exc
            os.close(current)
            current = next_descriptor
        return current
    except Exception:
        os.close(current)
        raise


def _same_filesystem_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _remove_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    expected: os.stat_result | None = None,
) -> None:
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"refusing to remove non-directory entry: {name}")
    if expected is not None and not _same_filesystem_object(before, expected):
        raise ValueError(f"refusing to remove replaced directory: {name}")
    try:
        directory_descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as exc:
        raise ValueError(f"refusing to follow directory during cleanup: {name}") from exc
    try:
        opened = os.fstat(directory_descriptor)
        if not _same_filesystem_object(before, opened):
            raise ValueError(f"directory changed during cleanup: {name}")
        os.fchmod(directory_descriptor, 0o700)
        with os.scandir(directory_descriptor) as entries:
            child_names = sorted(entry.name for entry in entries)
        for child_name in child_names:
            child_info = os.stat(
                child_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISDIR(child_info.st_mode) and not stat.S_ISLNK(child_info.st_mode):
                _remove_directory_at(directory_descriptor, child_name)
            else:
                os.unlink(child_name, dir_fd=directory_descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or not _same_filesystem_object(opened, current):
            raise ValueError(f"directory changed during cleanup: {name}")
    finally:
        os.close(directory_descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)


def _open_created_directory(parent_descriptor: int, name: str, mode: int) -> int:
    os.mkdir(name, mode, dir_fd=parent_descriptor)
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(named.st_mode) or not _same_filesystem_object(named, opened):
        os.close(descriptor)
        raise ValueError(f"created directory changed unexpectedly: {name}")
    return descriptor


def _validate_absolute_directory_identity(path: str, expected_descriptor: int) -> None:
    current_descriptor = _open_absolute_directory(path)
    try:
        if not _same_filesystem_object(
            os.fstat(expected_descriptor),
            os.fstat(current_descriptor),
        ):
            raise ValueError(f"directory path changed after validation: {path}")
    finally:
        os.close(current_descriptor)


def _prepare_judge_dirs(judge_root: str) -> _JudgeDirs:
    judge_root = os.path.abspath(os.fspath(judge_root))
    parent = os.path.dirname(judge_root)
    root_name = os.path.basename(judge_root)
    if not root_name:
        raise ValueError("judge root must not be the filesystem root")
    parent_descriptor = _ensure_real_directory(parent)
    root_descriptor = -1
    evidence_descriptor = -1
    scratch_descriptor = -1
    created_root = False
    try:
        try:
            existing = os.stat(
                root_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise ValueError(f"judge root must not be a symlink: {judge_root}")
            if not stat.S_ISDIR(existing.st_mode):
                raise ValueError(f"judge root must be a directory: {judge_root}")
            _remove_directory_at(parent_descriptor, root_name)
        root_descriptor = _open_created_directory(parent_descriptor, root_name, 0o755)
        created_root = True
        evidence_descriptor = _open_created_directory(root_descriptor, "evidence", 0o700)
        scratch_descriptor = _open_created_directory(root_descriptor, "scratch", 0o700)
        _validate_absolute_directory_identity(parent, parent_descriptor)
        named_root = os.stat(
            root_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_filesystem_object(named_root, os.fstat(root_descriptor)):
            raise ValueError("judge root changed after creation")
    except Exception:
        expected_root = (
            os.fstat(root_descriptor)
            if created_root and root_descriptor >= 0
            else None
        )
        for descriptor in (scratch_descriptor, evidence_descriptor, root_descriptor):
            if descriptor >= 0:
                os.close(descriptor)
        if created_root:
            try:
                _remove_directory_at(
                    parent_descriptor,
                    root_name,
                    expected=expected_root,
                )
            except (FileNotFoundError, ValueError, OSError):
                pass
        os.close(parent_descriptor)
        raise
    evidence = os.path.join(judge_root, "evidence")
    scratch = os.path.join(judge_root, "scratch")
    return _JudgeDirs(
        root_path=judge_root,
        evidence_path=evidence,
        scratch_path=scratch,
        root_name=root_name,
        parent_descriptor=parent_descriptor,
        root_descriptor=root_descriptor,
        evidence_descriptor=evidence_descriptor,
        scratch_descriptor=scratch_descriptor,
    )


def _open_relative_file(root_descriptor: int, parts: tuple[str, ...]) -> int:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            try:
                next_descriptor = os.open(part, directory_flags, dir_fd=current)
            except OSError as exc:
                raise ValueError(
                    f"artifact parent must be a real directory: {'/'.join(parts)}"
                ) from exc
            os.close(current)
            current = next_descriptor
        try:
            return os.open(
                parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=current,
            )
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError(
                    f"artifact must be a regular file, not a symlink: {'/'.join(parts)}"
                ) from exc
            raise
    finally:
        os.close(current)


def _open_destination_file(root_descriptor: int, parts: tuple[str, ...]) -> int:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, directory_flags, dir_fd=current)
            os.close(current)
            current = next_descriptor
        return os.open(
            parts[-1],
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=current,
        )
    finally:
        os.close(current)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while copying evidence")
        view = view[written:]


def _copy_validated_file(
    source_descriptor: int,
    destination_descriptor: int,
    *,
    rel: str,
    expected_size: object,
    expected_sha256: object,
    initial_info: os.stat_result,
) -> int:
    if isinstance(expected_size, bool) or not isinstance(expected_size, int):
        raise ValueError(f"artifact manifest size is invalid: {rel}")
    if initial_info.st_size != expected_size:
        raise ValueError(
            f"artifact size mismatch for {rel}: expected {expected_size}, got {initial_info.st_size}"
        )
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError(f"artifact manifest hash is invalid: {rel}")

    source_digest = hashlib.sha256()
    copied = 0
    remaining = initial_info.st_size
    while remaining:
        chunk = os.read(source_descriptor, min(_HASH_CHUNK_SIZE, remaining))
        if not chunk:
            raise ValueError(f"artifact ended before its validated size: {rel}")
        source_digest.update(chunk)
        copied += len(chunk)
        remaining -= len(chunk)
        _write_all(destination_descriptor, chunk)
    if os.read(source_descriptor, 1):
        raise ValueError(f"artifact grew beyond its validated size: {rel}")

    final_info = os.fstat(source_descriptor)
    if (
        not stat.S_ISREG(final_info.st_mode)
        or final_info.st_nlink != 1
        or _stat_identity(final_info) != _stat_identity(initial_info)
        or copied != initial_info.st_size
    ):
        raise ValueError(f"artifact changed while evidence was copied: {rel}")
    source_hash = source_digest.hexdigest()
    if source_hash != expected_sha256:
        raise ValueError(
            f"artifact hash mismatch for {rel}: expected {expected_sha256}, got {source_hash}"
        )

    os.fsync(destination_descriptor)
    os.lseek(destination_descriptor, 0, os.SEEK_SET)
    copied_digest = hashlib.sha256()
    copied_size = 0
    while True:
        chunk = os.read(destination_descriptor, _HASH_CHUNK_SIZE)
        if not chunk:
            break
        copied_digest.update(chunk)
        copied_size += len(chunk)
    if copied_size != copied or copied_digest.hexdigest() != source_hash:
        raise RuntimeError(f"copied evidence verification failed: {rel}")
    os.fchmod(destination_descriptor, 0o444)
    destination_info = os.fstat(destination_descriptor)
    if not stat.S_ISREG(destination_info.st_mode) or destination_info.st_nlink != 1:
        raise ValueError(f"copied evidence must be a single-link regular file: {rel}")
    return copied


def _revalidate_source_entry(
    work_descriptor: int,
    source_descriptor: int,
    parts: tuple[str, ...],
    initial_info: os.stat_result,
    row: dict,
) -> ManifestEntry:
    rel = "/".join(parts)
    retained_info = os.fstat(source_descriptor)
    if (
        not stat.S_ISREG(retained_info.st_mode)
        or retained_info.st_nlink != 1
        or _stat_identity(retained_info) != _stat_identity(initial_info)
    ):
        raise ValueError(f"artifact source changed after evidence copy: {rel}")

    named_descriptor = _open_relative_file(work_descriptor, parts)
    try:
        named_info = os.fstat(named_descriptor)
        if _stat_identity(named_info) != _stat_identity(retained_info):
            raise ValueError(f"artifact source name changed after evidence copy: {rel}")
        entry = _manifest_entry_from_descriptor(
            named_descriptor,
            rel,
            require_single_link=True,
        )
        final_named_info = os.fstat(named_descriptor)
        if _stat_identity(final_named_info) != _stat_identity(retained_info):
            raise ValueError(f"artifact source changed after evidence copy: {rel}")
    finally:
        os.close(named_descriptor)

    confirmation_descriptor = _open_relative_file(work_descriptor, parts)
    try:
        if _stat_identity(os.fstat(confirmation_descriptor)) != _stat_identity(
            final_named_info
        ):
            raise ValueError(f"artifact source name changed after evidence copy: {rel}")
    finally:
        os.close(confirmation_descriptor)

    if entry.size != row.get("size") or entry.sha256 != row.get("sha256"):
        raise ValueError(f"artifact source content changed after evidence copy: {rel}")
    return entry


def _revalidate_destination_entry(
    evidence_descriptor: int,
    destination_descriptor: int,
    parts: tuple[str, ...],
    copied_info: os.stat_result,
    expected: ManifestEntry,
) -> ManifestEntry:
    rel = "/".join(parts)
    retained_info = os.fstat(destination_descriptor)
    if (
        not stat.S_ISREG(retained_info.st_mode)
        or retained_info.st_nlink != 1
        or not _same_filesystem_object(copied_info, retained_info)
        or retained_info.st_size != expected.size
        or stat.S_IMODE(retained_info.st_mode) != 0o444
    ):
        raise ValueError(f"copied evidence destination changed after locking: {rel}")
    retained_size, retained_hash = _hash_descriptor(destination_descriptor)
    if retained_size != expected.size or retained_hash != expected.sha256:
        raise ValueError(f"copied evidence destination content changed: {rel}")

    named_descriptor = _open_relative_file(evidence_descriptor, parts)
    try:
        named_info = os.fstat(named_descriptor)
        if (
            not stat.S_ISREG(named_info.st_mode)
            or named_info.st_nlink != 1
            or not _same_filesystem_object(copied_info, named_info)
            or named_info.st_size != expected.size
            or stat.S_IMODE(named_info.st_mode) != 0o444
        ):
            raise ValueError(f"copied evidence destination name changed: {rel}")
        entry = _manifest_entry_from_descriptor(
            named_descriptor,
            rel,
            require_single_link=True,
        )
        final_named_info = os.fstat(named_descriptor)
        if not _same_filesystem_object(copied_info, final_named_info):
            raise ValueError(f"copied evidence destination changed: {rel}")
    finally:
        os.close(named_descriptor)

    confirmation_descriptor = _open_relative_file(evidence_descriptor, parts)
    try:
        if not _same_filesystem_object(
            copied_info,
            os.fstat(confirmation_descriptor),
        ):
            raise ValueError(f"copied evidence destination name changed: {rel}")
    finally:
        os.close(confirmation_descriptor)

    if entry != expected:
        raise ValueError(f"copied evidence destination content changed: {rel}")
    return entry


def _lock_evidence_directories(evidence_descriptor: int) -> None:
    with os.scandir(evidence_descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        before = os.stat(name, dir_fd=evidence_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise RuntimeError(f"symlink appeared while locking evidence: {name}")
        if stat.S_ISDIR(before.st_mode):
            child_descriptor = os.open(name, _directory_flags(), dir_fd=evidence_descriptor)
            try:
                opened = os.fstat(child_descriptor)
                if not _same_filesystem_object(before, opened):
                    raise RuntimeError(f"evidence directory changed while locking: {name}")
                _lock_evidence_directories(child_descriptor)
                current = os.stat(
                    name,
                    dir_fd=evidence_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(current.st_mode) or not _same_filesystem_object(
                    opened,
                    current,
                ):
                    raise RuntimeError(f"evidence directory changed while locking: {name}")
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"non-regular evidence appeared while locking: {name}")
        file_descriptor = os.open(name, _file_flags(), dir_fd=evidence_descriptor)
        try:
            opened = os.fstat(file_descriptor)
            if (
                not _same_filesystem_object(before, opened)
                or opened.st_nlink != 1
            ):
                raise RuntimeError(f"evidence file changed while locking: {name}")
            os.fchmod(file_descriptor, 0o444)
        finally:
            os.close(file_descriptor)
    os.fchmod(evidence_descriptor, 0o555)


def create_evidence_snapshot(
    work_dir: str,
    outputs: list[dict],
    judge_root: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> EvidenceSnapshot:
    """Copy declared outputs into a byte-verified, read-only evidence tree."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    work_dir = os.path.abspath(os.fspath(work_dir))

    selected: list[tuple[dict, tuple[str, ...]]] = []
    seen: set[str] = set()
    for row in outputs:
        if row.get("change") not in {"created", "modified"}:
            continue
        parts = _logical_parts(row.get("path"))
        rel = "/".join(parts)
        if rel in seen:
            raise ValueError(f"duplicate artifact output path: {rel}")
        seen.add(rel)
        selected.append((row, parts))
    selected.sort(key=lambda item: "/".join(item[1]))

    work_descriptor = _open_absolute_directory(work_dir)
    prepared: _JudgeDirs | None = None
    validated_sources: list[tuple[dict, tuple[str, ...], int, os.stat_result]] = []
    copied_destinations: list[tuple[tuple[str, ...], int, os.stat_result]] = []
    try:
        prepared = _prepare_judge_dirs(judge_root)
        evidence = prepared.evidence_path
        scratch = prepared.scratch_path
        total_bytes = 0
        for row, parts in selected:
            rel = "/".join(parts)
            source_descriptor = _open_relative_file(work_descriptor, parts)
            info = os.fstat(source_descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                os.close(source_descriptor)
                raise ValueError(f"artifact must be a single-link regular file: {rel}")
            validated_sources.append((row, parts, source_descriptor, info))
            total_bytes += info.st_size
            if total_bytes > max_bytes:
                raise EvidenceLimitError(
                    f"candidate output bytes exceed configured limit {max_bytes}"
                )

        copied_total = 0
        for row, parts, source_descriptor, info in validated_sources:
            rel = "/".join(parts)
            if info.st_size > max_bytes - copied_total:
                raise EvidenceLimitError(
                    f"candidate output bytes exceed configured limit {max_bytes}"
                )
            destination_descriptor = _open_destination_file(
                prepared.evidence_descriptor,
                parts,
            )
            try:
                copied = _copy_validated_file(
                    source_descriptor,
                    destination_descriptor,
                    rel=rel,
                    expected_size=row.get("size"),
                    expected_sha256=row.get("sha256"),
                    initial_info=info,
                )
                copied_total += copied
                copied_destinations.append(
                    (parts, destination_descriptor, os.fstat(destination_descriptor))
                )
            except Exception:
                os.close(destination_descriptor)
                raise

        evidence_rows, evidence_directories = _scan_manifest_descriptor(
            prepared.evidence_descriptor,
            skip_runtime=False,
            require_single_link=True,
        )
        entries = tuple(evidence_rows.values())
        if evidence_directories != _manifest_directories(entries):
            raise RuntimeError("unexpected directory in copied evidence")
        manifest_descriptor = os.open(
            "artifact-manifest.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=prepared.scratch_descriptor,
        )
        with os.fdopen(manifest_descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                [asdict(entry) for entry in entries],
                handle,
                ensure_ascii=True,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
        _lock_evidence_directories(prepared.evidence_descriptor)
        final_rows, final_directories = _scan_manifest_descriptor(
            prepared.evidence_descriptor,
            skip_runtime=False,
            require_single_link=True,
            required_file_mode=0o444,
            required_directory_mode=0o555,
        )
        final_entries = tuple(final_rows.values())
        expected_entries = tuple(
            _revalidate_source_entry(
                work_descriptor,
                source_descriptor,
                parts,
                info,
                row,
            )
            for row, parts, source_descriptor, info in validated_sources
        )
        if len(copied_destinations) != len(expected_entries):
            raise RuntimeError("copied evidence destination set is incomplete")
        destination_entries = tuple(
            _revalidate_destination_entry(
                prepared.evidence_descriptor,
                destination_descriptor,
                parts,
                copied_info,
                expected_entries[index],
            )
            for index, (parts, destination_descriptor, copied_info) in enumerate(
                copied_destinations
            )
        )
        expected_directories = _manifest_directories(expected_entries)
        if (
            entries != expected_entries
            or final_entries != expected_entries
            or destination_entries != expected_entries
            or evidence_directories != expected_directories
            or final_directories != expected_directories
        ):
            raise RuntimeError("evidence does not match finalized rollout outputs")
        _validate_absolute_directory_identity(
            prepared.root_path,
            prepared.root_descriptor,
        )
        _validate_absolute_directory_identity(
            prepared.evidence_path,
            prepared.evidence_descriptor,
        )
        _validate_absolute_directory_identity(
            prepared.scratch_path,
            prepared.scratch_descriptor,
        )
        return EvidenceSnapshot(
            evidence_dir=evidence,
            scratch_dir=scratch,
            tree_hash=_tree_hash(final_entries),
            files=final_entries,
        )
    except Exception:
        if prepared is not None:
            try:
                _remove_directory_at(
                    prepared.parent_descriptor,
                    prepared.root_name,
                    expected=os.fstat(prepared.root_descriptor),
                )
            except (FileNotFoundError, ValueError, OSError):
                pass
        raise
    finally:
        for _parts, destination_descriptor, _info in copied_destinations:
            os.close(destination_descriptor)
        for _row, _parts, source_descriptor, _info in validated_sources:
            os.close(source_descriptor)
        if work_descriptor >= 0:
            os.close(work_descriptor)
        if prepared is not None:
            prepared.close()


def verify_evidence_snapshot(snapshot: EvidenceSnapshot) -> None:
    """Raise if an evidence snapshot's path set or bytes have changed."""
    try:
        current_rows, current_directories = _scan_manifest(
            snapshot.evidence_dir,
            skip_runtime=False,
            require_single_link=True,
            required_file_mode=0o444,
            required_directory_mode=0o555,
        )
        current = tuple(current_rows.values())
    except Exception as exc:
        raise RuntimeError("evidence changed while judge was running") from exc
    if (
        current != snapshot.files
        or current_directories != _manifest_directories(snapshot.files)
        or _tree_hash(current) != snapshot.tree_hash
    ):
        raise RuntimeError("evidence changed while judge was running")
