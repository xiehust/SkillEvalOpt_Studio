"""Skill discovery (local agent sources + studio uploads) and zip upload.

A "skill" is any directory that directly contains a ``SKILL.md``.  Scan roots
are the configured sources plus ``studio_root/skills`` (source ``uploaded``).
Symlinked directories are resolved and included once — the first source that
reaches a physical directory wins, so a skill symlinked into two agent homes
appears a single time.

A source root holding an ``installed_plugins.json`` is a Claude Code plugins
root (default ``~/.claude/plugins``): instead of listing its subdirectories,
the manifest's installed plugins are read and each plugin's ``installPath`` is
searched for skills.  This deliberately mirrors what Claude Code itself loads —
uninstalled marketplace clones and stale cached versions stay hidden.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from pathlib import Path

import yaml

from skillopt_studio.config import StudioConfig
from skillopt_studio.models import SkillDetail, SkillFile, SkillInfo

UPLOAD_SOURCE = "uploaded"

# Built-in samples materialized by skillopt_studio.samples.  The sidecar holds
# display name/description so the sample's SKILL.md stays byte-identical to
# its repository source; it is hidden from listings and the file API.
SAMPLE_SOURCE = "sample"
SAMPLE_SIDECAR = ".studio_sample.json"

MAX_SKILL_FILE_BYTES = 512 * 1024

# Marks a source root as a Claude Code plugins root (~/.claude/plugins).
PLUGINS_MANIFEST = "installed_plugins.json"
PLUGIN_SKIP_DIRS = {"node_modules", "__pycache__"}
PLUGIN_SCAN_DEPTH = 4

# \w covers CJK and other unicode word chars — Chinese names are first-class here
_SLUG_RE = re.compile(r"[^\w.-]+", re.UNICODE)


def slugify(name: str) -> str:
    """Filesystem-safe slug; raises on names that reduce to nothing."""
    slug = _SLUG_RE.sub("-", name.strip()).strip("-.")
    if not slug or ".." in slug:
        raise ValueError(f"cannot derive a filesystem-safe slug from {name!r}")
    return slug


def _parse_description(skill_md_text: str) -> str:
    """Frontmatter ``description:`` if present, else the first non-heading line."""
    text = skill_md_text.lstrip("﻿")
    body = text
    if text.startswith("---"):
        parts = text.split("\n---", 2)
        if len(parts) >= 2:
            frontmatter_raw = parts[0].lstrip("-").lstrip("\n")
            body = parts[1]
            try:
                frontmatter = yaml.safe_load(frontmatter_raw)
            except yaml.YAMLError:
                frontmatter = None
            if isinstance(frontmatter, dict):
                description = frontmatter.get("description")
                if isinstance(description, str) and description.strip():
                    return description.strip()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped != "---":
            return stripped
    return ""


def _parse_name(skill_md_text: str, fallback: str) -> str:
    text = skill_md_text.lstrip("﻿")
    if text.startswith("---"):
        parts = text.split("\n---", 2)
        if len(parts) >= 2:
            try:
                frontmatter = yaml.safe_load(parts[0].lstrip("-").lstrip("\n"))
            except yaml.YAMLError:
                frontmatter = None
            if isinstance(frontmatter, dict):
                name = frontmatter.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    return fallback


def _read_sidecar(skill_dir: Path) -> dict:
    sidecar_path = skill_dir / SAMPLE_SIDECAR
    if not sidecar_path.is_file():
        return {}
    try:
        loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _build_skill_info(source: str, skill_dir: Path, slug_base: str | None = None) -> SkillInfo:
    skill_md_path = skill_dir / "SKILL.md"
    try:
        skill_md_text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        skill_md_text = ""
    files = [p for p in skill_dir.rglob("*") if p.is_file() and p.name != SAMPLE_SIDECAR]
    sidecar = _read_sidecar(skill_dir)
    name = sidecar.get("name") or _parse_name(skill_md_text, skill_dir.name)
    description = sidecar.get("description") or _parse_description(skill_md_text)
    return SkillInfo(
        id=f"{source}--{slugify(slug_base or skill_dir.name)}",
        name=str(name),
        source=source,
        path=str(skill_dir),
        description=str(description),
        files_count=len(files),
        has_support_files=len(files) > 1,
    )


def _topmost_skill_dirs(root: Path, depth: int = PLUGIN_SCAN_DEPTH) -> list[Path]:
    """Topmost dirs under root holding a SKILL.md; found skills are not descended into."""
    if (root / "SKILL.md").is_file():
        return [root]
    if depth <= 0:
        return []
    found: list[Path] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.is_dir() and not entry.name.startswith(".") and entry.name not in PLUGIN_SKIP_DIRS:
            found.extend(_topmost_skill_dirs(entry, depth - 1))
    return found


def _plugin_candidates(root: Path) -> list[tuple[Path, str | None]]:
    """(skill_dir, slug_base) for every skill of every installed Claude Code plugin.

    Reads ``installed_plugins.json`` (``{"plugins": {"name@marketplace": [{"scope",
    "installPath", ...}]}}``) rather than walking cache/marketplaces directories, so
    only what Claude Code actually has installed shows up.  User-scope installs are
    preferred when a plugin is installed at several scopes/versions (id dedup in
    scan_skills drops the rest).
    """
    try:
        manifest = json.loads((root / PLUGINS_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    plugins = manifest.get("plugins") if isinstance(manifest, dict) else None
    if not isinstance(plugins, dict):
        return []
    candidates: list[tuple[Path, str | None]] = []
    seen_installs: set[Path] = set()
    for key in sorted(plugins):
        installs = plugins[key]
        if isinstance(installs, dict):  # tolerate a single-entry (non-list) shape
            installs = [installs]
        if not isinstance(installs, list):
            continue
        plugin_name = key.partition("@")[0] or key
        entries = sorted(
            (e for e in installs if isinstance(e, dict)),
            key=lambda e: (e.get("scope") != "user", str(e.get("installPath", ""))),
        )
        for entry in entries:
            install_path = entry.get("installPath")
            if not isinstance(install_path, str) or not install_path:
                continue
            install_dir = Path(install_path).expanduser()
            if install_dir in seen_installs or not install_dir.is_dir():
                continue
            seen_installs.add(install_dir)
            for skill_dir in _topmost_skill_dirs(install_dir):
                candidates.append((skill_dir, f"{plugin_name}-{skill_dir.name}"))
    return candidates


def _candidate_dirs(source: str, root: Path) -> list[tuple[Path, str | None]]:
    """(dir, id slug base) pairs to probe for SKILL.md under one source root.

    A None slug base means "use the resolved directory name" (plain sources);
    plugin candidates carry an explicit ``<plugin>-<skill>`` base instead.
    """
    if (root / PLUGINS_MANIFEST).is_file():
        return _plugin_candidates(root)
    if not root.is_dir():
        return []
    candidates: list[tuple[Path, str | None]] = [
        (entry, None) for entry in sorted(root.iterdir()) if entry.is_dir()
    ]
    if source == "codex":
        system_layer = root / ".system"
        if system_layer.is_dir():
            candidates.extend((entry, None) for entry in sorted(system_layer.iterdir()) if entry.is_dir())
    return candidates


def scan_skills(config: StudioConfig) -> list[SkillInfo]:
    """Discover skills across all configured sources plus studio uploads."""
    sources: dict[str, Path] = {}
    if config.samples_enabled:
        # first so a symlink-shared dir dedups in favor of the sample source
        sources[SAMPLE_SOURCE] = config.samples_skills_dir
    sources.update(config.skill_sources)
    sources[UPLOAD_SOURCE] = config.skills_dir

    skills: list[SkillInfo] = []
    seen_resolved: set[Path] = set()
    seen_ids: set[str] = set()
    for source, root in sources.items():
        for entry, slug_base in _candidate_dirs(source, root.expanduser()):
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved in seen_resolved or not (resolved / "SKILL.md").is_file():
                continue
            info = _build_skill_info(source, resolved, slug_base)
            if info.id in seen_ids:  # same plugin at another scope/version, or a name clash
                continue
            seen_resolved.add(resolved)
            seen_ids.add(info.id)
            skills.append(info)
    return skills


def get_skill(config: StudioConfig, skill_id: str) -> SkillInfo | None:
    for skill in scan_skills(config):
        if skill.id == skill_id:
            return skill
    return None


def get_skill_detail(config: StudioConfig, skill_id: str) -> SkillDetail | None:
    skill = get_skill(config, skill_id)
    if skill is None:
        return None
    skill_dir = Path(skill.path)
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    file_tree = sorted(
        str(p.relative_to(skill_dir))
        for p in skill_dir.rglob("*")
        if p.is_file() and p.name != SAMPLE_SIDECAR
    )
    return SkillDetail(**skill.model_dump(), skill_md=skill_md, file_tree=file_tree)


def skill_file_path(config: StudioConfig, skill_id: str, rel_path: str) -> Path | None:
    """Resolved path of one file inside the skill directory; None if skill/file missing.

    Traversal (``..``, absolute, ``~``, empty) raises ValueError → API 400.
    """
    skill = get_skill(config, skill_id)
    if skill is None:
        return None
    rel = str(rel_path or "").strip()
    if not rel or rel.startswith(("/", "\\", "~")):
        raise ValueError(f"file path must be relative, got {rel!r}")
    root = Path(skill.path).resolve()
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"file path {rel!r} escapes the skill directory")
    if candidate.name == SAMPLE_SIDECAR:
        return None  # studio-internal metadata, never served
    if not candidate.is_file():
        return None
    return candidate


def read_skill_file(config: StudioConfig, skill_id: str, rel_path: str) -> SkillFile | None:
    """Content of one file inside the skill directory (see skill_file_path for errors).

    Binary files (or non-UTF-8) return metadata only, never bytes.
    """
    candidate = skill_file_path(config, skill_id, rel_path)
    if candidate is None:
        return None
    rel = str(rel_path).strip()
    size = candidate.stat().st_size
    with open(candidate, "rb") as f:
        data = f.read(MAX_SKILL_FILE_BYTES + 1)
    if b"\x00" in data:
        return SkillFile(path=rel, kind="binary", size=size)
    try:
        text = data[:MAX_SKILL_FILE_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        return SkillFile(path=rel, kind="binary", size=size)
    return SkillFile(path=rel, kind="text", size=size, truncated=size > MAX_SKILL_FILE_BYTES, content=text)


def _safe_members(archive: zipfile.ZipFile, target_dir: Path) -> list[zipfile.ZipInfo]:
    """Zip-slip guard: every member must land strictly inside target_dir."""
    target_resolved = target_dir.resolve()
    members = []
    for member in archive.infolist():
        name = member.filename
        if not name or name.startswith(("/", "\\")) or "\\" in name:
            raise ValueError(f"zip member {name!r} has an unsafe path")
        destination = (target_resolved / name).resolve()
        if destination != target_resolved and target_resolved not in destination.parents:
            raise ValueError(f"zip member {name!r} escapes the extraction directory")
        members.append(member)
    return members


def _skill_prefix(names: list[str]) -> str:
    """'' if SKILL.md sits at the archive root, else the unique top dir holding it."""
    if any(name == "SKILL.md" for name in names):
        return ""
    top_levels = {name.split("/", 1)[0] for name in names if name.strip("/")}
    if len(top_levels) == 1:
        top = next(iter(top_levels))
        if f"{top}/SKILL.md" in names:
            return f"{top}/"
    raise ValueError(
        "zip must contain SKILL.md at its root or inside a single top-level directory"
    )


def upload_skill_zip(config: StudioConfig, data: bytes, name: str) -> SkillInfo:
    """Extract an uploaded skill zip into studio_root/skills/<slug>/.

    Fail-fast: size cap, zip-slip guard and SKILL.md structural check all run
    before a single byte is written.  Re-uploading a slug replaces it.
    """
    if len(data) > config.max_skill_zip_bytes:
        raise ValueError(
            f"zip is {len(data)} bytes; limit is {config.max_skill_zip_bytes} bytes"
        )
    slug = slugify(name)
    target_dir = config.skills_dir / slug
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid zip archive: {exc}") from exc

    with archive:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        members = _safe_members(archive, target_dir)
        file_members = [m for m in members if not m.is_dir()]
        prefix = _skill_prefix([m.filename for m in file_members])

        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True)
        for member in file_members:
            relative = member.filename[len(prefix):]
            if not relative:
                continue
            destination = target_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)

    return _build_skill_info(UPLOAD_SOURCE, target_dir)
