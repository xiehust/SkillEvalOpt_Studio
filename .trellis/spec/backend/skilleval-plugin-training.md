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
    modified_skill_names: list[str] | tuple[str, ...] | None = None,
) -> PluginGateResult

minimum_plugin_task_count(skill_count: int) -> int
plan_disjoint_plugin_coverage(
    items: list[dict],
    required_training_skills: list[str] | tuple[str, ...],
    required_validation_skills: list[str] | tuple[str, ...],
) -> PluginCoveragePlan
```

Task generation:

```bash
python3 scripts/generate_tasks.py \
  --skill <plugin/skills/alpha> \
  --skill <plugin/skills/beta> \
  --count 5 \
  --min-tasks-per-skill 2 \
  --out_root <output>
```

Studio train request:

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

Studio coverage request and response:

```text
POST /api/tasksets/{taskset_id}/plugin-coverage
```

```json
{
  "skill_ids": ["<studio-id-1>", "<studio-id-2>"],
  "trainable_skill_ids": ["<studio-id-1>"],
  "plugin": "cc-knowledge",
  "split_ratio": "4:3:3"
}
```

```json
{
  "valid": true,
  "mode": "single",
  "total_count": 5,
  "generation_minimum_count": 3,
  "minimum_tasks_per_skill": 2,
  "skills": [
    {
      "skill_id": "<studio-id-1>",
      "skill_name": "alpha",
      "count": 2,
      "required": 2,
      "train_count": null,
      "validation_count": null
    }
  ],
  "reasons": []
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
  split, normalizes Plugin metadata, validates training and held-out coverage
  for every trainable Skill, and rejects unsupported modes and invalid numeric
  settings.
- Model setup applies shared and role-specific Azure/OpenAI, Qwen, MiniMax,
  Codex exec, and Claude Code exec configuration before training.
- Failed scored trajectories are categorized as `routing`, `execution`,
  `handoff`, or `shared_dependency`. `task_failure` and `judge_failure` remain
  visible but have no responsible Skills and produce no reflection call. Their
  attribution rows and step history preserve `task_id`, `target_skills`, and
  the normalized `error` / `judge_error` as `reason`.
- Responsible Skills are ordered by descending attributed failure count, then
  Plugin runtime order, and clipped by `max_skills_per_candidate`.
- Reflection, merge, ranking, and edit application run independently per
  selected Skill using only trajectories attributed to that Skill.
- A candidate is accepted only when the configured overall metric strictly
  improves and no trainable Skill regresses more than
  `max_skill_regression`. The default `0.0` forbids any regression. The trainer
  also passes the Skills whose content actually changed; at least one modified
  Skill must strictly improve its own held-out metric.
- `modified_skill_names=None` preserves the pure gate's legacy behavior for
  direct callers. `PluginTrainer` always supplies actual modified names when it
  evaluates a changed candidate.
- Every step persists `excluded_failures`, containing rollout/judge failures
  that could not produce gradients. Studio projects the optional field,
  totals it, and displays concrete reasons while old history remains readable.
- Validation coverage is checked after `sel_env_num` is applied, so the actual
  gate set must cover every trainable Skill. Installed frozen Skills remain in
  every rollout but do not require attributed validation tasks or receive
  dedicated regression metrics.
- Ratio-mode SkillEval splits greedily reserve deterministic, disjoint
  validation and training task covers for every trainable Skill. Before
  choosing the covers, they try each shuffled task in order as a reserved test
  candidate so a greedy tie cannot consume every task when another valid
  partition exists. A source needs at least two distinct target tasks per
  trainable Skill. The two covers plus one test task are minimum counts;
  remaining tasks are distributed using the requested ratio.
  `split_manifest.json` records
  `required_training_skills`, `required_validation_skills`,
  `minimum_training_count`, `minimum_validation_count`, actual counts, and
  `coverage_aware: true`.
- Explicit split directories receive the same train coverage and effective
  validation coverage checks. `sel_env_num` is applied before the latter check.
- `skillopt.envs.skilleval.coverage` owns
  `PLUGIN_MIN_TASKS_PER_SKILL=2`, `PLUGIN_TEST_RESERVE=1`, task counting, and
  disjoint-cover feasibility. The dataloader, Studio analyzer, and task
  generator consume this shared contract; frontend code must not redefine the
  constants.
- Task sets remain reusable and do not persist a Plugin or trainable-Skill
  binding. Upload/manual creation shows the server-owned minimum as guidance;
  the actual selected trainable Skills are resolved and checked again for each
  training request.
- Studio Plugin task generation raises the effective count to
  `2 * selected_skill_count + 1`, passes `--min-tasks-per-skill 2`, and shows
  that effective count before submission. Direct CLI callers keep the legacy
  default of one unless they pass the stricter flag.
- Multi-Skill generation prompts require the exact per-Skill quota.
  Post-generation validation reports `skill=actual/required`; a failed attempt
  is cleared before retry so the final attempt cannot accidentally publish a
  previously loaded invalid task list.
- `/api/environment.taskgen` exposes the minimum-per-Skill and test-reserve
  values. The Plugin Train form requests a typed coverage report whenever the
  Plugin, trainable subset, task set, or ratio changes, ignores stale
  responses, and disables submission while the current report is loading or
  invalid.
- `build_train_command()` invokes the same analyzer before writing a job
  command. Direct API callers therefore receive a synchronous `400` and no job
  is queued when coverage is invalid; `PluginTrainer.preflight()` remains the
  final CLI guard.
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
    step_record.json              # includes optional excluded_failures[]
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
| Ratio source has fewer than two distinct tasks for a trainable Skill | Exit before model configuration and report `name=actual/2` |
| Ratio dual coverage would leave test empty | Exit before model configuration |
| Generated Plugin task set under-covers a selected Skill | Retry with `name=actual/required`; fail after the final invalid attempt |
| Studio Plugin generation count is below `2 * Skills + 1` | Raise the effective command count and pass the per-Skill minimum |
| Studio coverage report is loading, stale, or invalid | Disable Train submission and show per-Skill counts/reasons |
| Direct Studio train request has invalid Plugin coverage | Return `400` before queue creation |
| Training split misses a trainable Skill | Exit before model configuration and name the missing training coverage |
| Validation subset misses a trainable Skill after `sel_env_num` | Exit before model configuration |
| Validation has no task for an installed frozen Skill | Continue; the Skill remains installed but has no dedicated regression metric |
| Coverage-aware validation would leave train or test empty | Exit before model configuration |
| No trainable Skill | Exit before model configuration |
| `max_skills_per_candidate <= 0` | Reject; do not replace zero with the default |
| `max_skill_regression` outside `[0, 1]` | Reject |
| Rewrite, slow update, meta-Skill, or accumulation other than 1 | Reject |
| Rollout or judge error | Persist the error; do not reflect it |
| No eligible attribution or no applied edit | Record a skip; do not run candidate validation |
| Candidate overall score does not strictly improve | Reject |
| Any per-Skill regression exceeds the threshold | Reject with named reasons |
| Overall improves but no modified Skill improves | Reject with each modified Skill's before/after score |
| Resume Skill order or trainable set differs | Reject resume |
| Snapshot support file or Skill content changes | Snapshot hash validation fails |
| Studio trainable ID is outside selected Plugin Skills | Reject command construction |
| Studio display name differs from runtime name | Emit the runtime name in `--train-skill` |

### 5. Good / Base / Bad Cases

- Good: six `cc-knowledge` Skills are installed, five are trainable, two are
  selected from attributed failures, and the complete six-Skill candidate is
  gated.
- Good: a frozen installed Skill has no attributed task; the runtime still
  installs it while coverage and per-Skill gate metrics use the five trainable
  Skills.
- Good: a candidate raises overall soft score and keeps every covered Skill at
  or above its previous score, and at least one modified Skill improves; it
  becomes the new `best_plugin/`.
- Good: a target rollout stalls; the step remains process-successful, records
  the task and reason in `excluded_failures`, and does not reflect that task.
- Good: six selected Skills make Studio generate at least 13 tasks and require
  each Skill in two distinct `target_skills` arrays.
- Good: a reusable task set is valid for one trainable subset and rejected for
  another; the report is computed from the current selection rather than
  stored ownership metadata.
- Base: `trainable_skill_ids` is omitted, so every selected Plugin Skill is
  trainable and the default candidate limit is two.
- Base: direct multi-Skill generation omits `--min-tasks-per-skill` and retains
  the legacy one-task-per-Skill validation.
- Base: a batch has only rollout/judge failures; the step is recorded as
  `skip_no_attribution` without optimizer calls.
- Bad: local improvement of one Skill is accepted without evaluating the
  complete Plugin.
- Bad: a higher Plugin score hides a trainable Skill regression above the
  configured threshold.
- Bad: an edit is accepted only because an unmodified Skill's one-sample score
  moved while every modified Skill stayed flat.
- Bad: validation coverage consumes the only task for a trainable Skill,
  leaving that Skill permanently absent from training.
- Bad: frontend code hard-codes `2` while the backend exports a different
  minimum, or enables Train using a coverage response for an older selection.
- Bad: Studio passes a sidecar display name as `--train-skill` instead of the
  frontmatter/path runtime name.

### 6. Tests Required

- State: distinct named documents, frozen unselected Skills, support-file copy,
  complete hash, snapshot round-trip, and tamper rejection.
- Attribution: every category, stable tie ordering, trainable filtering, no
  gradients for task/judge failures, and persisted target/reason details.
- Gate: strict overall improvement, hard/soft/mixed projection, threshold
  boundaries, modified-Skill relevance, trainable per-Skill coverage,
  frozen-Skill exclusion, accept, and reject reasons.
- Trainer: complete-Plugin baseline/candidate/test rollouts, accepted export,
  rejected trainable regression, deterministic disjoint train/validation
  coverage, alternate test reservations, insufficient source coverage,
  explicit-split coverage, `sel_env_num` truncation, excluded failures, skip
  paths, and resume without replay.
- CLI: task metadata and trainable held-out coverage failures occur before
  `_configure_models`; missing frozen-Skill coverage is accepted; role-specific
  backend settings are applied.
- Studio: exact repeated `--skill` flags, runtime `--train-skill` names,
  trainable subset validation, environment minimums, ratio/split coverage
  reports, pre-queue invalid rejection, normalized taskgen counts, stub job
  artifacts, mid-run Plugin mode, and per-Skill diffs plus excluded-failure
  projection.
- Generator: legacy minimum one, strict minimum two, exact
  `name=actual/required` retry feedback, and two invalid attempts exiting
  nonzero without publishing an invalid artifact.
- Frontend: `npm run build`, exact submitted IDs, all-Skills default selection,
  subset selection, upload/manual guidance, six-Skill effective count 13,
  current-response-only coverage status, invalid-submit guard, desktop/390px
  screenshots, no document overflow, and Plugin metrics/timeline/diffs in Job
  Detail.

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
    list(candidate_state.trainable_names),
    max_skill_regression=cfg["max_skill_regression"],
    modified_skill_names=modified_skill_names,
)
```

Always run the complete Plugin, enforce the overall condition across all
validation tasks, and enforce dedicated regression conditions for trainable
Skills. Supplying actual modified names prevents unrelated validation movement
from accepting an ineffective edit.

#### Wrong

```typescript
const minimum = selectedSkillIds.length * 2 + 1;
```

This duplicates backend policy and can drift from command validation.

#### Correct

```typescript
const environment = await api.environment();
const minimum =
  selectedSkillIds.length * environment.taskgen.plugin_min_tasks_per_skill
  + environment.taskgen.plugin_test_reserve;
```

Render server-owned constraints for guidance, but always let the backend
normalize task generation and reject invalid training before queue creation.
