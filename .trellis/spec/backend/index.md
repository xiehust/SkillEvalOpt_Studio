# Backend Development Guidelines

Project-specific contracts for the SkillOpt research framework, SkillOpt-Sleep,
and SkillOpt Studio backend.

---

## Pre-Development Checklist

Always read:

- [Directory Structure](./directory-structure.md)
- [Quality Guidelines](./quality-guidelines.md)

Then read the guides that match the change:

- Persistence, checkpoints, job records, or artifacts:
  [Persistence And Artifact Storage](./database-guidelines.md)
- Validation, API responses, worker failures, or per-task failures:
  [Error Handling](./error-handling.md)
- CLI progress, server logs, diagnostics, or secret redaction:
  [Logging And Observability](./logging-guidelines.md)
- Multi-Skill evaluation:
  [SkillEval Plugin Evaluation](./skilleval-plugin-evaluation.md)
- Multi-Skill training:
  [SkillEval Plugin Training](./skilleval-plugin-training.md)

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Package ownership, module placement, and naming | Active |
| [Persistence And Artifact Storage](./database-guidelines.md) | Filesystem-backed state, serialization, compatibility, and path safety | Active |
| [Error Handling](./error-handling.md) | Fail-fast validation, per-task isolation, job failures, and API mapping | Active |
| [Quality Guidelines](./quality-guidelines.md) | Architecture invariants, testing, verification, and forbidden patterns | Active |
| [Logging And Observability](./logging-guidelines.md) | CLI progress, server logging, artifacts, and secret redaction | Active |
| [SkillEval Plugin Evaluation](./skilleval-plugin-evaluation.md) | Multi-Skill runtime, Studio payload, validation, and metrics | Active |
| [SkillEval Plugin Training](./skilleval-plugin-training.md) | Directed multi-Skill updates, complete-Plugin gate, resume, and Studio contracts | Active |

All specification documents are written in English and should describe current
repository behavior. Update them when a code change deliberately changes one
of these contracts.
