# Plugin Unified Evaluation

## Goal

Evaluate the real behavior of multiple Skills belonging to one Plugin in a
single isolated agent runtime, with actionable Plugin and Skill-level results.

## Background

AI task generation already accepts multiple Skills from one Plugin and emits
`target_skills` plus `task_type`. Evaluation currently resolves one `skill_id`,
passes one `--skill`, and installs it as `skillopt-target`, so it cannot measure
Skill discovery, routing, or collaboration.

## Requirements

- Add Plugin mode to the evaluation CLI, Studio runner, API payload, and UI.
- Accept a deduplicated non-empty `skill_ids` list whose entries all belong to
  the same non-null Plugin; fail before model calls otherwise.
- Materialize each selected Skill under its own stable runtime directory,
  retaining its `SKILL.md` and support files.
- Run every task with all selected Skills available. `target_skills` is
  attribution metadata and must not reveal the expected route to the target
  model.
- Preserve task-level `hard` and `soft` scoring and enrich results with
  normalized `target_skills` and `task_type`.
- Produce deterministic aggregates for overall score, each Skill, each task
  type, routing tasks, integration tasks, and weakest Skill.
- Tasks without `target_skills` remain valid and contribute only to overall and
  task-type aggregates.
- Preserve the existing single-Skill CLI and Studio behavior.
- Job details and lists identify Plugin mode, Plugin name, and Skill count.

## Acceptance Criteria

- [x] Repeated `--skill` arguments evaluate all supplied Skills together;
      a single argument behaves exactly as before.
- [x] The workspace contains separate discoverable Skill directories and each
      Skill's support files without collisions.
- [x] Invalid paths, duplicate runtime names, cross-Plugin selections, malformed
      `target_skills`, and unknown target Skills fail before rollout.
- [x] `summary.json` includes task totals plus overall, per-Skill, per-task-type,
      routing, integration, and weakest-Skill hard/soft metrics.
- [x] Studio can select Plugin mode, defaults to all Plugin Skills, and submits
      the exact `skill_ids`.
- [x] `cc-knowledge` displays six selected Skills and a Plugin evaluation job
      can be constructed without issuing a real model call.
- [x] Focused tests, full pytest, frontend build, and desktop/mobile browser
      checks pass.

## Out of Scope

- Editing or installing the source Plugin.
- Training multiple Skills; owned by `07-14-plugin-gated-training`.
- Scoring whether a particular internal Skill tool invocation occurred; this
  phase attributes outcomes using task metadata.
