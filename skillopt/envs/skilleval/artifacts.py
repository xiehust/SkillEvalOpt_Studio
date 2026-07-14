"""Artifact discovery and immutable evidence snapshots for SkillEval."""
from __future__ import annotations

import errno
import hashlib
import json
import mimetypes
import os
import shutil
import stat
import subprocess
import zipfile
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

_SKIP_ROOTS = {".agents", ".claude", ".codex", ".git"}
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


def _normalized_mime(mime: str | None) -> str:
    return (mime or "").split(";", 1)[0].strip().lower()


def _mime(path: str) -> str:
    try:
        proc = subprocess.run(
            ["file", "--brief", "--mime-type", "--", path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return _normalized_mime(proc.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return mimetypes.guess_type(path, strict=False)[0] or "application/octet-stream"


def _safe_zip_member(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts


def _inspect_ooxml(path: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
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
            if content_types_info.flag_bits & 0x1 or content_types_info.file_size > 2 * 1024 * 1024:
                return None
            content_types = archive.read(content_types_info).lower()
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None

    matches = [
        kind
        for kind, marker in _OOXML_MARKERS.items()
        if marker in content_types
        and any(name.startswith(_OOXML_PREFIXES[kind]) for name in names)
    ]
    return matches[0] if len(matches) == 1 else None


def _read_signature(path: str) -> bytes:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            return os.read(descriptor, 16)
        finally:
            os.close(descriptor)
    except OSError:
        return b""


def detect_artifact_kind(path: str, mime: str | None = None) -> str | None:
    """Return a supported normalized artifact kind, or ``None``.

    A specific MIME type is authoritative. Generic MIME types are resolved by
    safe signature/container inspection before a supported suffix is used.
    """
    detected_mime = _normalized_mime(_mime(path) if mime is None else mime)
    specific_kind = _SPECIFIC_MIME_KINDS.get(detected_mime)
    if specific_kind is not None:
        return specific_kind
    if detected_mime not in _GENERIC_MIMES:
        return None

    signature = _read_signature(path)
    if signature.startswith(b"%PDF-"):
        return "pdf"
    if (
        signature.startswith(b"\x89PNG\r\n\x1a\n")
        or signature.startswith(b"\xff\xd8\xff")
        or (signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")
        or signature.startswith((b"II*\x00", b"MM\x00*"))
    ):
        return "image"

    suffix = os.path.splitext(os.fspath(path))[1].lower()
    is_zip = signature.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    if is_zip or detected_mime in _ZIP_MIMES:
        return _inspect_ooxml(path)
    if signature.startswith(_OLE_SIGNATURE):
        return _SUFFIX_KINDS.get(suffix) if suffix in {".xls", ".doc", ".ppt"} else None
    return _SUFFIX_KINDS.get(suffix)


def _hash_regular_file(path: str) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"non-regular artifact is not allowed: {path}")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, _HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or size != after.st_size:
            raise ValueError(f"artifact changed while manifest was built: {path}")
        return size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _relative_posix(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def build_manifest(root: str) -> dict[str, ManifestEntry]:
    """Build a deterministic manifest of regular, non-runtime files in *root*."""
    root = os.path.abspath(os.fspath(root))
    root_info = os.lstat(root)
    if stat.S_ISLNK(root_info.st_mode):
        raise ValueError(f"artifact workspace root must not be a symlink: {root}")
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"artifact workspace root is not a directory: {root}")

    rows: dict[str, ManifestEntry] = {}
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        safe_dirs: list[str] = []
        for name in sorted(dirs):
            full = os.path.join(current, name)
            info = os.lstat(full)
            rel = _relative_posix(full, root)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"symlink directory is not allowed: {rel}")
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"non-directory artifact entry is not allowed: {rel}")
            if name not in _SKIP_ROOTS:
                safe_dirs.append(name)
        dirs[:] = safe_dirs

        for name in sorted(files):
            full = os.path.join(current, name)
            rel = _relative_posix(full, root)
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"symlink is not allowed in artifact workspace: {rel}")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"non-regular artifact is not allowed: {rel}")
            if rel == "task.md":
                continue
            size, sha256 = _hash_regular_file(full)
            mime = _mime(full)
            rows[rel] = ManifestEntry(
                path=rel,
                size=size,
                sha256=sha256,
                mime=mime,
                kind=detect_artifact_kind(full, mime),
            )
    return {path: rows[path] for path in sorted(rows)}


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


def _logical_parts(raw_path: object) -> tuple[str, ...]:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path or "\\" in raw_path:
        raise ValueError(f"invalid relative artifact path: {raw_path!r}")
    logical = PurePosixPath(raw_path)
    parts = logical.parts
    if logical.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"invalid relative artifact path: {raw_path!r}")
    if logical.as_posix() != raw_path:
        raise ValueError(f"invalid relative artifact path: {raw_path!r}")
    if any(part in _SKIP_ROOTS for part in parts) or raw_path == "task.md":
        raise ValueError(f"runtime path is not candidate evidence: {raw_path!r}")
    return parts


def _ensure_real_directory(path: str) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current = os.open(os.path.abspath(os.sep), directory_flags)
    try:
        for part in os.path.abspath(path).split(os.sep)[1:]:
            try:
                next_descriptor = os.open(part, directory_flags, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                try:
                    next_descriptor = os.open(part, directory_flags, dir_fd=current)
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
    finally:
        os.close(current)


def _make_tree_removable(root: str) -> None:
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_info = os.lstat(current)
        if stat.S_ISDIR(current_info.st_mode):
            os.chmod(current, 0o700)
        safe_dirs: list[str] = []
        for name in dirs:
            full = os.path.join(current, name)
            info = os.lstat(full)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in files:
            full = os.path.join(current, name)
            info = os.lstat(full)
            if stat.S_ISREG(info.st_mode):
                os.chmod(full, 0o600)


def _prepare_judge_dirs(judge_root: str) -> tuple[str, str, str]:
    judge_root = os.path.abspath(os.fspath(judge_root))
    parent = os.path.dirname(judge_root)
    _ensure_real_directory(parent)
    if os.path.lexists(judge_root):
        info = os.lstat(judge_root)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"judge root must not be a symlink: {judge_root}")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"judge root must be a directory: {judge_root}")
        _make_tree_removable(judge_root)
        shutil.rmtree(judge_root)
    os.mkdir(judge_root, 0o755)
    evidence = os.path.join(judge_root, "evidence")
    scratch = os.path.join(judge_root, "scratch")
    os.mkdir(evidence, 0o700)
    os.mkdir(scratch, 0o700)
    return judge_root, evidence, scratch


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
) -> None:
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
    while True:
        chunk = os.read(source_descriptor, _HASH_CHUNK_SIZE)
        if not chunk:
            break
        source_digest.update(chunk)
        copied += len(chunk)
        _write_all(destination_descriptor, chunk)

    final_info = os.fstat(source_descriptor)
    initial_identity = (
        initial_info.st_dev,
        initial_info.st_ino,
        initial_info.st_size,
        initial_info.st_mtime_ns,
    )
    final_identity = (
        final_info.st_dev,
        final_info.st_ino,
        final_info.st_size,
        final_info.st_mtime_ns,
    )
    if final_identity != initial_identity or copied != initial_info.st_size:
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


def _lock_evidence_directories(evidence: str) -> None:
    for current, dirs, _files in os.walk(evidence, topdown=False, followlinks=False):
        for name in dirs:
            full = os.path.join(current, name)
            info = os.lstat(full)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f"invalid evidence directory after copy: {full}")
            os.chmod(full, 0o555)
        os.chmod(current, 0o555)


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
    if os.path.realpath(work_dir) != work_dir:
        raise ValueError(f"artifact workspace path contains a symlink: {work_dir}")

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

    prepared_root, evidence, scratch = _prepare_judge_dirs(judge_root)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    work_descriptor = -1
    evidence_descriptor = -1
    validated_sources: list[tuple[dict, tuple[str, ...], int, os.stat_result]] = []
    try:
        work_descriptor = os.open(work_dir, directory_flags)
        evidence_descriptor = os.open(evidence, directory_flags)
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

        for row, parts, source_descriptor, info in validated_sources:
            rel = "/".join(parts)
            destination_descriptor = _open_destination_file(evidence_descriptor, parts)
            try:
                _copy_validated_file(
                    source_descriptor,
                    destination_descriptor,
                    rel=rel,
                    expected_size=row.get("size"),
                    expected_sha256=row.get("sha256"),
                    initial_info=info,
                )
            finally:
                os.close(destination_descriptor)

        entries = tuple(build_manifest(evidence).values())
        manifest_path = os.path.join(scratch, "artifact-manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(
                [asdict(entry) for entry in entries],
                handle,
                ensure_ascii=True,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
        _lock_evidence_directories(evidence)
        return EvidenceSnapshot(
            evidence_dir=evidence,
            scratch_dir=scratch,
            tree_hash=_tree_hash(entries),
            files=entries,
        )
    except Exception:
        if os.path.isdir(prepared_root):
            _make_tree_removable(prepared_root)
            shutil.rmtree(prepared_root)
        raise
    finally:
        for _row, _parts, source_descriptor, _info in validated_sources:
            os.close(source_descriptor)
        if evidence_descriptor >= 0:
            os.close(evidence_descriptor)
        if work_descriptor >= 0:
            os.close(work_descriptor)


def verify_evidence_snapshot(snapshot: EvidenceSnapshot) -> None:
    """Raise if an evidence snapshot's path set or bytes have changed."""
    try:
        current = tuple(build_manifest(snapshot.evidence_dir).values())
    except Exception as exc:
        raise RuntimeError("evidence changed while judge was running") from exc
    if current != snapshot.files or _tree_hash(current) != snapshot.tree_hash:
        raise RuntimeError("evidence changed while judge was running")
