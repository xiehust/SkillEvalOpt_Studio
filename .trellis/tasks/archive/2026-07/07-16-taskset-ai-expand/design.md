# Design: AI expansion in task-set editing

## Boundaries

The feature extends the existing taskgen pipeline rather than adding a model API. Studio remains an orchestration layer: the React editor creates and observes a normal `taskgen` job, `skillopt_studio/runners.py` validates/materializes expansion context, and `scripts/generate_tasks.py` owns prompt construction and generated-task validation.

## Contracts

### Optional job params

A taskgen expansion adds both fields:

```json
{
  "taskset_id": "existing-set",
  "target_split": "train"
}
```

They are an all-or-none pair. Ordinary taskgen requests omit both and retain current behavior. `target_split` must be `tasks` for single mode, or an existing split / optional `test` for split mode. Sample task sets are rejected as expansion targets at the backend boundary as defense in depth.

### Snapshot artifact

During command construction, the runner reads the full task set and writes a UTF-8 JSON input file under the reserved job directory. The payload records `taskset_id`, `target_split`, and `tasks_by_split`. This happens before queueing, making generation reproducible even if the task set changes while waiting. The subprocess receives the snapshot via optional `--existing-tasks` and `--target-split` arguments.

### Generated output

`generated_tasks.json` remains a plain task array. Optional expansion metadata may be added to `gen_summary.json`; old readers tolerate its absence. Job params remain the frontend source of truth for the return target.

## Data flow

1. User enters full task-set edit mode and opens the AI expansion panel.
2. Single mode fixes the target to `tasks`; split mode lets the user choose an existing split or create/select `test`.
3. The shared `GenerateTaskSetForm` submits the existing taskgen fields plus expansion params. An optional callback keeps the page mounted instead of navigating.
4. Runner validates the target and writes the full task-set snapshot before queueing the normal subprocess job.
5. The generator orders snapshot context with the target split first, serializes only evaluation-relevant fields, and caps prompt context at fixed item/character budgets. All snapshot IDs remain reserved even if some task text is omitted from the prompt.
6. Generated ID overlap fails validation and feeds the existing second attempt. Normal task validation remains unchanged.
7. The editor polls the job. On success it fetches results exactly once and appends them to the current `editSplits` state. A pure merge helper tracks IDs across all splits and renames residual collisions with `nextTaskId`.
8. Persistence changes only when the user presses the existing Save button. Cancel leaves the backend unchanged.
9. JobDetail detects expansion params and offers “append to original task set”. It routes `tasks`, target split, and job ID to TaskSetDetail; that page loads the latest full task set, merges once, clears router state, and opens edit mode.

## UI states and failures

- One expansion job may be active in an editor at a time; its queued/running/succeeded/failed/cancelled state and a JobDetail link remain visible.
- Editing and Save stay available while generation runs. Leaving the page does not cancel the job.
- API/job/result failures are shown without changing the draft. A failed job can be inspected through its detail link.
- Result application is idempotent per job ID in the mounted page, preventing polling from appending twice.
- An empty or non-taskgen result is an explicit UI error, not a silent no-op.

## Compatibility and rollback

All new CLI flags and job fields are optional. Existing new-task-set generation, old job records, and output JSON schemas continue to work. Rollback consists of removing the optional expansion UI/params and optional CLI context handling; persisted task sets need no migration.

## Test strategy

- Generator unit tests: context ordering/capping, reserved-ID failure, retry feedback compatibility, no-context prompt compatibility.
- Studio runner/API tests: valid single/split snapshots, invalid pair/split/sample/not-found errors before queueing, argv and snapshot content, ordinary taskgen unchanged.
- Studio end-to-end taskgen tests: stub CLI accepts optional flags and returns generated results with expansion params preserved.
- Frontend: TypeScript/Vite production build; stable test IDs support browser-level follow-up testing.
