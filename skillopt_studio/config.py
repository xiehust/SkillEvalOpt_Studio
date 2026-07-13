"""Studio runtime configuration.

Everything path-like is overridable both programmatically (tests build a
StudioConfig pointing at tmp_path) and via environment variables (so a shell
can relocate the data root without touching code):

- ``SKILLOPT_STUDIO_ROOT`` — where uploads, tasksets and job records live.
- ``SKILLOPT_STUDIO_SKILL_SOURCES`` — comma-separated ``name=path`` pairs that
  replace the default four scan sources entirely.
- ``SKILLOPT_STUDIO_SAMPLES`` — built-in sample skills/tasksets; on by default
  for real servers (``from_env``), off for directly constructed configs so
  tests are never polluted.  ``0``/``false``/``off``/``no`` disables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_STUDIO_ROOT = "outputs/studio"

MAX_SKILL_ZIP_BYTES = 50 * 1024 * 1024


def _default_skill_sources() -> dict[str, Path]:
    home = Path.home()
    return {
        "claude": home / ".claude" / "skills",
        # Claude Code plugin installs; discovered via installed_plugins.json,
        # not a directory walk (see skill_sources._plugin_candidates).
        "claude-plugins": home / ".claude" / "plugins",
        "codex": home / ".codex" / "skills",
        "kiro": home / ".kiro" / "skills",
        "agents": home / ".agents" / "skills",
    }


def _parse_sources_env(raw: str) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(
                f"SKILLOPT_STUDIO_SKILL_SOURCES entry {pair!r} must be 'name=path'"
            )
        name, _, path = pair.partition("=")
        sources[name.strip()] = Path(path.strip()).expanduser()
    return sources


@dataclass
class StudioConfig:
    """All knobs the studio backend needs; safe to construct in tests."""

    studio_root: Path = field(default_factory=lambda: Path(DEFAULT_STUDIO_ROOT))
    skill_sources: dict[str, Path] = field(default_factory=_default_skill_sources)
    max_concurrent_jobs: int = 1
    max_skill_zip_bytes: int = MAX_SKILL_ZIP_BYTES
    samples_enabled: bool = False

    def __post_init__(self) -> None:
        self.studio_root = Path(self.studio_root).expanduser()
        self.skill_sources = {name: Path(p).expanduser() for name, p in self.skill_sources.items()}

    @property
    def skills_dir(self) -> Path:
        return self.studio_root / "skills"

    @property
    def tasksets_dir(self) -> Path:
        return self.studio_root / "tasksets"

    @property
    def jobs_dir(self) -> Path:
        return self.studio_root / "jobs"

    @property
    def samples_skills_dir(self) -> Path:
        return self.studio_root / "samples" / "skills"

    @classmethod
    def from_env(cls, **overrides) -> "StudioConfig":
        """Build a config from environment variables, then apply overrides."""
        kwargs: dict = {}
        root = os.environ.get("SKILLOPT_STUDIO_ROOT")
        if root:
            kwargs["studio_root"] = Path(root)
        sources_raw = os.environ.get("SKILLOPT_STUDIO_SKILL_SOURCES")
        if sources_raw:
            kwargs["skill_sources"] = _parse_sources_env(sources_raw)
        samples_raw = os.environ.get("SKILLOPT_STUDIO_SAMPLES", "")
        kwargs["samples_enabled"] = samples_raw.strip().lower() not in {"0", "false", "off", "no"}
        kwargs.update(overrides)
        return cls(**kwargs)
