# SkillEval Plugin Evaluation

## Scenario: Evaluate Multiple Skills From One Plugin

### 1. Scope / Trigger

Use this contract when SkillEval runs multiple Skills in one isolated agent
workspace or Studio creates a Plugin evaluation job. The target agent receives
all selected Skills, while task attribution metadata remains hidden from its
prompt.

### 2. Signatures

CLI:

```bash
python3 scripts/evaluate_skill.py \
  --skill <plugin/skills/alpha> \
  --skill <plugin/skills/beta> \
  --tasks <tasks.json> \
  --out_root <output>
```

Runtime:

```python
prepare_workspace(
    *,
    work_dir: str,
    skill_md: str,
    installed_skills: list[tuple[str, str]] | None = None,
    copy_files: list[tuple[str, str]] | None = None,
    ...
) -> tuple[str, str]
```

Studio Plugin job params:

```json
{
  "target_mode": "plugin",
  "plugin": "cc-knowledge",
  "skill_ids": ["<studio-skill-id>", "<studio-skill-id>"],
  "taskset_id": "<taskset-id>"
}
```

### 3. Contracts

- Repeated `--skill` arguments are ordered and installed together. One
  `--skill` retains the legacy `.agents/skills/skillopt-target/` layout.
- A Plugin runtime Skill has `name`, `content`, and `files`. Its workspace is
  `.agents/skills/<name>/SKILL.md` plus support files below the same directory.
- Runtime names are filesystem-safe and unique. Frontmatter `name` wins;
  directories fall back to their basename and standalone Markdown files to
  their filename stem.
- `target_skills` is absent or a non-empty string array. Values are stripped,
  deduplicated in order, and must name installed runtime Skills.
- `task_type` is a string, stripped at the Plugin boundary, and defaults to
  `default`.
- The target prompt contains only the task question and seeded files. It never
  contains `target_skills` or the expected route.
- `summary.json` contains `overall`, `by_skill`, `by_task_type`, `routing`,
  `integration`, and `weakest_skill`, each using `{count, hard, soft}` metrics.
- Studio resolves `skill_ids`, preserves their order after deduplication, and
  requires one non-null `(source, plugin)` identity. Legacy `skill_id` payloads
  remain valid.

### 4. Validation & Error Matrix

| Condition | Required result |
|---|---|
| Skill path or `SKILL.md` missing | Exit before backend configuration |
| Runtime name unsafe or duplicated | Exit before task rollout |
| `target_skills` malformed or unknown | Exit before task rollout |
| `task_type` is not a string | Fail during task loading |
| Task `files` escapes the workspace | Fail during task loading |
| Task `files` starts with `.agents` or `task.md` | Fail during task loading |
| `skill_ids` is not a list or has fewer than two distinct IDs | Reject command construction |
| Selected Skills span Plugin identities | Reject command construction |
| `target_mode` disagrees with `skill_id` / `skill_ids` | Reject command construction |
| Per-task target or judge execution fails | Persist `error` / `judge_error`; do not hide it |

### 5. Good / Base / Bad Cases

- Good: six `cc-knowledge` IDs with `target_mode=plugin` produce six repeated
  `--skill` flags and one shared runtime.
- Base: one `--skill` behaves as legacy single-Skill evaluation.
- Good: a task without `target_skills` contributes to overall and task-type
  metrics but not per-Skill metrics.
- Bad: two Skill directories with the same frontmatter name are rejected.
- Bad: `files={".agents": "..."}` or an unknown target Skill is rejected
  before any target or judge call.

### 6. Tests Required

- Unit: runtime naming, duplicate/unsafe names, workspace layout, support-file
  copying, reserved-path rejection, metadata normalization, and aggregation.
- CLI: one Skill keeps legacy layout; repeated Skills share one rollout and
  write Plugin metrics; validation happens before backend configuration.
- Studio: exact repeated argv, cross-Plugin and mode mismatch rejection, and
  API artifact round-trip for Plugin aggregates.
- Frontend: build succeeds; desktop and 390px checks prove Plugin default-all
  selection, exact submitted IDs, no horizontal overflow, and explicit Plugin
  identity in job lists/details.

### 7. Wrong vs Correct

#### Wrong

```python
task_text = f"{item['question']}\nExpected skill: {item['target_skills'][0]}"
```

This leaks the expected route and no longer measures discovery.

#### Correct

```python
prepare_workspace(
    task_text=item["question"],
    installed_skills=[(skill["name"], skill["content"]) for skill in runtime_skills],
)
result["target_skills"] = list(item.get("target_skills") or [])
```

Install every selected Skill, keep attribution outside the target prompt, and
attach it only to scored results.
