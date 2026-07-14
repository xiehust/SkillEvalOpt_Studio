# Plugin-level Evaluation and Training

## Goal

Make a Claude Code Plugin, rather than an isolated Skill, a first-class unit for
task generation, evaluation, and optimization while preserving existing
single-Skill workflows.

## Requirements

- Evaluate multiple Skills from one Plugin in the same isolated agent runtime.
- Attribute results by `target_skills` and `task_type`, including routing and
  multi-Skill integration scenarios.
- Train selected Skills from failure attribution while validating candidates
  against the complete Plugin.
- Prevent improvements in strong Skills from hiding regressions in another
  Plugin Skill.
- Keep existing single-Skill APIs, jobs, artifacts, and UI flows compatible.
- Use `cc-knowledge` and its six Skills as the end-to-end reference Plugin.

## Task Map

- `07-14-plugin-unified-evaluation`: Plugin runtime, attribution, aggregation,
  Studio evaluation flow, and reports.
- `07-14-plugin-gated-training`: directed multi-Skill state updates and
  Plugin-level validation gate. Depends on the evaluation contracts above.

## Acceptance Criteria

- [x] A Plugin task set can be generated, evaluated, and reported without
      flattening all Skills into one synthetic Skill.
- [x] Reports expose overall, per-Skill, routing, integration, and weakest-Skill
      metrics.
- [x] Training emits a deployable `best_plugin/` snapshot and rejects
      candidates that violate Plugin regression constraints.
- [x] Existing single-Skill tests and workflows remain green.
