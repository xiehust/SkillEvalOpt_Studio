# Plugin Gated Directed Training

## Goal

Optimize one or more responsible Skills while accepting a candidate only when
the complete Plugin improves without unacceptable per-Skill regression.

## User Value

Plugin authors can improve failures attributed to individual Skills without
flattening the Plugin into one document or accepting a locally harmful update
because another Skill's stronger score hides the regression.

## Background

Implementation starts after `07-14-plugin-unified-evaluation` stabilizes the
Plugin snapshot, task attribution, aggregate report, and workspace contracts.
That task is complete in commit `be007a7`; its runtime and result contracts are
documented in `.trellis/spec/backend/skilleval-plugin-evaluation.md`.

The existing `ReflACTTrainer` treats trainable state as one string and persists
`best_skill.md`. Plugin training therefore uses a dedicated orchestration path
that reuses the existing reflection, aggregation, ranking, edit application,
scoring, model configuration, and SkillEval runtime primitives without
changing the single-Skill trainer contract.

## Requirements

### R1. Named Plugin State

- Represent state as an ordered set of runtime Skills with unique names,
  mutable `SKILL.md` content, and frozen support files.
- Never flatten multiple Skill documents into one optimizer input or artifact.
- Allow the operator to mark a subset of installed Plugin Skills as trainable.

### R2. Failure Attribution

- Classify scored failures deterministically as `routing`, `execution`,
  `handoff`, or `shared_dependency` from `task_type` and `target_skills`.
- Classify rollout and judge failures separately as `task_failure` and
  `judge_failure`; these failures remain visible in artifacts but never produce
  optimizer gradients.
- Select responsible trainable Skills from attributed failures in stable,
  failure-count-first order.

### R3. Directed Candidate Updates

- Update no more than `max_skills_per_candidate`, default `2`.
- Run reflection, aggregation, ranking, and edit application independently for
  each selected Skill using only trajectories attributed to that Skill.
- Preserve every unselected Skill byte-for-byte in the candidate snapshot.

### R4. Plugin Validation Gate

- Evaluate the baseline and every candidate by installing and running the
  complete Plugin on the held-out validation split.
- Require strict improvement of the configured overall gate metric.
- Reject a candidate when any covered Skill regresses beyond the configurable
  `max_skill_regression`, default `0.0`.
- Require held-out validation coverage for every installed Plugin Skill before
  model calls so the regression guard cannot silently omit a Skill.

### R5. Persistence and Resume

- Persist immutable complete Plugin snapshots, step history, aggregate metrics,
  attribution records, candidate edit reports, and a runtime-state pointer.
- Resume from the last completed Plugin snapshot without replaying accepted
  steps.
- Emit `best_plugin/manifest.json` plus each Skill directory, `SKILL.md`, and
  support files as a deployable artifact.

### R6. CLI and Studio

- Provide a Plugin training CLI/config path that accepts repeated Skill paths,
  a trainable Skill subset, regression threshold, and maximum Skills updated
  per candidate.
- Extend Studio Train with Single Skill / Plugin modes, Plugin selection, and
  trainable-Skill controls using the established evaluation selection
  contract.
- Render Plugin gate and per-Skill metrics from additive training artifacts.

### R7. Compatibility

- Preserve `scripts/train.py`, `ReflACTTrainer`, single-Skill Studio payloads,
  single-Skill artifacts, and `best_skill.md`.
- Keep Plugin support-file optimization and epoch-level slow/meta-Skill stages
  out of this initial directed-training path.

## Out of Scope

- Jointly editing more than two Skills in one candidate by default.
- Training arbitrary Plugin hooks, agents, commands, binaries, or non-Skill
  files.
- Inferring causal responsibility from hidden agent reasoning beyond explicit
  task metadata and observable execution/judge failures.
- Running real `cc-knowledge` model calls in automated tests.

## Acceptance Criteria

- [x] Plugin state and candidate artifacts retain distinct named Skill
      documents; unselected Skills are unchanged.
- [x] Routing, execution, handoff, and shared-dependency failures select only
      named trainable Skills, with at most two selected by default.
- [x] Rollout/judge failures are recorded but produce no reflection calls.
- [x] Validation fails before model configuration when any Plugin Skill lacks
      held-out attribution coverage.
- [x] A candidate with a higher overall score but excessive per-Skill
      regression is rejected by a pure, unit-tested gate.
- [x] A strictly improved, non-regressing candidate is accepted and exported
      as a complete `best_plugin/` snapshot.
- [x] Interrupted training resumes from the last complete Plugin snapshot and
      aggregate state.
- [x] Studio constructs a `cc-knowledge` Plugin training command and renders
      Plugin metrics using stub CLIs only.
- [x] Existing single-Skill training, evaluation, Studio tests, and frontend
      build remain green.
