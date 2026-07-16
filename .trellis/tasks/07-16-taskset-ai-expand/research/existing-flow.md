# Existing taskgen and task-set edit flow

## Confirmed code paths

- `skillopt_studio/frontend/src/pages/TaskSetDetail.tsx`: entering edit loads `?full=1`; local `editSplits` is the unsaved source of truth; save performs full replacement through `api.updateTaskset`.
- `skillopt_studio/frontend/src/components/GenerateTaskSetForm.tsx`: owns existing Skill/Plugin, backend, model, count, guidance, timeout fields; currently creates a `taskgen` job and always navigates to `/jobs/{id}`.
- `skillopt_studio/frontend/src/pages/JobDetail.tsx::TaskgenResultsView`: reads standard `TaskItem[]`; currently only routes results to `/tasksets` as a new manual task set.
- `skillopt_studio/api/jobs.py::create_job`: reserves `job_dir`, calls the runner synchronously, and only then writes/queues the job. A task-set context snapshot can therefore be materialized by `build_taskgen_command`; validation failure removes the reservation and returns HTTP 400.
- `skillopt_studio/runners.py::build_taskgen_command`: resolves Skills/backend and constructs `scripts/generate_tasks.py` argv. It is the correct Studio owner for validating `taskset_id` + `target_split` and writing the immutable input snapshot.
- `scripts/generate_tasks.py`: existing prompt + strict task validation + one retry; adding optional existing-task input preserves the normal generation path.
- `skillopt_studio/frontend/src/components/TaskItemsEditor.tsx::nextTaskId`: existing helper for safe `task_NNN` allocation; expansion merge should reuse it over all split items.

## Decisions

- Reuse the current taskgen job and Claude Code/Codex backends; do not create a second AI endpoint.
- Keep the editor mounted and poll the submitted job; append successful results to the current draft, never directly to persistence.
- Persist `taskset_id` and `target_split` in job params so JobDetail can restore the append flow after navigation/refresh.
- Snapshot all splits at job creation, ordered target split first in the prompt and capped by deterministic item/character budgets.
- Validate generated IDs against every ID in the full snapshot. Retry through the existing feedback path, then retain a frontend merge-time fallback that renames any residual collision.

## Compatibility constraints

- Taskgen jobs without expansion params must produce the same argv/prompt behavior as before.
- New job params and summary fields are optional so old job records remain readable.
- Sample task sets remain read-only.
- No real model/CLI calls in tests.
