# Studio Taskgen Expansion

## Scenario: Expand an existing task set with AI-generated tasks

### 1. Scope / Trigger

This contract applies when Studio creates a `taskgen` job that should add complementary tasks to an existing editable task set. It spans the React editor, generic Studio job API, runner-owned filesystem snapshot, `scripts/generate_tasks.py`, the isolated Codex/Claude exec workspace, and JobDetail recovery.

Keep this as an optional extension of normal task generation. Do not add a second model endpoint or write generated tasks directly to task-set persistence.

### 2. Signatures

Studio job request:

```http
POST /api/jobs
Content-Type: application/json

{
  "type": "taskgen",
  "params": {
    "taskset_id": "existing-set",
    "target_split": "train",
    "skill_id": "source--skill",
    "target_backend": "claude_code_exec",
    "count": 5
  }
}
```

The normal taskgen fields remain unchanged. `taskset_id` and `target_split` are optional as a pair.

Runner and generator signatures:

```python
build_taskgen_command(config: StudioConfig, params: dict, job_dir: Path) -> list[str]
load_existing_task_context(path: str, target_split: str) -> ExistingTaskContext
validate_generated_tasks(
    tasks: list[dict],
    requested_count: int,
    skills: list[SkillDocument],
    min_tasks_per_skill: int = 1,
    reserved_ids: frozenset[str] = frozenset(),
) -> None
```

Optional CLI arguments:

```text
scripts/generate_tasks.py ... \
  --existing-tasks <job_dir>/existing_tasks.json \
  --target-split train
```

Codex task generation must run with a workspace-local project root and no lifecycle hooks:

```text
codex exec -C <work_dir> \
  -c project_root_markers=[] \
  --disable hooks \
  ...
```

### 3. Contracts

Expansion job params:

| Field | Type | Contract |
|---|---|---|
| `taskset_id` | non-empty string | Existing, non-sample Studio task-set ID |
| `target_split` | non-empty string | `tasks` for single mode; an existing split or optional `test` for split mode |

Before queueing, `skillopt_studio/runners.py` writes this immutable UTF-8 snapshot under the reserved job directory:

```json
{
  "taskset_id": "existing-set",
  "target_split": "train",
  "tasks_by_split": {
    "train": [{"id": "task_001", "question": "...", "rubric": "..."}],
    "val": [{"id": "task_101", "question": "...", "rubric": "..."}]
  }
}
```

`tasks_by_split` must have canonical shape: exactly `{tasks}` for single mode, or `{train,val[,test]}` for split mode. The generator reserves every existing ID, including task summaries omitted from the prompt.

The existing-task prompt section is deterministic: target split first, remaining snapshot order afterward, at most `MAX_EXISTING_TASK_ITEMS`, at most `MAX_EXISTING_TASK_CONTEXT_CHARS`, and each displayed field bounded by `MAX_EXISTING_TASK_FIELD_CHARS`. Do not include unbounded `files` contents. The prompt must request semantically new coverage across all splits.

`generated_tasks.json` remains a task array. Expansion metadata in `gen_summary.json` is optional, so old jobs remain readable. Job params are the source of truth for frontend recovery.

Studio output directories normally live below the SkillOpt repository. A Codex target run must treat its generated workspace as the project root so it cannot discover parent `.codex/`, `AGENTS.md`, or unrelated repository Skills. Lifecycle hooks must also be disabled: a parent `UserPromptSubmit` hook may execute relative to the generated workspace, fail before the model turn, and still leave Codex with a zero exit code and an empty final response.

When the agent returns without writing `generated_tasks.json`, taskgen validation includes a bounded, single-line form of the final agent response when one exists. This preserves infrastructure explanations such as a failed Codex sandbox initialization instead of reducing every failure to “agent did not write”.

The frontend keeps generated tasks in `editSplits` only. It may persist them only through the existing full task-set Save action. JobDetail recovery passes router state with `appendGeneratedTasks`, `targetSplit`, and `sourceJobId`; TaskSetDetail reloads full data, validates the target, merges once, and clears router state.

Editable, non-sample TaskSetDetail pages expose `taskset-ai-expand-shortcut` in the `PageHeader` action group. The shortcut must call the full-data loader through an explicit wrapper such as `() => enterEdit({ openExpansion: true })`; normal Edit calls `() => enterEdit()`. Do not pass an options-accepting loader directly as a React click handler, because the click event would be interpreted as the options argument. Failed full-data loads must remain visible while the page is still outside edit mode.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Only one of `taskset_id` / `target_split` supplied | `ValueError` before job queue/model call |
| Task set missing | HTTP 400 from job creation with named task-set error |
| Task set is a read-only sample | HTTP 400; do not write a snapshot |
| Single mode target is not `tasks` | HTTP 400 before queueing |
| Split mode target is neither existing nor optional `test` | HTTP 400 before queueing |
| Snapshot is unreadable, malformed, noncanonical, or target mismatches CLI | CLI preflight failure before backend/model call |
| Generated ID is in `reserved_ids` | Validation failure enters the existing second-attempt feedback path |
| Parent repository has Codex hooks or instructions | The exec command stops parent project discovery and disables hooks |
| Codex reports a successful process exit but a hook blocked the turn | The harness raises an explicit blocked-hook error and persists its trace |
| Agent writes no output but returns a diagnostic response | Validation and retry feedback include a bounded form of that response |
| Host cannot initialize the configured Codex sandbox | The diagnostic reaches the taskgen job log; fix the host sandbox instead of silently weakening it |
| Residual collision during frontend merge | Allocate the next available `task_NNN` across all splits; never overwrite |
| Job result target does not match current task set/draft mode | Show a localized error and leave the draft untouched |
| Empty/non-taskgen result | Show an explicit error; do not silently append or save |
| Sample/read-only detail page | Do not render the header expansion shortcut |
| Header shortcut full-data load fails | Keep preview mode and show the load error outside the edit-only card |

### 5. Good / Base / Bad Cases

- **Good expansion:** split task set targets `train`; the snapshot contains train/val/test, prompt shows train first, generated unique tasks append to the current train draft, and Save performs the only persistence write.
- **Good optional split:** split task set has train/val and targets `test`; the draft creates `test` when generated tasks are merged.
- **Base compatibility:** ordinary taskgen omits both expansion fields; no snapshot or new CLI flags are produced, and JobDetail retains “import as new task set”.
- **Bad request:** only `taskset_id` is supplied; command construction fails and the reserved empty job directory is cleaned up.
- **Bad generation:** the first result reuses `task_001`; validation feedback requests a complete rewrite, and only a collision-free retry succeeds.
- **Bad host hook:** a repository-level `UserPromptSubmit` hook would resolve its command relative to `gen_workspace`; Codex isolation prevents it from running.
- **Bad host sandbox:** Codex replies that its workspace sandbox failed to initialize and writes no file; taskgen includes that reply in the validation failure so the operator can repair bwrap/AppArmor.
- **Bad recovery:** stale job params point to another task set; TaskSetDetail reports an invalid restore target and does not mutate `editSplits`.

### 6. Tests Required

Generator tests in `tests/test_generate_tasks.py` must assert:

- strict snapshot decoding and all-or-none CLI flags happen before the backend stub is called;
- target-first ordering, item/character budgets, and oversized metadata bounds;
- IDs from displayed and omitted tasks remain reserved;
- collision feedback reaches the retry and expansion summary metadata is written;
- a missing output file preserves a bounded agent diagnostic in retry/final errors;
- prompts without expansion context remain compatible.

Exec harness tests in `tests/test_exec_usage.py` must assert:

- non-judge Codex CLI argv contains `project_root_markers=[]` and `--disable hooks`;
- a zero-exit transcript that reports a blocked hook raises an explicit error and persists the raw trace.

Studio tests in `tests/test_studio_runners.py` must assert:

- single, split, and optional-test snapshots contain all splits and correct CLI flags;
- missing, sample, wrong-mode/wrong-split, and partial-pair inputs fail before queueing;
- ordinary taskgen produces no expansion snapshot/flags;
- an end-to-end stub taskgen job preserves expansion params and exposes generated results.

Frontend contract changes require `npm run build`, matching English/Simplified Chinese locale keys, stable expansion/recovery test IDs, and review of polling idempotency plus draft-only persistence.

### 7. Wrong vs Correct

#### Wrong

```python
# Loses reproducibility and lets the CLI race with later task-set edits.
argv += ["--taskset-id", params["taskset_id"]]
```

```python
# Lets a job-local Codex run inherit parent hooks, AGENTS.md, and unrelated Skills.
cmd = ["codex", "exec", "-C", work_dir]
```

```tsx
// Bypasses human review and the task-set full-replace validation boundary.
await api.updateTaskset(id, { tasks_by_split: mergedGeneratedTasks });
```

#### Correct

```python
# Resolve and validate synchronously, then pass an immutable job-local snapshot.
snapshot_path, target_split = _materialize_taskgen_expansion(config, params, job_dir)
argv += ["--existing-tasks", str(snapshot_path), "--target-split", target_split]
```

```python
# Keep the generated workspace isolated while retaining user-level provider/auth config.
cmd = [
    "codex", "exec", "-C", work_dir,
    "-c", "project_root_markers=[]",
    "--disable", "hooks",
]
```

```tsx
// Generated tasks remain draft state until the existing Save action is used.
setEditSplits((current) =>
  current ? mergeGeneratedTasks(current, targetSplit, results.tasks) : current,
);
```

This separation preserves normal taskgen behavior, job reproducibility, human review, and atomic task-set persistence.
