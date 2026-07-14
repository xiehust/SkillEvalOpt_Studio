# Fix Plugin validation split coverage

## Goal

Prevent Studio Plugin training from reaching model setup with a randomly
under-covered validation split, while preserving regression protection for
every trainable Plugin Skill.

## Background

- Job `train-20260714-140248-4974a9` selected seven Plugin Skills and used a
  ten-task single-file task set with ratio `4:3:3`.
- In the current Studio payload, `skill_ids` defines all Skills installed and
  validated during the run, while `trainable_skill_ids` only defines which of
  those Skill documents the optimizer may edit. The Train UI exposes the
  latter checkbox set. This behavior remains unchanged.
- The generic ratio splitter shuffles tasks and slices them by count
  (`skillopt/datasets/base.py:330`), producing a three-task validation split.
  Three tasks cannot cover the six trainable Skills.
- The complete source task set targets six Skills and contains no task for
  `webnovel-dashboard`. It was generated for those six Skills, while the later
  training job installed all seven.
- Plugin training currently checks the actual held-out selection items against
  every installed Skill before model configuration
  (`skillopt/engine/plugin_trainer.py:117`); this is the behavior being narrowed
  to trainable Skills.
- Plugin task generation already rejects missing coverage for the Skills
  selected during generation (`scripts/generate_tasks.py:266`).

## Requirements

- Ratio splitting for Plugin training must deterministically place tasks so
  the validation split covers every trainable Plugin Skill when the source
  task set makes that possible.
- The effective validation size may exceed the nominal ratio allocation when
  coverage requires it, but train and test splits must remain non-empty and
  all splits must remain disjoint.
- A source task set that cannot cover every trainable Plugin Skill must fail
  before any model call with an actionable error naming the uncovered
  trainable Skills.
- Required validation coverage and per-Skill regression checks are based on
  `trainable_skill_ids`. An installed but frozen Skill may have no directly
  attributed task.
- Every `skill_ids` entry remains installed during all rollouts. Frozen Skills
  are excluded only from directed edits and per-Skill gate metrics, not from
  the runtime.
- Overall gate scoring still uses every validation task. A frozen Skill without
  attributed validation tasks has no dedicated regression metric; this is an
  accepted limitation of excluding it from the trainable scope.
- Explicit split-directory task sets retain their authored boundaries and
  continue to fail if their validation split does not cover every trainable
  Skill.
- Generic ratio splitting and existing single-Skill training behavior must not
  change.
- The gate must not silently drop regression checks for any trainable Skill.

## Acceptance Criteria

- [x] A Plugin ratio split with sufficient source coverage creates a
      deterministic validation set covering every trainable Skill, even when
      the nominal validation count is too small.
- [x] The generated split manifest records actual split counts and the required
      validation Skill names.
- [x] Missing source coverage names absent trainable Skills and performs zero
      model calls; missing coverage for frozen Skills is accepted.
- [x] A positive `sel_env_num` that truncates required validation coverage is
      rejected before model setup.
- [x] Per-Skill baseline, candidate, persisted runtime, and summary metrics use
      the trainable Skill set and do not require frozen-Skill coverage.
- [x] Existing generic splitter, single-Skill, explicit split-directory, and
      Plugin gate tests remain green.
