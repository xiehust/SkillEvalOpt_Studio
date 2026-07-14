# Existing Training and Plugin Contracts

## Sources Inspected

- `skillopt/engine/trainer.py`
- `skillopt/evaluation/gate.py`
- `skillopt/gradient/reflect.py`
- `skillopt/gradient/aggregate.py`
- `skillopt/optimizer/clip.py`
- `skillopt/optimizer/skill.py`
- `skillopt/envs/base.py`
- `skillopt/envs/skilleval/adapter.py`
- `skillopt/envs/skilleval/rollout.py`
- `scripts/evaluate_skill.py`
- `skillopt_studio/runners.py`
- `skillopt_studio/artifacts.py`
- `skillopt_studio/frontend/src/pages/Evaluate.tsx`
- `.trellis/spec/backend/skilleval-plugin-evaluation.md`
- `tests/test_skilleval.py`
- `tests/test_studio_runners.py`

## Confirmed Reuse Points

- `EnvAdapter.reflect()` accepts an arbitrary filtered result list and delegates
  to shared minibatch reflection.
- `merge_patches()`, `rank_and_select()`, and `apply_patch_with_report()` are
  document-local and can be invoked once per selected Skill.
- `select_gate_score()` already defines hard, soft, and mixed projection.
- SkillEval Plugin rollout already installs ordered runtime Skills together and
  keeps `target_skills` out of the target prompt.
- `aggregate_results()` already emits overall, per-Skill, per-type, routing,
  integration, and weakest-Skill metrics.
- Studio already validates Plugin identity and emits repeated `--skill`
  arguments for task generation/evaluation.

## Constraints

- `ReflACTTrainer` stores current/best state as strings and persists
  `best_skill.md`; slow update, appendix, rewrite, and resume code all assume a
  single document.
- `SkillEvalAdapter.rollout()` currently accepts one string and installs the
  legacy `skillopt-target` runtime.
- Config flattening passes unknown `env` keys but requires explicit mappings for
  new optimizer/evaluation keys.
- Studio train currently resolves only `skill_id`, writes one `skill_init`, and
  parses `best_skill.md` into one diff.

## Resulting Decision

Add a focused Plugin trainer and CLI. Reuse stable stage primitives and Plugin
runtime contracts, but do not refactor the single-Skill trainer into generic
multi-state machinery during this task.
