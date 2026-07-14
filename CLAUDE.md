# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SkillOpt treats an agent's skill document (markdown) as the trainable state of a frozen model: a separate optimizer model turns scored rollouts into bounded add/delete/replace edits on the skill, and a candidate edit is accepted only if it strictly improves a held-out validation score. The deployed artifact is a `best_skill.md`. Internally many modules still use the historical name **ReflACT** in docstrings — same thing.

## Commands

```bash
# On this box, `python` is not on PATH — always use python3.
python3 -m pytest tests/ -q                    # full test suite (fast, ~3s)
python3 -m pytest tests/test_skilleval.py -q   # single test file
python3 -m pytest tests/test_scoring.py::TestComputeScore::test_single_result -q  # single test

# ruff is configured in pyproject.toml but not installed in the venv;
# use py_compile as the syntax gate when ruff is unavailable:
python3 -m py_compile <files>

# Training / evaluation
python3 scripts/train.py --config configs/searchqa/default.yaml   # train a skill
python3 scripts/eval_only.py --config <cfg> --skill <skill.md>    # score one skill on a benchmark env
python3 scripts/evaluate_skill.py --skill <SKILL.md> --tasks <tasks.json> --out_root <dir>  # evaluate an arbitrary skill on a custom task set
python3 scripts/generate_tasks.py --skill <skill> --backend claude_code_exec --count 5 --out_root <dir>  # AI-generate a skilleval task set for a skill (Studio job type "taskgen")
bash scripts/run_searchqa.sh                   # wrapper with env-var model selection

# SkillOpt Studio (localhost web console for skilleval + train; see docs/guide/studio.md)
./start.sh && ./stop.sh                        # start/stop wrapper (pidfile, health check, auto frontend build)
python3 -m skillopt_studio                     # serve on 127.0.0.1:8321 (needs frontend built once)
cd skillopt_studio/frontend && npm run build   # build the frontend (tsc + vite → dist/)

# Data: data/*_id_split/ contains ID-only manifests (no content, licensing).
# Hydrate before running benchmarks, e.g.:
python3 scripts/materialize_searchqa.py        # needs `pip install datasets`
```

Backend/env config comes from `.env` (see `.env.example`; load with `set -a; source .env; set +a`). CLI flags on train.py/eval_only.py override YAML config keys.

## Architecture

Three top-level packages, deliberately decoupled:

- **`skillopt/`** — the research framework (training loop).
- **`skillopt_sleep/`** — SkillOpt-Sleep, a standalone nightly self-evolution tool for local coding agents. **Zero dependency on `skillopt/`** (the validation gate is vendored). Don't introduce cross-imports.
- **`skillopt_webui/`** — optional Gradio dashboard.
- **`skillopt_studio/`** — FastAPI + React localhost console for skilleval evaluation and training (wraps the CLIs as subprocesses; only imports `skillopt` for config parsing and task validation). Frontend lives in `skillopt_studio/frontend/` (Vite+React+TS+Tailwind); tests in `tests/test_studio_core.py` / `tests/test_studio_runners.py` (stub CLIs, no model calls). Built-in samples (`skillopt_studio/samples.py`) materialize ckpt/ + skilleval_demo skills and read-only sample tasksets at startup; `SKILLOPT_STUDIO_SAMPLES=0` disables (`StudioConfig(samples_enabled=...)` defaults False for tests).

### The training loop (skillopt/)

`skillopt/engine/trainer.py` (the single large trainer) drives a 6-stage per-step pipeline — rollout → reflect → aggregate → select → update → gate — plus epoch-level slow-update/meta-skill stages. Stage responsibilities map onto packages:

| Stage | Package |
|---|---|
| Rollout (target model executes tasks under the skill) | `skillopt/envs/<name>/rollout.py` |
| Reflect/Aggregate (optimizer analyzes trajectories → patches) | `skillopt/gradient/` |
| Select/Clip/Update (learning-rate-bounded edits to the skill doc) | `skillopt/optimizer/` |
| Gate (accept iff held-out val score strictly improves) | `skillopt/evaluation/gate.py` (pure decision function; trainer owns side-effects) |
| LR / schedule | `skillopt/scheduler/` |

Shared dataclasses (Edit, Patch, RolloutResult, GateResult, BatchSpec) live in `skillopt/types.py`; everything round-trips to plain dicts.

### Environments (benchmarks)

Each benchmark is a package `skillopt/envs/<name>/` with `dataloader.py`, `rollout.py`, and an `adapter.py` implementing `EnvAdapter` (`skillopt/envs/base.py`). **The env registry lives in `scripts/train.py::_register_builtins()`** (and duplicated in `eval_only.py`), not in `skillopt/envs/__init__.py` — registration is lazy try/except so optional deps don't break `--help`. To add a benchmark, follow `docs/guide/new-benchmark.md`; simplest reference env is `skillopt/envs/searchqa/`.

Key conventions:
- Rollout result dicts need `id`, `hard` (0/1), `soft` (0–1 partial credit); everything else rides along as extras for reflection. There is no separate `evaluate()` — scoring lives inside rollout.
- Dataset items always have an `id`; other fields are env-specific. `SplitDataLoader` (`skillopt/datasets/base.py`) handles both pre-split `train/ val/ test/` dirs (`split_mode: split_dir`) and deterministic ratio splits from a raw file (`split_mode: ratio`).
- `skillopt/envs/skilleval/` is different from the benchmark envs: it evaluates arbitrary user skills on user-provided tasks (each task carries its own `rubric` for an LLM judge) and is driven by `scripts/evaluate_skill.py`, not train.py.

### Model backends

`skillopt/model/` routes two roles — **optimizer** (reflection/patch generation, also the skilleval judge) and **target** (task execution) — each independently set to a backend: `openai_chat` (Azure/OpenAI-compatible), `claude_chat`, `qwen_chat`, `minimax_chat`, or agentic exec backends `codex_exec` / `claude_code_exec`. Call `chat_optimizer()` / `chat_target()` from `skillopt.model`, never a vendor SDK directly. Exec backends live in `codex_harness.py`: `prepare_workspace()` seeds a work_dir (skill goes to `.agents/skills/skillopt-target/SKILL.md`, task to `task.md`) and `run_claude_code_exec()` / `run_codex_exec()` drive the CLI (SDK/CLI dual mode, empty-response retries, artifact persistence). Note `prepare_workspace` rmtree's an existing work_dir — task/item ids are validated filesystem-safe for this reason.

The Claude backend shells out to the `claude` CLI, so the `anthropic` SDK need not be installed.

### Configuration

YAML with single-parent inheritance: `configs/<env>/default.yaml` sets `_base_: ../_base_/default.yaml`. **`_base_` is a string path, not a list.** Config sections: `model`, `train`, `gradient`, `optimizer`, `env`. Parsing lives in `skillopt/config.py`.

## Conventions

- Tests are plain pytest in `tests/` (flat, `test_*.py`), using `tmp_path` and `monkeypatch`; class-based grouping (`class TestX:`) is the norm.
- Design specs for features developed in-session live in `docs/superpowers/specs/`.
- Line length 120 (ruff config); `from __future__ import annotations` at the top of modules.
- Fail-fast validation before spending model calls; failures must surface in results (`error`, `judge_error` fields) rather than being silently swallowed.
<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->
