# Implementation Plan: Plugin Unified Evaluation

## Checklist

- [x] Add shared runtime-Skill descriptors and multi-Skill workspace
      materialization with path/name validation.
- [x] Extend skilleval rollout and `evaluate_skill.py` to accept repeated Skills
      while preserving one-Skill behavior.
- [x] Add pure task metadata normalization and Plugin result aggregation.
- [x] Persist additive Plugin metrics in evaluation artifacts.
- [x] Extend Studio eval runner validation and repeated `--skill` argv.
- [x] Add Plugin/Single Skill selection to the Evaluate page using existing
      Plugin grouping patterns from task generation.
- [x] Render Plugin identity, Skill count, and aggregate metrics in jobs.
- [x] Add Python tests for workspace layout, validation, attribution,
      aggregation, CLI compatibility, and runner construction.
- [x] Build frontend and browser-test `cc-knowledge` at desktop and 390px.

## Validation

```bash
python3 -m pytest tests/test_skilleval.py tests/test_studio_runners.py -q
python3 -m pytest tests/ -q
python3 -m py_compile scripts/evaluate_skill.py skillopt/envs/skilleval/*.py skillopt/model/codex_harness.py
cd skillopt_studio/frontend && npm run build
git diff --check
```

No real target-model or judge call is required for acceptance.

## Risk and Rollback Points

- Workspace deletion semantics must remain one delete per task.
- Support-file destinations must be validated against traversal and collision.
- Do not change existing summary keys or single-Skill runtime directory names.
- Land backend contracts and tests before UI wiring so payload drift is caught.
