"""Skill discovery (four local agent sources + studio uploads) and zip upload.

A "skill" is any directory that directly contains a ``SKILL.md``.  Scan roots
are the configured sources plus ``studio_root/skills`` (source ``uploaded``).
Symlinked directories are resolved and included once — the first source that
reaches a physical directory wins, so a skill symlinked into two agent homes
appears a single time.
"""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path

import yaml

from skillopt_studio.config import StudioConfig
from skillopt_studio.models import SkillDetail, SkillInfo

UPLOAD_SOURCE = "uploaded"

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


def _build_skill_info(source: str, skill_dir: Path) -> SkillInfo:
    skill_md_path = skill_dir / "SKILL.md"
    try:
        skill_md_text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        skill_md_text = ""
    files = [p for p in skill_dir.rglob("*") if p.is_file()]
    return SkillInfo(
        id=f"{source}--{slugify(skill_dir.name)}",
        name=_parse_name(skill_md_text, skill_dir.name),
        source=source,
        path=str(skill_dir),
        description=_parse_description(skill_md_text),
        files_count=len(files),
        has_support_files=len(files) > 1,
    )


def _candidate_dirs(source: str, root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    candidates = [entry for entry in sorted(root.iterdir()) if entry.is_dir()]
    if source == "codex":
        system_layer = root / ".system"
        if system_layer.is_dir():
            candidates.extend(entry for entry in sorted(system_layer.iterdir()) if entry.is_dir())
    return candidates


def scan_skills(config: StudioConfig) -> list[SkillInfo]:
    """Discover skills across all configured sources plus studio uploads."""
    sources: dict[str, Path] = dict(config.skill_sources)
    sources[UPLOAD_SOURCE] = config.skills_dir

    skills: list[SkillInfo] = []
    seen_resolved: set[Path] = set()
    for source, root in sources.items():
        for entry in _candidate_dirs(source, root.expanduser()):
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved in seen_resolved or not (resolved / "SKILL.md").is_file():
                continue
            seen_resolved.add(resolved)
            skills.append(_build_skill_info(source, resolved))
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
    file_tree = sorted(str(p.relative_to(skill_dir)) for p in skill_dir.rglob("*") if p.is_file())
    return SkillDetail(**skill.model_dump(), skill_md=skill_md, file_tree=file_tree)


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
