# Running task execution on Bedrock AgentCore Runtime

SkillOpt can run its exec-backend task execution — skilleval rollouts, AI task
generation, and training rollouts — inside per-task AWS Bedrock AgentCore
microVMs instead of local `claude`/`codex` subprocesses. Orchestration
(trainer, `evaluate_skill.py`, Studio, the judge) stays local; only the
`run_claude_code_exec` / `run_codex_exec` calls move to the cloud.

Design spec: `docs/superpowers/specs/agentcore-exec-runner.md`.

## How it works

1. The local orchestrator seeds the work_dir as usual (`prepare_workspace`).
2. With `SKILLOPT_EXEC_RUNNER=agentcore`, the harness tars the work_dir to
   `s3://$SKILLOPT_AGENTCORE_S3_BUCKET/exec-jobs/<job>/in.tar.gz` and invokes
   the `skillopt_exec_worker` runtime with a fresh `runtimeSessionId`
   (1 task = 1 isolated microVM, so batch workers parallelize in the cloud).
3. The worker (same `skillopt` harness code, baked into the image with the
   claude/codex standalone binaries) extracts the work_dir under the managed
   session-storage mount `/mnt/workspace`, runs the CLI **Bedrock-direct**
   (SigV4 from the execution role — no API keys anywhere), and while the task
   runs syncs changed files to `.../progress/` every
   `SKILLOPT_AGENTCORE_SYNC_INTERVAL` seconds (default 30, 0 disables).
4. Invocation is **asynchronous**: the invoke returns `accepted` immediately
   and the job continues in a background thread inside the microVM, kept
   alive past the 15-minute idle timeout by the SDK's async-task tracking
   (`/ping` → HealthyBusy; ceiling is the runtime's 8h `maxLifetime`). The
   worker uploads `started.json` on entry, and on completion flushes
   `out.tar.gz` (the mutated work_dir) plus `result.json`
   (response/raw/error) — every failure path included. The local runner
   polls S3 for `result.json` (interval `SKILLOPT_AGENTCORE_POLL_INTERVAL`,
   default 10s) up to `timeout × (retries+1) + 600s`, downloads both,
   replaces the local work_dir in place, and persists `claude_raw.txt` /
   trace summaries exactly like a local run. Manifest diffing, judging, and
   `codex_last_message.txt` readback are unchanged.

Long tasks (well beyond 15 minutes) are therefore fine; the session is only
stopped by the runner after the result lands. Judge calls (`policy=...`)
never go remote.

## Setup

```bash
pip install boto3          # local orchestrator dependency
python3 scripts/agentcore/setup_infra.py       # bucket + ECR + role + image + runtime
# paste the printed exports into .env, then:
set -a; source .env; set +a
python3 scripts/agentcore/setup_infra.py --smoke   # ping the worker
```

The setup is idempotent: rerunning rebuilds/pushes the image and updates the
runtime in place (`update_agent_runtime` creates a new version). Runtime info
is written to `outputs/agentcore/runtime_info.json`. `--teardown` deletes the
runtime (bucket/ECR/role are kept; they have no idle cost).

There is no VPC, NAT, or S3-Files mount: the runtime uses PUBLIC network mode
and managed session storage, and hands results back through the S3 bucket.

## Usage

CLI — any exec-backend run picks it up from the environment:

```bash
export SKILLOPT_EXEC_RUNNER=agentcore   # plus ARN/bucket from setup output
python3 scripts/evaluate_skill.py --skill <SKILL.md> --tasks <tasks.json> \
    --out_root outputs/eval_agentcore --model us.anthropic.claude-sonnet-5
python3 scripts/generate_tasks.py --skill <skill> --backend claude_code_exec \
    --model us.anthropic.claude-sonnet-5 --count 3 --out_root outputs/gen_agentcore
python3 scripts/train.py --config configs/skilleval/default.yaml ...
```

Models must be Bedrock IDs (the worker calls Bedrock directly):
`us.anthropic.claude-sonnet-5`, `us.anthropic.claude-opus-5`, … for
claude_code_exec; `openai.gpt-5.6-sol`, `openai.gpt-5.5`, … for codex_exec.
The runtime env sets `ANTHROPIC_MODEL=us.anthropic.claude-sonnet-5` as the
claude default when no `--model` is passed.

Studio — the eval/train/taskgen wizards expose a "Run on AgentCore" toggle
(enabled when the studio process env carries the runner config); it submits
`exec_runner: "agentcore"` and the job subprocess inherits the switch.

## Limitations

- `images` / `data_dirs` / `link_dirs` (docvqa, officeqa style envs) reference
  host paths outside the work_dir and are rejected with a clear error — bake
  the data into the worker image or extend the payload protocol first.
- Model access is whatever `bedrock:InvokeModel` on the execution role allows,
  in the runtime's region (us-west-2).
- Managed session storage is a Preview feature; each job re-seeds its
  workspace, so its reset-on-version-update semantics don't matter here.

## Troubleshooting

- Worker logs: CloudWatch log group `/aws/bedrock-agentcore/runtimes/<id>`.
- Mid-task progress: `aws s3 ls s3://$SKILLOPT_AGENTCORE_S3_BUCKET/exec-jobs/<job>/progress/`.
- `AgentCoreRunnerError ... no result.json appeared`: the worker crashed
  before its final flush — check CloudWatch; the invoke reply (if any) carries
  a traceback in `error`.
- HTTP 424 on invoke: session storage mount issue (see AWS docs
  `runtime-filesystem-configurations`).
