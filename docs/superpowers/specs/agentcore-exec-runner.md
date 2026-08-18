# AgentCore Exec Runner

Run SkillOpt's exec-backend task execution (rollouts, taskgen, training rollouts) on
Amazon Bedrock AgentCore Runtime instead of local subprocesses, with per-task microVM
isolation. Designed 2026-08-17 with River; supersedes the earlier BYO-S3-Files draft
(the user chose managed session storage + S3 sync to avoid VPC/NAT infrastructure).

## Architecture

```
local orchestrator (trainer / evaluate_skill / generate_tasks / studio / judge)
  │ prepare_workspace() → local work_dir            (unchanged)
  │ run_claude_code_exec / run_codex_exec           (hook at top, after judge-policy branch)
  │   └─ agentcore_runner.run_remote_exec()
  │        tar work_dir ──► s3://<bucket>/<prefix>/<job>/in.tar.gz
  │        InvokeAgentRuntime(payload)  [1 task = 1 runtimeSessionId = 1 microVM]
  │        ◄── out.tar.gz (work_dir after exec) + result.json {response, raw, error}
  │        replace local work_dir, return (response, raw)
  │   └─ _persist_*_artifacts() locally             (unchanged semantics)
  └ manifest diff / judge / readback of codex_last_message.txt etc.  (unchanged, local)

AgentCore Runtime (PUBLIC network, ARM64 container, managed session storage /mnt/workspace)
  worker = skillopt.model.agentcore_worker (BedrockAgentCoreApp, HTTP :8080)
    download in.tar.gz → /mnt/workspace/exec/<job>/<work_dir_name>
    background thread: every N s best-effort sync work_dir → s3 .../progress/  (observability)
    run the SAME run_*_exec harness (CLI mode) with Bedrock-direct model access
    final flush: upload out.tar.gz + result.json
```

Key decisions:
- **No VPC.** PUBLIC network mode has direct internet; model calls are Bedrock-direct
  SigV4 via the execution role (`CLAUDE_CODE_USE_BEDROCK=1` for claude; codex
  `model_provider = "amazon-bedrock"`). Zero secrets in the image or payload.
  All models available in us-west-2 (`us.anthropic.claude-*`, `openai.gpt-5.6-*`).
- **Judge never remote.** `policy is not None` (judge transport) bypasses the hook.
- **Raw passthrough.** The remote harness produces the same attempt-banner raw;
  the local hook re-runs `_persist_*_artifacts` so parent-dir artifacts
  (`claude_raw.txt`, trace summaries) and `extract_exec_usage` behave identically.
- **Error parity.** Harness-internal failures (claude timeout → empty response)
  travel through result.json like local. Worker-caught exceptions (codex nonzero
  exit raises locally) are re-raised locally as RuntimeError. Infra failures
  (missing bucket/ARN, mount, S3) fail fast with clear messages.
- **Session storage** (`/mnt/workspace`, Preview) is the work root; per-session
  isolation is fine because 1 task = 1 session. Falls back to a temp dir if absent.

## Config (env vars, read at call time)

| var | default | meaning |
|---|---|---|
| `SKILLOPT_EXEC_RUNNER` | `local` | `agentcore` enables remote exec |
| `SKILLOPT_AGENTCORE_RUNTIME_ARN` | — | required when enabled |
| `SKILLOPT_AGENTCORE_S3_BUCKET` | — | required when enabled |
| `SKILLOPT_AGENTCORE_S3_PREFIX` | `exec-jobs` | key prefix |
| `SKILLOPT_AGENTCORE_REGION` | parsed from ARN | boto3 region |
| `SKILLOPT_AGENTCORE_SYNC_INTERVAL` | `30` | worker progress-sync seconds, 0 = off |
| `SKILLOPT_AGENTCORE_QUALIFIER` | `DEFAULT` | runtime endpoint |

## Limitations (fail fast, documented)

- `images` / `data_dirs` / symlinked `link_dirs` reference host paths outside the
  work_dir and are not shipped: the runner raises `ValueError`. Covers docvqa /
  officeqa style envs — bake data into the image or extend the protocol later.
- Invocation is **asynchronous** (docs: `runtime-long-run`): the worker
  validates, spawns a daemon thread (tracked via `add_async_task` /
  `complete_async_task` so `/ping` reports HealthyBusy and the session
  survives the idle timeout, up to the 8h maxLifetime), and returns
  `accepted` immediately. The thread uploads `started.json` on entry and —
  no matter what fails — flushes `result.json` at the end; the runner polls
  S3 for it (`SKILLOPT_AGENTCORE_POLL_INTERVAL`, default 10s) until
  `timeout × (retries+1) + 600s`, then stops the session. If the invoke
  reply is lost, `started.json` within 120s decides between keep-waiting
  and fail-fast. Tasks may run well past 15 minutes.
- Session storage is a Preview feature; data resets on runtime version updates
  (irrelevant here — every job re-seeds).

## Infra (scripts/agentcore/setup_infra.py, idempotent)

S3 bucket + ECR repo + execution role (trust bedrock-agentcore; ECR pull, logs,
X-Ray, `bedrock:InvokeModel*`, S3 RW on the bucket) + `create_agent_runtime`
(container, PUBLIC, sessionStorage `/mnt/workspace`, env defaults). Image built
natively on this aarch64 box; claude/codex standalone binaries staged from the
local install (`deploy/agentcore/`). `--teardown` removes the runtime.

## Studio

Eval/taskgen/train job requests accept `exec_runner: "local" | "agentcore"`;
when `agentcore`, the job env gets `SKILLOPT_EXEC_RUNNER=agentcore` (ARN/bucket
inherited from the studio process env, i.e. `.env`), and the local-CLI presence
check in `_resolve_target_backend` is skipped.
