# Evaluate a Custom Skill

Answer the question **"is this skill any good?"** for an arbitrary skill
document — e.g. one from `~/.claude/skills/` — without registering a
benchmark env. `scripts/evaluate_skill.py` installs your skill into a
Claude Code CLI workspace, runs it on your own task set, scores every
response with an LLM judge against each task's rubric, and writes a
report.

> **Design spec.** The full design (decisions, scope, follow-ups) lives in
> [`docs/superpowers/specs/2026-07-01-skilleval-design.md`](../superpowers/specs/2026-07-01-skilleval-design.md).

## Usage

```bash
# single-file skill
python3 scripts/evaluate_skill.py \
    --skill ~/.claude/skills/my-skill/SKILL.md \
    --tasks data/my_tasks.json \
    --out_root outputs/skilleval_myskill \
    [--workers 4] [--timeout 600] [--limit 5] [--model <model>]

# multi-file skill (scripts/, references/, ...): pass the directory instead
python3 scripts/evaluate_skill.py \
    --skill ~/.claude/skills/my-skill \
    --tasks data/my_tasks.json \
    --out_root outputs/skilleval_myskill
```

Passing a directory requires a `SKILL.md` inside it; every other regular file
is copied into each task workspace under `.agents/skills/skillopt-target/`,
so relative references like `scripts/run.py` keep working. Hidden files,
`.git`/`__pycache__`/`node_modules`, and symlinks are skipped (a workspace
must never reach back into your source skill). Note that a script's runtime
dependencies (pip packages, CLI tools) are part of the machine, not the
skill — install them first or the evaluation measures your environment, not
the skill.

Backend configuration follows the same environment conventions as
`train.py` / `eval_only.py`. The target backend defaults to
`claude_code_exec` (the `claude` CLI must be on PATH and authenticated);
the judge runs on the optimizer backend (`--optimizer_backend`,
default `openai_chat`).

While debugging a new task set, start with `--limit 2` — task validation
runs over the whole file either way, so format errors surface immediately
without spending model calls.

## Task file format

A JSON array or JSONL file. Each item:

```json
{
  "id": "task_001",
  "question": "Summarize data/report.csv into a monthly table",
  "rubric": "Output must contain 12 month rows; sums correct; output path given",
  "files": {"data/report.csv": "month,amount\n2026-01,10\n..."},
  "task_type": "data-processing"
}
```

| Field | Required | Description |
|---|---|---|
| `id` | ✅ | Unique, filesystem-safe (no `/`, `\`, `..`) — names the task work_dir |
| `question` | ✅ | Task text given to the agent (written to `task.md`) |
| `rubric` | ✅ | Natural-language acceptance criteria for the judge |
| `files` | — | `{relative path: text content}` seeded into the work_dir |
| `task_type` | — | Grouping key for the report, default `"default"` |

Validation is fail-fast: a missing field, duplicate id, or unsafe id
anywhere in the file aborts the run before any model call.

## What a run does

For each task (thread pool, `--workers`):

1. Creates `out_root/rollouts/<id>/` with your skill at
   `.agents/skills/skillopt-target/SKILL.md`, the question at `task.md`,
   and any `files` seeded alongside.
2. Drives Claude Code CLI in that directory (per-task `--timeout`).
3. The judge model reads the question, the rubric, the agent's response,
   and a listing of files the agent produced, then returns a JSON verdict
   `{"pass": bool, "score": 0-1, "reason": str}`.

A task that crashes or times out never aborts the batch — it is scored 0,
the judge is skipped, and the error appears in the report's Failures
section. A judge reply that fails JSON parsing is retried once; a second
failure scores 0 with a `judge_error` marker (never silently).

## Output artifacts

| Path | Contents |
|---|---|
| `out_root/report.md` | Summary (pass rate, soft mean), per-`task_type` breakdown, per-task table, cost, failures |
| `out_root/results.json` | Every result dict verbatim, for programmatic use |
| `out_root/rollouts/<id>/` | Per-task workspace incl. the Claude Code transcript artifacts |

Scoring follows the project-wide convention: `hard` (0/1, rubric fully
satisfied) and `soft` (0–1 partial credit) — the same signal the trainer
consumes, so a task set built for evaluation can later drive skill
optimization unchanged.

## Current limitations

The minimal version deliberately leaves out (see the design spec's
里程碑之后 section for the planned path):

- **No baseline comparison** — it does not run a no-skill control group.
- **No improvement suggestions** — failures are reported, not reflected on.
- **No train.py integration** — the env is not registered as an adapter
  yet; the task format is already compatible.
- **Token usage** is reported as `n/a` (durations are tracked).
