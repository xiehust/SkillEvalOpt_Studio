# Error Handling

## Error Boundaries

SkillOpt uses different failure behavior at configuration, per-task, API, and
background-worker boundaries.

| Boundary | Required behavior |
|---|---|
| Config, dataset, and CLI preflight | Raise immediately with an actionable `ValueError` before model calls |
| One rollout or judge item | Isolate the item and persist `error` or `judge_error` on its result |
| Studio API | Translate expected domain errors into a stable HTTP status and `detail` string |
| Studio job process | Persist terminal status, exit code, and error; keep the worker alive |
| Optional convenience feature | Log a warning and continue only when the core operation remains valid |

The guiding rule is fail fast before expensive work, but do not let one
independent task erase the rest of a batch.

## Validation Errors

Use built-in exception types unless a stable custom hierarchy is needed:

- `ValueError` for malformed values, unsupported modes, unsafe paths, and
  inconsistent combinations.
- `KeyError` for a missing resource in manager/service code.
- `RuntimeError` for an operation that could not reach its promised lifecycle
  state.
- `ImportError` with context when an optional dependency is required by the
  selected environment.

Messages must name the field or item and include the invalid value when safe.
`skillopt/envs/skilleval/dataloader.py` is the reference: errors identify
`item #N`, include the task ID when present, and explain the path or field
contract.

Parse helpers should remove noisy parser context when it adds no value:

```python
try:
    number = int(value)
except (TypeError, ValueError):
    raise ValueError(f"{key} must be an integer, got {value!r}") from None
```

At an API boundary, preserve the cause:

```python
except ValueError as exc:
    raise HTTPException(status_code=400, detail=str(exc)) from exc
```

References:

- `skillopt_studio/runners.py::_validated_int`
- `skillopt_studio/api/jobs.py::create_job`
- `skillopt/envs/skilleval/dataloader.py::_normalize_items`

## Per-Task Failures

Rollout and judge failures are scored as failures but remain inspectable.
`skillopt/envs/skilleval/rollout.py::_rollout_one()` initializes a result row,
catches the task exception, and stores:

```python
result["error"] = f"{type(exc).__name__}: {exc}"
result["error_traceback"] = traceback.format_exc(limit=5)
```

An empty agent response is also an explicit error, with extracted backend
failure detail when available. Judge parse/call failures use `judge_error`;
they do not masquerade as a normal rubric rejection.

Maintain these distinctions:

- A valid response that fails the rubric: `hard=0`, normal judge reason.
- Target execution failure: `error`.
- Judge execution or parse failure: `judge_error`.
- Deliberately skipped judge because the target response is empty:
  `judge_skipped="empty_response"`.

Do not drop errored rows or return an empty reason. Training code may exclude a
failed row from gradient generation, but the task ID and reason must remain in
history/results.

## Studio Jobs

`skillopt_studio/jobs.py::JobManager` owns job lifecycle failures:

- Spawn failure sets `status="failed"` and `error="failed to spawn: ..."`.
- Non-zero exit sets `status="failed"`, records `exit_code`, and stores a
  concise process-exit error.
- Unexpected worker exceptions are caught at the worker loop, written to the
  job record with `repr(exc)`, and do not terminate the worker thread.
- Cancellation owns a distinct `cancelled` terminal state.

The subprocess stdout and stderr are combined into `log.txt`; the structured
job record remains the source of truth for status.

Command construction and preflight happen before `jobs.create_job()`. If
validation fails after reserving a job directory, the API removes that empty
reservation and returns HTTP 400 instead of queueing a job that is guaranteed
to fail.

## API Error Mapping

Studio uses FastAPI's normal `{"detail": ...}` response shape:

- `400` for invalid request combinations or an invalid lifecycle operation.
- `404` for a missing job, task set, Skill, or artifact.
- Framework/Pydantic validation handles malformed request bodies.

Keep domain helpers independent of FastAPI. They raise Python exceptions;
routers map those exceptions to HTTP responses.

## Narrow Recovery

Broad catches are acceptable only at explicit isolation boundaries:

- `JobManager._worker_loop()` must keep accepting later jobs.
- A single rollout must not abort unrelated items.
- Studio sample materialization is optional and logs a warning with traceback.
- Lazy imports in CLI environment registration skip unavailable optional
  benchmark dependencies.

Do not use `except Exception: pass` around validation, result persistence, or a
required training stage. If fallback is intentional, document why it is safe
and expose the failure through a log, result field, or status record.

## Tests

Error-path tests should assert both control flow and observability:

- Validation occurs before the backend/model stub is called.
- The message identifies the bad field, item, or resource.
- Per-task failures remain in result artifacts.
- Studio failures reach a terminal state and expose an error.
- HTTP status and `detail` match the domain error.

Representative tests:

- `tests/test_skilleval.py::TestJudge`
- `tests/test_searchqa_rollout_failfast.py`
- `tests/test_studio_core.py`
- `tests/test_studio_runners.py`
- `tests/test_plugin_training.py`
