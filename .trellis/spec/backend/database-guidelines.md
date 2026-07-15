# Persistence And Artifact Storage

## Storage Model

This repository has no database, ORM, migration framework, or transactional
service. Runtime state is filesystem-backed:

| Data | Format and owner |
|---|---|
| Experiment configuration | YAML under `configs/`, parsed by `skillopt/config.py` |
| Training checkpoints and summaries | JSON and Markdown below `out_root`, owned by the trainer |
| Rollout/evaluation records | JSON or JSONL below the job/output directory |
| Studio jobs | One job directory with `job.json`, `log.txt`, and `out/`, owned by `JobManager` |
| Studio task sets and uploads | Files below `StudioConfig.studio_root` |
| SkillOpt-Sleep state | `state.json` plus staged proposal directories |

Do not add a database dependency for data already scoped to one local run or
Studio installation. If concurrent multi-host service operation becomes a
requirement, design that as a separate storage change rather than pretending
the current files provide distributed transactions.

## Serialization Contracts

- Use JSON/YAML parsers rather than string manipulation.
- Write text as UTF-8. JSON intended for people uses `ensure_ascii=False` and
  indentation.
- Keep persisted payloads as plain dictionaries/lists. Dataclasses and
  Pydantic models define in-process contracts and convert at the boundary.
- Preserve unknown environment-specific rollout fields. `RolloutResult` stores
  them in `extras` and merges them back in `to_dict()`.
- New persisted fields should normally be optional on read so old jobs and
  histories remain usable. `JobInfo.tokens` is an example of a derived API
  field that is not written back into `job.json`.

References:

- `skillopt/types.py::RolloutResult`
- `skillopt_studio/models.py::JobInfo`
- `skillopt_studio/artifacts.py`

## Atomic Writes

Use a temporary file plus replace for small mutable control records that may
be read while a process is running.

`skillopt_studio/jobs.py::JobManager._write_record()` follows this pattern:

```python
tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
tmp_path.replace(path)
```

`skillopt_sleep/state.py::SleepState.save()` similarly writes `.tmp` and calls
`os.replace()`.

Trainer outputs such as `history.json`, `runtime_state.json`, and versioned
Skill files are written by one training process at stage boundaries. Preserve
their existing schema and ordering when changing resume behavior. If a new
record is polled concurrently or is the sole resume pointer, prefer the atomic
pattern used by JobManager and SleepState.

Use JSONL for append-oriented event or trajectory streams where each row is
independently meaningful, such as Plugin attribution. Do not rewrite JSONL by
concatenating unescaped strings; serialize each dictionary with `json.dumps`.

## Configuration Persistence

`skillopt/config.py` owns YAML loading and recursive dictionary merge.
Configuration inheritance has these constraints:

- `_base_` is one path relative to the child YAML file.
- Circular inheritance raises `ValueError`.
- Structured config is flattened only at the trainer/adapter compatibility
  boundary.
- CLI overrides are parsed as typed values and then applied to the structured
  config.
- Persisted runtime config must redact secret-bearing fields; see
  `skillopt/engine/trainer.py::_redact_cfg`.

Do not implement a second YAML inheritance parser in a new CLI or Studio
feature.

## Validation And Path Safety

Validate user-controlled identifiers before using them as path components.
SkillEval task IDs name work directories, so
`skillopt/envs/skilleval/dataloader.py` rejects `/`, `\`, and `..`. Studio job
IDs receive the same checks in `JobManager._job_dir()`.

For uploaded or artifact paths:

1. Reject absolute paths and traversal components.
2. Resolve the candidate path.
3. Confirm it remains below the intended root.
4. Reject names reserved by the runtime, such as `.agents` and `task.md` in
   SkillEval seeded files.

Do not rely only on a filename extension or prefix check for containment.

## Read Behavior

Choose strictness based on the boundary:

- User task/config input is strict and fails with an item/field-specific
  message before model calls.
- Required checkpoint state is strict when continuing would produce an
  incorrect run.
- Optional discovery and backward-compatible resume probes may return a
  sentinel such as `None` or `[]`, but the caller must explicitly handle it.
- Corrupt Studio job records are skipped by best-effort listing; job execution
  failures are instead persisted as `status="failed"` with an `error`.

Avoid broad `except Exception: return {}` in required execution paths. It
turns corrupt state into plausible empty state and makes failures hard to
diagnose.

## Schema Changes

There is no migration command. Evolve file schemas by:

1. Adding optional fields with defaults where compatibility is possible.
2. Keeping readers tolerant of older records.
3. Updating writers, readers, API projections, and tests together.
4. Adding an explicit one-off conversion script only when compatibility cannot
   be handled safely in the reader.

Never silently reinterpret an existing field with a different type or meaning.
