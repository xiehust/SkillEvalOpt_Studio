# SkillEval Plugin Training

## Scenario: Gated Directed Training of a Complete Plugin

### 1. Scope / Trigger

Use this contract when SkillEval optimizes one or more named Skills while every
rollout and validation run installs the complete Plugin.

Plugin training is an additive path:

```text
scripts/train_plugin.py -> PluginTrainer -> complete-Plugin rollout
  -> failure attribution -> per-Skill patches -> complete-Plugin gate
  -> snapshots + best_plugin/
```

Do not route this through `ReflACTTrainer`. Its state, slow update, meta-Skill,
appendix, resume, and `best_skill.md` contracts assume one document.

The initial Plugin path supports patch updates, fixed edit budgets, and frozen
support files. Slow/meta/rewrite updates and support-file optimization are out
of scope.

### 2. Signatures

CLI:

```bash
python3 scripts/train_plugin.py \
  --config <structured-skilleval-config.yaml> \
  --skill <plugin/skills/alpha> \
  --skill <plugin/skills/beta> \
  --train-skill alpha \
  --out_root <output>
```

Core state and orchestration:

```python
collect_plugin_state(
    paths: list[str],
    trainable_names: list[str] | None = None,
    *,
    require_multiple: bool = True,
) -> PluginState

PluginTrainer(cfg, adapter, initial_state).preflight() -> None
PluginTrainer(cfg, adapter, initial_state).train() -> dict

evaluate_plugin_gate(
    current_aggregates: dict,
    candidate_aggregates: dict,
    skill_names: list[str],
    *,
    metric: Literal["hard", "soft", "mixed"] = "hard",
    mixed_weight: float = 0.5,
    max_skill_regression: float = 0.0,
) -> PluginGateResult
```

Studio request:

```json
{
  "target_mode": "plugin",
  "plugin": "cc-knowledge",
  "skill_ids": ["<studio-id-1>", "<studio-id-2>"],
  "trainable_skill_ids": ["<studio-id-1>"],
  "taskset_id": "<taskset-id>",
  "max_skills_per_candidate": 2,
  "max_skill_regression": 0.0
}
```

### 3. Contracts

- Plugin state preserves repeated `--skill` order and keeps each `SKILL.md`
  separate. Runtime names follow frontmatter `name`, then directory/file name.
- Studio maps `trainable_skill_ids` to runtime names through
  `collect_plugin_state`; `SkillInfo.name` is display metadata and may be
  overridden by a Studio sidecar.
- All installed Skills remain present in every rollout. Only explicitly
  trainable Skills may be replaced in a candidate.
- Support files are copied into every snapshot and rollout but are not
  optimizer state. `plugin_hash()` includes Skill names, Skill content,
  support-file relative paths, and support-file bytes.
- `PluginTrainer.preflight()` runs before `_configure_models()`. It loads every
  split, normalizes Plugin metadata, validates complete held-out coverage, and
  rejects unsupported modes and invalid numeric settings.
- Model setup applies shared and role-specific Azure/OpenAI, Qwen, MiniMax,
  Codex exec, and Claude Code exec configuration before training.
- Failed scored trajectories are categorized as `routing`, `execution`,
  `handoff`, or `shared_dependency`. `task_failure` and `judge_failure` remain
  visible but have no responsible Skills and produce no reflection call.
- Responsible Skills are ordered by descending attributed failure count, then
  Plugin runtime order, and clipped by `max_skills_per_candidate`.
- Reflection, merge, ranking, and edit application run independently per
  selected Skill using only trajectories attributed to that Skill.
- A candidate is accepted only when the configured overall metric strictly
  improves and no covered Skill regresses more than
  `max_skill_regression`. The default `0.0` forbids any regression.
- Validation coverage is checked after `sel_env_num` is applied, so the actual
  gate set must cover every installed Skill.
- Complete snapshots are written below `plugin_versions/`. Runtime state points
  only to a completed snapshot. Resume requires the same ordered runtime names
  and trainable names and starts after `last_completed_step`.
- `best_plugin/` is deployable: it contains `manifest.json`, one directory per
  Skill, every `SKILL.md`, and frozen support files.
- Studio result parsing identifies Plugin mode from either `summary.json` or
  job params so a running Plugin job is not temporarily rendered as a
  single-Skill job.
- Plugin Train and Job Detail must have no document-level horizontal overflow
  at desktop and 390px widths. Wide navigation and metric tables may scroll
  only inside their bounded containers.

Artifact contract:

```text
out/
  config.json
  history.json
  runtime_state.json
  attribution.jsonl
  plugin_versions/plugin_v0000/<skill-name>/...
  steps/step_NNNN/
    attribution.json
    skills/<skill-name>/
    candidate_plugin/
    selection_eval/
    step_record.json
  best_plugin/manifest.json
  summary.json
```

### 4. Validation & Error Matrix

| Condition | Required result |
|---|---|
| Fewer than two `--skill` paths | Exit before model configuration |
| Missing/empty `SKILL.md`, unsafe name, or duplicate runtime name | Exit before model configuration |
| Unknown `--train-skill` | Exit before model configuration |
| Malformed or unknown task target | Exit before model configuration |
| Validation subset misses an installed Skill | Exit before model configuration |
| No trainable Skill | Exit before model configuration |
| `max_skills_per_candidate <= 0` | Reject; do not replace zero with the default |
| `max_skill_regression` outside `[0, 1]` | Reject |
| Rewrite, slow update, meta-Skill, or accumulation other than 1 | Reject |
| Rollout or judge error | Persist the error; do not reflect it |
| No eligible attribution or no applied edit | Record a skip; do not run candidate validation |
| Candidate overall score does not strictly improve | Reject |
| Any per-Skill regression exceeds the threshold | Reject with named reasons |
| Resume Skill order or trainable set differs | Reject resume |
| Snapshot support file or Skill content changes | Snapshot hash validation fails |
| Studio trainable ID is outside selected Plugin Skills | Reject command construction |
| Studio display name differs from runtime name | Emit the runtime name in `--train-skill` |

### 5. Good / Base / Bad Cases

- Good: six `cc-knowledge` Skills are installed, five are trainable, two are
  selected from attributed failures, and the complete six-Skill candidate is
  gated.
- Good: a candidate raises overall soft score and keeps every covered Skill at
  or above its previous score; it becomes the new `best_plugin/`.
- Base: `trainable_skill_ids` is omitted, so every selected Plugin Skill is
  trainable and the default candidate limit is two.
- Base: a batch has only rollout/judge failures; the step is recorded as
  `skip_no_attribution` without optimizer calls.
- Bad: local improvement of one Skill is accepted without evaluating the
  complete Plugin.
- Bad: a higher Plugin score hides a covered Skill regression above the
  configured threshold.
- Bad: Studio passes a sidecar display name as `--train-skill` instead of the
  frontmatter/path runtime name.

### 6. Tests Required

- State: distinct named documents, frozen unselected Skills, support-file copy,
  complete hash, snapshot round-trip, and tamper rejection.
- Attribution: every category, stable tie ordering, trainable filtering, and no
  gradients for task/judge failures.
- Gate: strict overall improvement, hard/soft/mixed projection, threshold
  boundaries, complete per-Skill coverage, accept, and reject reasons.
- Trainer: complete-Plugin baseline/candidate/test rollouts, accepted export,
  rejected regression, skip paths, and resume without replay.
- CLI: task metadata and held-out coverage failures occur before
  `_configure_models`; role-specific backend settings are applied.
- Studio: exact repeated `--skill` flags, runtime `--train-skill` names,
  trainable subset validation, stub job artifacts, mid-run Plugin mode, and
  per-Skill diffs.
- Frontend: `npm run build`, exact submitted IDs, all-Skills default selection,
  subset selection, desktop/390px screenshots, no document overflow, and
  Plugin metrics/timeline/diffs in Job Detail.

```bash
python3 -m pytest tests/test_plugin_training.py tests/test_skilleval.py tests/test_studio_runners.py -q
python3 -m pytest tests/ -q
python3 -m py_compile scripts/train_plugin.py skillopt/engine/plugin_trainer.py \
  skillopt/evaluation/plugin_gate.py skillopt/envs/skilleval/plugin.py
cd skillopt_studio/frontend && npm run build
git diff --check
```

### 7. Wrong vs Correct

#### Wrong

```python
train_argv += ["--train-skill", studio_skill.name]
```

The Studio display name may come from `.studio_sample.json` and is not the
runtime identity installed by SkillEval.

#### Correct

```python
state = collect_plugin_state([skill.path for skill in selected_skills])
runtime_names = dict(zip(selected_ids, state.names, strict=True))
train_argv += ["--train-skill", runtime_names[trainable_id]]
```

#### Wrong

```python
if candidate_skill_score > current_skill_score:
    accept(candidate_skill)
```

This ignores routing, handoff, and regressions in the rest of the Plugin.

#### Correct

```python
candidate_results = adapter.rollout_plugin(validation_items, candidate_state, out_dir)
gate = evaluate_plugin_gate(
    current_aggregates,
    aggregate_results(candidate_results, list(candidate_state.names)),
    list(candidate_state.names),
    max_skill_regression=cfg["max_skill_regression"],
)
```

Always validate the complete Plugin and enforce both the overall and per-Skill
conditions.
