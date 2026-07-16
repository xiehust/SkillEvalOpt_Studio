# Implementation plan

1. **Generator context contract**
   - Add optional existing-task snapshot CLI arguments to `scripts/generate_tasks.py`.
   - Parse and strictly validate the snapshot before any backend call.
   - Build target-first, bounded prompt context and reserve all existing IDs.
   - Extend generated-task validation so collisions enter the existing retry feedback path.
   - Add focused tests in `tests/test_generate_tasks.py`.

2. **Studio taskgen orchestration**
   - Extend `build_taskgen_command` to treat `taskset_id` + `target_split` as an all-or-none expansion context.
   - Validate task-set existence, mutability, mode/split compatibility, and write the immutable snapshot under `job_dir` before queueing.
   - Pass optional CLI flags while leaving ordinary taskgen argv unchanged.
   - Update runner/API and stub-CLI tests in `tests/test_studio_runners.py`.

3. **Reusable generation form and editor merge**
   - Parameterize `GenerateTaskSetForm` with optional extra job params and an `onJobCreated` callback; preserve default navigation behavior.
   - Add an AI expansion panel to `TaskSetDetail`, including target split selection and one-active-job state.
   - Poll job status, fetch taskgen results once, and merge into the live draft without blocking edits.
   - Reuse `nextTaskId` over all splits for deterministic collision fallback.
   - Consume JobDetail restore router state only after full task data loads, then clear it to prevent replay.

4. **Job result recovery and localization**
   - Extend `TaskgenResultsView` with expansion metadata and an “append to original task set” action while retaining “import as new”.
   - Add English and Simplified Chinese strings in taskset/job/wizard locales as appropriate.
   - Add stable `data-testid` hooks for expansion form, status, and restore action.

5. **Validation and review**
   - Run focused tests: `python3 -m pytest tests/test_generate_tasks.py tests/test_studio_runners.py -q`.
   - Run changed Python syntax checks if Ruff is unavailable.
   - Run `cd skillopt_studio/frontend && npm run build`.
   - Run full suite: `python3 -m pytest tests/ -q`.
   - Run `git diff --check`, inspect the full cross-layer data flow, and fix all findings.

6. **Task-set detail header shortcut**
   - Add an editable-only “AI expand” action to the `PageHeader` right-side button group.
   - Reuse the full-data edit loader with an explicit `openExpansion` option; normal Edit remains unchanged, while the shortcut enters edit mode with the existing expansion panel open.
   - Add a stable `taskset-ai-expand-shortcut` selector, reuse localized expansion copy, and verify sample task sets never render the shortcut.
   - Re-run the frontend production build and focused backend compatibility tests, then `git diff --check`.

## Risk and rollback points

- Keep ordinary taskgen tests green after steps 1–2; if compatibility breaks, revert optional CLI/runner branches before touching frontend.
- Do not persist generated items from polling callbacks. The existing task-set Save endpoint remains the only write boundary.
- If polling/UI state proves unstable, JobDetail recovery remains a functional fallback, but the task is not complete until the in-page flow is verified.
