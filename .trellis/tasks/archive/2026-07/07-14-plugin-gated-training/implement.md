# Implementation Plan: Plugin Gated Directed Training

## Checklist

- [x] Add shared Plugin state collection, manifest, immutable snapshot, reload,
      hash, and deployable export helpers using existing runtime-name and
      support-file validation.
- [x] Add pure failure attribution, responsible-Skill selection, Plugin metric
      projection, validation-coverage checks, and regression gate.
- [x] Add a SkillEval complete-Plugin training adapter that reuses Plugin
      rollout/judging and persists reflection trajectories.
- [x] Implement `PluginTrainer` for baseline, deterministic train batches,
      directed per-Skill patches, complete-Plugin validation, history,
      snapshots, resume, and optional test evaluation.
- [x] Add `scripts/train_plugin.py`, structured config mappings/defaults, and
      fail-fast validation before model configuration.
- [x] Extend Studio train command construction for Plugin selection and
      `cc-knowledge`-shaped repeated Skill paths/trainable names.
- [x] Extend Train UI with Plugin mode, trainable-Skill controls, regression
      threshold, and max-Skills controls while preserving Single Skill mode.
- [x] Extend train artifact parsing, TypeScript contracts, job summary, and
      per-Skill diffs for Plugin runs.
- [x] Add backend unit/integration tests for attribution, gate decisions,
      unchanged Skills, snapshots/export, resume, CLI preflight, Studio command
      construction, and stub Plugin jobs.
- [x] Add frontend tests/build and desktop/mobile browser checks for Plugin
      training selection and result rendering.
- [x] Run the full quality gate, update the Plugin training spec, and commit the
      child task independently.

## Validation

```bash
python3 -m pytest tests/test_plugin_training.py tests/test_skilleval.py tests/test_studio_runners.py -q
python3 -m pytest tests/ -q
python3 -m py_compile scripts/train_plugin.py skillopt/engine/plugin_trainer.py skillopt/evaluation/plugin_gate.py
cd skillopt_studio/frontend && npm run build
git diff --check
```

Browser verification uses the Studio dev server with a stub or constructed
Plugin train job; no real target or optimizer call is required.

## Risk and Rollback Points

- Land pure state/attribution/gate contracts and tests before the orchestration
  loop.
- Keep model configuration after all path/task/coverage validation so malformed
  Plugin jobs spend no model calls.
- Keep `ReflACTTrainer`, `scripts/train.py`, and legacy Studio command output
  byte-for-byte untouched unless an additive shared helper extraction has
  explicit regression coverage.
- Snapshot only complete Plugin directories and atomically move them into
  place; never let resume point to a partial candidate.
- Validate backend and Studio artifacts before UI wiring to catch payload drift.
- If Plugin orchestration fails integration tests, remove only the new
  `train_plugin.py` runner branch; single-Skill workflows remain available.
