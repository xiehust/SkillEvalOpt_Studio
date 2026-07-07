"""SkillOpt Studio — FastAPI + React operator console for skill evaluation and training.

Standalone package: wraps the existing CLI entry points (scripts/evaluate_skill.py,
scripts/train.py) behind a localhost web UI.  It must not be imported by
``skillopt/`` or ``skillopt_sleep/``.
"""
from __future__ import annotations

__version__ = "0.1.0"
