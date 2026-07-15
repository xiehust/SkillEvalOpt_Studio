# Harden Multi-Skill Plugin Training: Design

## Scope

This change tightens the existing Plugin training path without changing
single-Skill training or Plugin evaluation. The implementation remains one
task because task partitioning, trainer preflight, gate decisions, persisted
history, Studio API projection, and Job Detail rendering form one versioned
data flow.

## Data Flow

```text
raw Plugin tasks
  -> Studio creation guidance / AI generation quota
  -> dynamic task-set coverage report
  -> SkillEvalDataLoader dual-coverage ratio partition
  -> PluginTrainer preflight validates actual train + effective validation
  -> complete-Plugin train rollout
  -> FailureAttribution with excluded-failure reason
  -> candidate edits + complete-Plugin validation
  -> gate checks overall, regressions, and modified-Skill improvement
  -> history.json / summary.json
  -> Studio train_summary projection
  -> typed Job Detail timeline
```

## Dual-Coverage Partition

`SkillEvalDataLoader` will treat the trainable Skill list as required coverage
for both train and validation.

1. Build each task's set of required `target_skills`.
2. Reject source data when any required Skill appears in fewer than two
   distinct tasks. One task cannot be shared by disjoint train and validation
   splits.
3. Try each shuffled task in deterministic order as a reserved test candidate.
4. Greedily choose a validation cover from the other tasks. A task is eligible
   only when removing it leaves at least one remaining source task for every
   required training Skill.
5. Greedily choose a training cover from the remaining tasks. Keep the first
   reservation that yields both covers; reject only if no reservation works.
6. Treat the two coverage sets plus one test slot as minimum counts. Distribute
   remaining tasks according to the requested ratio, preserving disjointness.

The split manifest adds:

```json
{
  "coverage_aware": true,
  "required_training_skills": ["alpha", "beta"],
  "required_validation_skills": ["alpha", "beta"],
  "minimum_training_count": 2,
  "minimum_validation_count": 2
}
```

The algorithm is deterministic for a fixed source order, split seed, ratio,
and trainable Skill order. Ratio counts are targets after mandatory coverage,
not exact quotas.

## Preflight Validation

`PluginTrainer.preflight()` remains the final fail-fast owner:

- Set both required training and validation Skill names before adapter setup.
- Validate `dataloader.train_items` coverage.
- Apply `sel_env_num`, then validate effective validation coverage.
- Name the split and missing Skills in each error.
- Run all coverage checks before model configuration.

This also protects explicit `split_dir` inputs, which bypass ratio
materialization.

## Failure Attribution Contract

`FailureAttribution` gains backward-compatible fields with defaults:

```python
target_skills: tuple[str, ...] = ()
reason: str | None = None
```

For `task_failure` and `judge_failure`, attribution preserves normalized target
Skills and the concrete `error` or `judge_error`, while responsibility remains
empty and `gradient_eligible` remains false.

Each step writes an `excluded_failures` array derived from attribution rows.
It contains only infrastructure/judge failures:

```json
{
  "task_id": "task_003",
  "category": "task_failure",
  "target_skills": ["webnovel-plan"],
  "reason": "claude_code_exec failed: Response stalled mid-stream"
}
```

The field is optional for old artifacts. Studio projects it through
`_STEP_FIELDS`, computes a total excluded-failure count, and renders both a
summary badge and per-step details.

## Gate Relevance

`evaluate_plugin_gate()` accepts optional `modified_skill_names`. The trainer
always supplies the actual content changes, derived by comparing current and
candidate Plugin states.

The gate accepts only when all are true:

1. Candidate overall metric strictly improves.
2. No trainable Skill exceeds `max_skill_regression`.
3. At least one modified Skill strictly improves its own metric.

Neutral modified Skills are allowed. Unknown or empty modified names are
rejected at the trainer boundary; direct legacy callers that omit the new
optional argument retain the old pure-function behavior.

The persisted rejection reason lists each modified Skill's before/after score
when none improves.

## Shared Coverage Contract

`skillopt.envs.skilleval.coverage` owns the Plugin task-count constant and the
pure disjoint-cover planner. `SkillEvalDataLoader`, Studio coverage reporting,
and AI generation validation consume that contract instead of restating it.

```python
PLUGIN_MIN_TASKS_PER_SKILL = 2
PLUGIN_TEST_RESERVE = 1

minimum_plugin_task_count(skill_count) -> int
plan_disjoint_plugin_coverage(
    items,
    required_training_skills,
    required_validation_skills,
) -> PluginCoveragePlan
```

The conservative generation minimum is `2 * skill_count + 1`. Multi-target
tasks can make a smaller data set mathematically feasible, but generation uses
the conservative count to avoid optimizing toward fragile all-integration
sets.

## Task Generation

Studio's environment response exposes the server-owned minimums. In Plugin
mode the frontend derives and displays the effective count and raises the
editable count when the selected Skill set grows. The command builder repeats
that normalization so direct API callers cannot bypass it.

`scripts/generate_tasks.py` gains an optional `--min-tasks-per-skill`. Its
default remains one for direct CLI compatibility. Studio passes two. Both the
prompt and `validate_generated_tasks()` use the same argument; validation
reports `name=actual/required` and the existing second attempt receives that
feedback.

## Studio Coverage Report

Task sets do not persist Plugin ownership. Studio computes a report from:

```json
{
  "taskset_id": "tasks-id",
  "skill_ids": ["installed-skill-id"],
  "trainable_skill_ids": ["trainable-skill-id"],
  "plugin": "plugin-name",
  "split_ratio": "4:3:3"
}
```

The response contains `valid`, mode, total/minimum counts, per-Skill source
counts, split counts when applicable, and concrete reasons. Runtime Skill names
are resolved from Plugin state, not Studio display names.

The Train form requests this report only after all required selections exist,
discards stale responses when selections change, renders per-Skill status, and
disables submission while coverage is loading or invalid. The train command
builder invokes the same report synchronously before writing config or queuing
the job.

## Compatibility

- Existing `history.json` rows without `excluded_failures` remain readable.
- Existing direct callers of `evaluate_plugin_gate()` can omit
  `modified_skill_names`.
- Existing split manifests remain readable; new manifests add fields.
- Frozen Skills stay installed but do not require dedicated coverage.
- `plugin_diffs` continues to contain only non-empty diffs.
- Existing task sets gain no new persisted metadata.
- Single-Skill generation and direct multi-Skill CLI generation retain their
  existing defaults.

## Risk And Rollback

- Dual coverage can reject task sets that previously ran but could never train
  all selected Skills. This is intentional fail-fast behavior.
- Small task sets will need at least two distinct tasks per trainable Skill and
  enough total tasks to retain test.
- If the gate relevance rule proves too strict, it can be disabled at the
  single trainer call site by omitting `modified_skill_names`; persisted data
  remains compatible.
- The conservative AI count can increase generation cost. The UI shows the
  effective count before submission, and the existing upper bound remains
  explicit.
