# Design: Plugin Unified Evaluation

## Boundaries

The feature extends the existing skilleval path without introducing a new
benchmark environment:

`Studio UI -> job params -> runner argv -> evaluate_skill.py -> skilleval rollout
-> workspace -> result aggregation -> Studio artifacts`

Single-Skill mode remains the one-element form of the same internal contract.

## Selection Contract

Studio jobs use:

```json
{
  "target_mode": "plugin",
  "plugin": "cc-knowledge",
  "skill_ids": ["claude-plugins:cc-knowledge:skill-a", "..."],
  "taskset_id": "..."
}
```

The runner resolves IDs, deduplicates while preserving order, and verifies one
`(source, plugin)` key. It invokes the CLI with repeated `--skill` arguments.
The CLI independently validates paths and unique runtime names.

## Runtime Snapshot

Introduce a small immutable Skill runtime descriptor containing a stable name,
source directory, `SKILL.md`, and support files. For Plugin mode, rollout
creates:

```text
<work_dir>/.agents/skills/<runtime-name>/SKILL.md
<work_dir>/.agents/skills/<runtime-name>/<support files>
```

The existing `skillopt-target` directory is retained for one-Skill calls to
avoid behavioral changes. Plugin runtime names come from directory names and
must be unique and filesystem-safe.

`prepare_workspace()` gains an optional collection of runtime Skills. The
workspace is still deleted once per task before any files are written.

## Task Metadata

The dataloader continues to own base task validation. Plugin evaluation adds a
normalization boundary:

- `target_skills`: absent or a non-empty list of unique strings.
- Every listed target must match one selected runtime Skill name.
- `task_type`: normalized non-empty string, defaulting to `default`.

Metadata is copied to the result after judging and never included in the target
agent prompt.

## Aggregation

A pure aggregation function consumes scored results and returns metric buckets:

- `overall`
- `by_skill`
- `by_task_type`
- `routing` for `task_type == "routing"`
- `integration` for tasks targeting multiple Skills or explicitly typed as
  integration
- `weakest_skill`

Each bucket reports `count`, mean `hard`, and mean `soft`. Multi-target tasks
contribute to every named Skill. Empty buckets are omitted or represented as
`null`, never divided by zero. Stable key ordering makes artifacts testable.

## Compatibility

- `--skill` changes from scalar to append/repeated but one use is unchanged.
- Existing summary fields remain; Plugin aggregates are additive.
- Existing Studio `skill_id` payload is still accepted.
- Non-exec chat backends retain their current prompt-based behavior; real
  multi-Skill discovery is guaranteed for exec backends used by Plugin mode.

## Failure and Rollback

Validation occurs before model configuration and rollout. Per-task execution
and judge failures remain visible through existing `error` and `judge_error`
fields. The change can be rolled back by selecting Single Skill mode; no stored
task set or source Skill migration is required.
