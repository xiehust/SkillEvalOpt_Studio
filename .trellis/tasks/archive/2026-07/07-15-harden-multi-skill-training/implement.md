# Implementation Plan

## 1. Dual-Coverage Splitting

- [x] Add deterministic coverage helpers in
      `skillopt/envs/skilleval/dataloader.py`.
- [x] Partition ratio-mode tasks into disjoint train and validation covers
      before distributing ratio extras.
- [x] Try deterministic test reservations so a greedy cover does not reject a
      valid disjoint train/validation/test partition.
- [x] Persist required/minimum train and validation coverage in the split
      manifest.
- [x] Add ratio tests for deterministic dual coverage, source counts below
      two, insufficient room for test, and the investigated webnovel shape.

## 2. Fail-Fast Trainer Validation

- [x] Generalize `validate_plugin_coverage()` with a named split.
- [x] Validate actual train coverage and post-`sel_env_num` validation coverage
      in `PluginTrainer.preflight()`.
- [x] Add explicit-split regressions proving failures occur before model setup
      and frozen Skills remain exempt.

## 3. Failure Result Contract

- [x] Extend `FailureAttribution` with target Skills and normalized reason.
- [x] Persist `excluded_failures` in attribution artifacts and step history.
- [x] Project the optional field and total count through
      `skillopt_studio/artifacts.py`.
- [x] Extend frontend API types, translations, and Job Detail timeline to show
      excluded failure reasons.
- [x] Add backend/API regressions for old rows and new failure details.

## 4. Gate Relevance

- [x] Add optional modified-Skill input to `evaluate_plugin_gate()`.
- [x] Derive actually modified Skills from current/candidate Plugin state.
- [x] Reject candidates when no modified Skill strictly improves; preserve
      overall and regression checks.
- [x] Add pure gate and trainer-level accept/reject tests.

## 5. Contract Documentation

- [x] Update `.trellis/spec/backend/skilleval-plugin-training.md` with dual
      coverage, excluded failures, and modified-Skill gate requirements.

## 6. Verification

- [x] Run:
      `python3 -m pytest tests/test_plugin_training.py tests/test_skilleval.py tests/test_studio_runners.py -q`
- [x] Run full suite: `python3 -m pytest tests/ -q`
- [x] Run `python3 -m py_compile` on changed Python modules.
- [x] Run `cd skillopt_studio/frontend && npm run build`.
- [x] Run `git diff --check`.
- [x] Inspect the final diff for unrelated changes and preserve all pre-existing
      user modifications.

## 7. Shared Coverage Analysis

- [ ] Extract the Plugin coverage minimum and disjoint planner into a pure
      SkillEval module.
- [ ] Keep `SkillEvalDataLoader` behavior and error messages compatible while
      delegating to the shared planner.
- [ ] Add Studio task-set coverage reporting for ratio and explicit splits.
- [ ] Reject invalid Plugin training requests before queue creation.

## 8. Proactive Task Generation

- [ ] Add an optional per-Skill minimum to `scripts/generate_tasks.py`.
- [ ] Include exact per-Skill quotas in the prompt and validator retry reason.
- [ ] Normalize Studio Plugin generation count to `2 * Skills + 1` in both the
      UI and command builder.
- [ ] Expose server-owned task-generation constraints through the environment
      API.

## 9. Studio Guidance And Train Guard

- [ ] Add upload/manual format guidance for Plugin training coverage.
- [ ] Show effective AI generation count and per-Skill minimum in Plugin mode.
- [ ] Add typed API contracts and a live coverage report to Plugin Train.
- [ ] Disable submission while coverage is loading or invalid.

## 10. Extended Verification

- [ ] Add pure planner, generator, runner/API, and backward-compatibility tests.
- [ ] Run targeted and full pytest.
- [ ] Run Python syntax checks, frontend build, and `git diff --check`.
- [ ] Verify creation guidance, auto count, coverage status, invalid-submit
      guard, console health, and responsive layout in a rendered browser.

## Rollback Points

- Split logic is isolated to `SkillEvalDataLoader`; revert its helper and
  manifest changes together.
- Gate relevance is isolated behind `modified_skill_names`.
- Studio failure rendering is additive and may be reverted without invalidating
  backend artifacts.
- Proactive coverage reporting is additive and does not change task-set storage.
- The task generator CLI minimum defaults to one, so Studio's stricter behavior
  can be rolled back at the command-builder call site.
