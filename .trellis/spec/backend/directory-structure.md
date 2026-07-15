# Backend Directory Structure

## Package Boundaries

SkillOpt is a Python 3.10+ repository with four runtime packages and thin CLI
entry points. Preserve these ownership boundaries:

| Path | Responsibility |
|---|---|
| `skillopt/` | Research training framework: environments, model routing, reflection, optimization, and validation gates |
| `skillopt_sleep/` | Standalone nightly self-evolution tool for local coding agents |
| `skillopt_studio/` | FastAPI service that validates requests and runs the repository CLIs as subprocess jobs |
| `skillopt_webui/` | Optional Gradio dashboard |
| `scripts/` | User-facing training, evaluation, task generation, and data materialization entry points |
| `configs/` | Inheritable YAML experiment configuration |
| `tests/` | Flat backend test suite |
| `plugins/` | Host-specific wrappers around the public CLI or `skillopt_sleep` interface |

`skillopt_sleep/` deliberately has zero imports from `skillopt/`. Its validation
gate is vendored in `skillopt_sleep/gate.py`; keep it behaviorally aligned
without introducing a cross-package import.

`skillopt_studio/` is an orchestration layer, not a second training engine. It
may reuse `skillopt` config parsing and SkillEval validation, but training and
evaluation still run through scripts such as `scripts/train.py`,
`scripts/train_plugin.py`, and `scripts/evaluate_skill.py`.

References:

- `CLAUDE.md`
- `skillopt_sleep/gate.py`
- `skillopt_studio/runners.py`
- `skillopt_studio/jobs.py`

## Core Framework Layout

The main training loop is organized by pipeline responsibility:

| Path | Local pattern |
|---|---|
| `skillopt/engine/` | Orchestration and persisted training state |
| `skillopt/envs/<name>/` | Benchmark-specific adapter, dataloader, rollout, and evaluator |
| `skillopt/gradient/` | Reflection and patch aggregation |
| `skillopt/optimizer/` | Edit selection, clipping, application, rewrite, and slow/meta updates |
| `skillopt/evaluation/` | Pure gate decisions; callers own I/O and state mutation |
| `skillopt/model/` | Optimizer/target backend routing and agentic execution harnesses |
| `skillopt/datasets/` | Batch planning and deterministic train/eval splits |
| `skillopt/types.py` | Shared edit, patch, rollout, and stage-result dataclasses |

`skillopt/engine/trainer.py` owns the six-stage single-Skill loop. Plugin
training has different state and artifact contracts and belongs in
`skillopt/engine/plugin_trainer.py`; do not force it through `ReflACTTrainer`.

Use the public model routing functions from `skillopt.model`, such as
`chat_optimizer()` and `chat_target()`. Vendor-specific SDK and CLI details
stay under `skillopt/model/`.

Shared cross-stage payloads should have one typed owner. For example,
`RolloutResult` and `Patch` live in `skillopt/types.py`, while `BatchSpec` lives
next to the dataloader contract in `skillopt/datasets/base.py`. Convert these
objects to plain dictionaries at artifact and legacy API boundaries.

## Environment Modules

A dataset-backed benchmark normally follows:

```text
skillopt/envs/<name>/
  adapter.py
  dataloader.py
  rollout.py
  evaluator.py
```

The adapter implements `EnvAdapter` from `skillopt/envs/base.py`. Rollout rows
must contain `id`, `hard`, and `soft`; environment-specific fields remain on
the row and are preserved by `RolloutResult.extras`.

Built-in registration lives in `scripts/train.py::_register_builtins()` and is
duplicated in `scripts/eval_only.py`. Imports are intentionally lazy and
guarded so optional benchmark dependencies do not break CLI parsing or
`--help`. When adding a benchmark, update both registries and use
`skillopt/envs/searchqa/` or `skillopt/envs/_template/` as the reference.

## Studio Layout

Keep Studio concerns separated:

| Path | Responsibility |
|---|---|
| `skillopt_studio/models.py` | Pydantic request/response contracts |
| `skillopt_studio/api/` | FastAPI routes and HTTP error translation |
| `skillopt_studio/runners.py` | Request validation, config materialization, and subprocess argv construction |
| `skillopt_studio/jobs.py` | Queue, process lifecycle, cancellation, job records, and log capture |
| `skillopt_studio/artifacts.py` | Read-only projection of job output artifacts |
| `skillopt_studio/tasksets.py` | Task-set persistence and validation |
| `skillopt_studio/skill_sources.py` | Skill discovery and source normalization |
| `skillopt_studio/frontend/` | Vite/React/TypeScript application |

Routes should stay thin: resolve dependencies, call a domain/helper function,
and translate expected exceptions to `HTTPException`. Do not put subprocess
construction or filesystem traversal directly in a route.

## Naming And Placement

- Use `snake_case.py` for modules and `snake_case` for functions.
- Use `PascalCase` for dataclasses, Pydantic models, adapters, and managers.
- Prefix module-private helpers with `_`.
- Keep tests in flat `tests/test_<area>.py` files and group related cases in
  `class Test...` where useful.
- Keep experiment defaults in `configs/<env>/default.yaml`; `_base_` is one
  relative string path, not a list.
- Keep generated jobs, checkpoints, rollouts, and reports below an `out_root`
  or Studio data root. Do not write generated state into package directories.

## Placement Checklist

Before adding a module:

1. Identify the package that owns the behavior; do not create a generic
   top-level helper for package-specific logic.
2. Search for an existing shared contract or helper before adding one.
3. Put validation at the earliest boundary that has enough context.
4. Keep pure decisions separate from side effects where the codebase already
   does so, as in `skillopt/evaluation/gate.py`.
5. Add or update the flat test module that owns the behavior.

Avoid importing Studio code from `skillopt/`, importing `skillopt/` from
`skillopt_sleep/`, calling a vendor model SDK outside `skillopt/model/`, or
duplicating backend constants in frontend code.
