# Logging And Observability

## Three Observability Channels

This repository does not use one structured logging framework everywhere.
Match the existing channel for the runtime:

1. Training and evaluation CLIs print human-readable progress to stdout or
   stderr. Studio captures subprocess stdout and stderr in `log.txt`.
2. Long-lived Python services use the standard `logging` module for server
   warnings and errors.
3. Machine-readable state belongs in JSON/JSONL artifacts, not only in prose
   logs.

A successful run must be diagnosable from both its progress log and artifacts.
An error reason that exists only in terminal output is insufficient when the
result/job schema has an `error` field.

## CLI Progress Output

Use concise stage prefixes already established by the scripts:

```text
[taskgen] attempt 1/2
[skilleval] rolling out 10 tasks (workers=4)
[config] train_size=30
[resume] from step 3
```

References:

- `scripts/generate_tasks.py`
- `scripts/evaluate_skill.py`
- `skillopt/engine/trainer.py`
- `skillopt/envs/skilleval/rollout.py`

Use `flush=True` for progress that must be visible while a long model call,
subprocess, or concurrent batch is running. Final summaries may use ordinary
`print`.

CLI output should report:

- Resolved backend/model roles and non-secret run settings.
- Dataset/task counts and deterministic split/seed information.
- Stage transitions, resume points, and skip/retry reasons.
- Final score, artifact path, and terminal failure summary.

Do not print full prompts, responses, environment dictionaries, or credentials
as routine progress.

## Standard Logging

Library/server modules that need logging use:

```python
import logging

logger = logging.getLogger(__name__)
```

Use lazy formatting arguments and `exc_info=True` when a traceback is useful:

```python
logger.warning("sample skill %r skipped: source %s missing", slug, source)
logger.warning("sample materialization failed", exc_info=True)
```

Current level conventions:

- `warning`: an optional or recoverable feature was skipped and the server can
  continue correctly, as in `skillopt_studio/samples.py`.
- `error`: a backend operation failed and cannot produce its requested result.
- `info`/`debug`: not broadly configured in this codebase; do not add verbose
  library logging without a concrete consumer.

Do not call `logging.basicConfig()` from reusable package modules. Entrypoints
own logging configuration.

## Structured Artifacts

Use artifacts for fields that code or the Studio UI consumes:

- `job.json`: job status, timestamps, exit code, and concise error.
- `results.json`/JSONL: per-task scores, reasons, `error`, and `judge_error`.
- `summary.json`: aggregate scores and token counts.
- `history.json` and `runtime_state.json`: training decisions and resume state.
- Plugin attribution/step records: excluded failure IDs and reasons.

When adding an observable state transition, update the artifact schema and its
reader/tests together. Do not parse human-readable log lines to reconstruct
state that can be persisted directly.

## Secrets And Sensitive Content

Never log or persist raw API keys, passwords, bearer tokens, private keys, or a
complete process environment.

- Training config persistence uses
  `skillopt/engine/trainer.py::_redact_cfg()`.
- SkillOpt-Sleep uses `skillopt_sleep/staging.py::redact_secrets()` before
  persisting backend stderr, optimizer replies, and diagnostics.
- Bug reports and examples must remove API keys.

Backend stderr can contain credentials, especially authentication failures.
Run secret-bearing free text through the package's redaction helper before
adding it to a durable diagnostic artifact.

Task prompts and model responses may contain user data. Persist them only when
they are part of the established rollout/debug artifact contract, and do not
duplicate them into server logs.

## Subprocess Logs

Studio starts each job in a new process session and redirects stderr to stdout
in the job's `log.txt`. Preserve this single ordered stream so incremental
offset reads remain valid.

The process log and job error have different purposes:

- `log.txt` contains detailed command progress and traceback/output.
- `job.json.error` contains a short reason suitable for lists and dashboards.

Cancellation, spawn errors, and non-zero exits must still update the job
record even if no log file was produced.

## Anti-Patterns

Avoid:

- Swallowing an exception without a result field, status update, or warning.
- Logging success before the corresponding artifact write completes.
- Emitting the same high-volume message from every worker without task IDs.
- Using root `logging` calls in new reusable modules.
- Treating `print()` output as an API contract.
- Persisting raw config dictionaries or backend error payloads without
  redaction.
