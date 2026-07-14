# Design: Plugin Gated Directed Training

## Boundaries

Plugin training is an additive path:

```text
Studio Train
  -> Plugin job params
  -> scripts/train_plugin.py
  -> PluginTrainer
  -> SkillEval complete-Plugin rollout
  -> failure attribution
  -> existing reflect / aggregate / rank / apply primitives
  -> complete-Plugin validation
  -> Plugin gate
  -> snapshots + best_plugin/
```

`scripts/train.py` and `ReflACTTrainer` remain the single-Skill path. The new
trainer deliberately does not generalize the existing 1,600-line string-state
loop because its slow update, meta-Skill, appendix, resume, and artifact logic
all assume one document.

## State Contracts

Introduce small dataclasses in a Plugin-focused engine module:

```python
@dataclass(frozen=True)
class PluginSkill:
    name: str
    source_dir: str
    content: str
    files: tuple[tuple[str, str], ...]
    trainable: bool

@dataclass(frozen=True)
class PluginState:
    skills: tuple[PluginSkill, ...]

@dataclass(frozen=True)
class PluginGateResult:
    action: Literal["accept_new_best", "reject"]
    reasons: tuple[str, ...]
    overall_score: float
    regressions: dict[str, float]
```

Order is preserved from repeated CLI `--skill` arguments. Runtime names use the
same frontmatter-or-path resolution and filesystem validation as Plugin
evaluation. Candidate creation replaces content only for explicitly selected
trainable names.

Support files are frozen during optimization but copied into every rollout and
exported snapshot. Plugin support-file training can be added later without
changing the named state boundary.

## CLI and Configuration

Add `scripts/train_plugin.py` with:

```text
--config <structured skilleval config>
--skill <skill-dir>              repeated, at least two
--train-skill <runtime-name>     repeated, defaults to all
--out_root <dir>
```

Structured config adds:

```yaml
optimizer:
  max_skills_per_candidate: 2
evaluation:
  max_skill_regression: 0.0
```

The CLI resolves and validates Skills/tasks before configuring model backends
or creating rollout artifacts. The normal SkillEval split modes and model
settings are reused.

Studio uses the existing Plugin selection payload:

```json
{
  "target_mode": "plugin",
  "plugin": "cc-knowledge",
  "skill_ids": ["..."],
  "trainable_skill_ids": ["..."],
  "taskset_id": "...",
  "max_skills_per_candidate": 2,
  "max_skill_regression": 0.0
}
```

Single-Skill payloads remain unchanged. The runner resolves all IDs, verifies
one Plugin identity, maps trainable IDs to selected runtime names, writes the
normal config, and invokes `train_plugin.py`.

## Dataset and Runtime

Reuse `SkillEvalDataLoader` for train/validation/test planning. Normalize every
split with the shared Plugin task metadata contract before any model call.
Validation must have at least one task targeting each installed Skill.

Extend the SkillEval adapter with a complete-Plugin rollout method or a narrow
Plugin subclass. It converts `PluginState` to existing `RuntimeSkill` entries,
calls `run_batch`, judges results, attaches `target_skills` / `task_type`, and
persists the same reflection trajectories as single-Skill training.

No attribution metadata is added to the target prompt.

## Failure Attribution

A pure attribution function handles each failed result:

| Condition | Category | Gradient |
|---|---|---|
| `error` present | `task_failure` | none |
| `judge_error` present | `judge_failure` | none |
| `task_type == routing` | `routing` | named targets |
| `task_type == shared_dependency` | `shared_dependency` | named targets |
| `task_type == integration` or multiple targets | `handoff` | named targets |
| otherwise | `execution` | named targets |

Unknown or non-trainable targets remain in diagnostics but are not candidates.
Responsible Skills are ranked by descending attributed failure count, then
runtime order, and clipped to `max_skills_per_candidate`.

For each selected Skill, reflection receives only results attributed to that
name and the current content of that Skill. Existing trajectory files are
shared read-only; patches, merged edits, rankings, and apply reports are written
below `steps/step_NNNN/skills/<name>/`.

## Candidate and Gate

The trainer applies each selected Skill's ranked patch independently, then
evaluates the entire candidate Plugin on validation tasks. The pure gate:

1. Projects overall and per-Skill `{hard, soft}` through the existing
   `hard|soft|mixed` metric.
2. Requires `candidate.overall > current.overall`.
3. For every Skill with validation coverage, computes
   `current_skill_score - candidate_skill_score`.
4. Rejects if any regression exceeds `max_skill_regression`.

The default threshold `0.0` means no covered Skill may regress. The threshold
must be in `[0, 1]`. Since accepted overall scores are strictly monotonic, every
accepted candidate is also the new best.

Task/judge failures score zero in aggregates as they do today, but they never
create patches. A candidate may still be rejected because those failures lower
its complete-Plugin validation score.

## Training Loop

1. Load config, Skills, task splits, and runtime state.
2. Validate unique names, trainable subset, Plugin task metadata, and complete
   validation attribution coverage.
3. Configure models and evaluate the baseline complete Plugin.
4. For each deterministic training step:
   - Roll out the current complete Plugin on one training batch.
   - Attribute failures and choose at most the configured Skill count.
   - Reflect, aggregate, rank, and apply per selected Skill.
   - Skip if no eligible attribution or no applied edit.
   - Evaluate the complete candidate Plugin on validation.
   - Gate, persist history, and atomically update current/best pointers.
5. Optionally evaluate `best_plugin` on test.
6. Write summary and deployable `best_plugin/`.

The MVP supports patch update mode and the configured fixed learning rate.
Unsupported slow/meta/rewrite modes fail fast instead of silently diverging
from single-Skill semantics.

## Artifacts and Resume

```text
out/
  config.json
  history.json
  runtime_state.json
  attribution.jsonl
  plugin_versions/
    plugin_v0000/<skill-name>/...
    plugin_v0001/<skill-name>/...
  steps/step_0001/
    rollout/
    attribution.json
    skills/<skill-name>/{patches,merged_patch.json,ranked_edits.json,...}
    selection_eval/
    step_record.json
  best_plugin/
    manifest.json
    <skill-name>/SKILL.md
    <skill-name>/<support files>
  summary.json
```

Snapshots are written to a temporary sibling and renamed only after all files
and the manifest are complete. `runtime_state.json` points only to completed
snapshots and stores current/best aggregate metrics and the last completed
step. Resume validates the manifest's ordered Skill names against current CLI
inputs before continuing.

## Studio Presentation

Train reuses the evaluation page's mode and Plugin grouping controls. Plugin
mode selects the whole Plugin runtime and separately marks trainable Skills.
Numeric settings use bounded inputs for max Skills per candidate and allowed
regression.

Training results add Plugin fields without removing existing single-Skill
fields:

- baseline/current/best overall metrics
- per-Skill baseline/current/best metrics
- selected Skills and attribution category counts per step
- rejected regression reasons
- `best_plugin/` artifact link

## Compatibility and Rollback

- No existing CLI flag or artifact changes in the single-Skill path.
- Existing `skill_diff` remains for single-Skill jobs; Plugin jobs expose
  per-Skill diffs as an additive map.
- A Plugin job can be rolled back by deploying the initial
  `plugin_v0000` snapshot.
- Removing the new Plugin CLI/runner branch leaves all existing workflows
  intact.
