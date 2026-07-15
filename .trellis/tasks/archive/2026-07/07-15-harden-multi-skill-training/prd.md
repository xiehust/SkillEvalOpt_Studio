# Harden multi-skill plugin training

## Goal

Make Studio Plugin training fail before model calls when selected trainable
Skills cannot receive both training gradients and held-out validation, and make
completed job results explain which task failures were excluded from training.
Accepted candidates must show improvement connected to the Skills that were
actually modified, rather than passing only because an unrelated validation
metric moved. Prevent avoidable coverage failures by surfacing the same
requirements while task sets are created and before Studio submits training.

## Background

- Job `train-20260715-030604-9039f9` selected seven Plugin Skills and marked
  six trainable, but the coverage-aware `4:3:3` split produced only three train
  tasks covering `webnovel-plan`, `webnovel-query`, and `webnovel-review`.
- `webnovel-init`, `webnovel-learn`, and `webnovel-write` had no training
  examples. Existing preflight validates held-out coverage only
  (`skillopt/engine/plugin_trainer.py:113`).
- `webnovel-plan` failed twice with `Response stalled mid-stream`. The intended
  no-gradient-on-infrastructure-failure rule
  (`skillopt/evaluation/plugin_gate.py:44`) left only `webnovel-review`
  eligible for optimization.
- The accepted candidate changed only `webnovel-review`; its held-out soft
  score remained `0.45`, while `webnovel-write` moved from `0.35` to `0.58`.
  The current gate checks overall improvement plus non-regression, but does not
  require an edited Skill to improve
  (`skillopt/evaluation/plugin_gate.py:154`).
- Studio step projection currently exposes attribution counts but not the task
  IDs or persisted execution/judge errors
  (`skillopt_studio/artifacts.py:20`).
- Job `train-20260715-052430-7f9ece` correctly failed preflight because
  `webnovel-write` appeared in only one source task. Studio task generation
  currently asks only for one task per selected Skill, so its output contract
  can produce data that Plugin training must reject.

## Requirements

### R1. Disjoint training and validation coverage

- Ratio-mode Plugin splitting must deterministically reserve disjoint task sets
  covering every trainable Skill in both train and validation.
- Requested ratios remain targets, but coverage may expand train or validation
  counts when enough source tasks exist.
- Train, validation, and test remain disjoint; train and validation must be
  non-empty. Preserve the existing non-empty test contract.
- The split manifest must record required training and validation Skills,
  minimum coverage counts, actual counts, and coverage-aware mode.

### R2. Fail-fast coverage validation

- Plugin preflight must validate the actual train set and the post-`sel_env_num`
  validation set before model configuration.
- Missing coverage errors must name the split and every uncovered trainable
  Skill.
- If the source cannot provide disjoint train and validation coverage while
  retaining a non-empty test set, fail before model calls with an actionable
  message.
- Explicit `split_dir` inputs receive the same train and validation coverage
  validation as ratio-mode inputs.

### R3. Visible excluded failures

- Every training step must persist rollout and judge failures excluded from
  attribution, including task ID, category, target Skills, and normalized
  failure reason.
- Studio's train result contract and Job Detail timeline must display these
  failures without requiring artifact inspection.
- Successful Plugin jobs with excluded failures remain process-successful, but
  the result view must visibly distinguish them from fully successful rollouts.

### R4. Candidate gate relevance

- Preserve complete-Plugin validation, strict overall improvement, and
  per-trainable-Skill regression checks.
- Require at least one Skill actually modified in the candidate to strictly
  improve its own held-out metric. Other modified Skills may remain neutral,
  but all trainable Skills remain subject to the configured regression limit.
- Rejection reasons must name the relevant modified Skills and be persisted in
  history and Studio results.

### R5. Compatibility

- Frozen installed Skills do not require train or validation coverage.
- Single-Skill training and Plugin evaluation behavior remain unchanged.
- Existing history and summary artifacts lacking new optional fields remain
  readable by Studio.
- No model calls are used by tests.

### R6. Proactive task-set coverage guidance

- The new-task-set upload and manual paths must explain that each trainable
  Plugin Skill needs at least two distinct source tasks for disjoint train and
  validation coverage, plus enough data to retain test.
- Task sets remain reusable and are not permanently bound to a Plugin or
  trainable Skill selection.
- Studio must expose the minimum through a typed backend contract rather than
  duplicating a frontend-only constant.

### R7. AI generation guarantees

- Plugin task generation treats the selected Skills as required coverage.
- Studio automatically raises the requested count to at least
  `2 * selected_skill_count + 1`.
- The generation prompt and post-generation validator require every selected
  Skill to appear in at least two distinct tasks.
- A deficient generation attempt must name each under-covered Skill and feed
  that reason into the existing retry.
- Direct task-generation CLI callers retain the legacy one-task default unless
  they opt into a higher per-Skill minimum.

### R8. Training submission check

- After a Plugin, trainable Skill subset, and task set are selected, Studio
  must display a per-Skill coverage report.
- Single-file task sets use the same exact disjoint train/validation/test
  feasibility rule as ratio-mode training.
- Explicit split task sets report train and validation counts separately.
- Studio must reject an invalid Plugin training request synchronously before a
  job is queued, while `PluginTrainer.preflight()` remains the final CLI guard.

## Acceptance Criteria

- [x] A ratio task set with sufficient examples produces deterministic,
      disjoint train/validation coverage for every trainable Skill and a
      non-empty test set.
- [x] The webnovel task distribution from the investigated job is rejected
      before model setup because `webnovel-write` has only one source task and
      cannot cover both train and validation.
- [x] Ratio and explicit-split preflight errors list missing train versus
      validation coverage separately.
- [x] Rollout and judge failures remain gradient-ineligible and appear in
      `history.json`, the Studio train results API, and Job Detail with their
      concrete reasons.
- [x] A candidate whose overall score rises only on an unmodified Skill is
      rejected with a named gate reason.
- [x] Existing Plugin snapshots, resume, per-Skill diffs, and old result
      artifacts remain compatible.
- [x] Targeted backend tests, full pytest, Python syntax checks, frontend build,
      and `git diff --check` pass.
- [ ] Upload/manual task-set creation explains the Plugin coverage minimum.
- [ ] Plugin AI generation auto-raises six selected Skills to at least 13
      tasks and validates at least two target occurrences per Skill.
- [ ] The generation retry reason names under-covered Skills and actual versus
      required counts.
- [ ] Plugin Train shows a typed per-Skill coverage report and cannot submit an
      invalid selected task set.
- [ ] Direct API callers receive the same pre-queue rejection as the UI.
- [ ] New backend/frontend regressions, full pytest, frontend build, and
      desktop/mobile rendered checks pass.

## Out of Scope

- Retrying or salvaging stalled Claude API streams.
- Installing or configuring `claude_agent_sdk`.
- Changing successful job status to a new persisted status enum.
- Repairing an existing task set by generating only its missing tasks.
