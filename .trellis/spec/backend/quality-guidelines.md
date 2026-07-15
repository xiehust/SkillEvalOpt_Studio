# Backend Quality Guidelines

## Baseline

Follow the existing Python 3.10+ style:

- Put `from __future__ import annotations` at the top of new Python modules.
- Add type hints to function signatures and shared data structures.
- Prefer dataclasses for internal value/state objects and Pydantic models for
  Studio HTTP contracts.
- Keep docstrings concise and focused on contracts or non-obvious behavior.
- Keep comments for invariants, compatibility, or subtle control flow; do not
  narrate obvious assignments.
- Use structured parsers for JSON/YAML and `pathlib.Path` or established local
  path helpers for filesystem work.

Ruff is configured for `E`, `F`, `I`, and `W`, Python 3.10, with a 120-character
line length. `E501` is ignored, but new code should still remain readable.

References:

- `pyproject.toml`
- `skillopt/types.py`
- `skillopt_studio/models.py`
- `skillopt_studio/config.py`

## Required Architecture Invariants

- `skillopt_sleep/` must remain independent of `skillopt/`.
- Model calls go through `skillopt.model` routing; do not call vendor SDKs from
  trainers, environments, or Studio routes.
- Validation that can prevent model calls runs before backend configuration or
  rollout.
- Rollout rows retain `id`, `hard`, and `soft`; failures remain visible in
  `error`/`judge_error`.
- Pure gate decisions stay free of persistence and printing; trainers own side
  effects.
- Studio routes remain thin and subprocess construction remains in
  `skillopt_studio/runners.py`.
- Shared constants and payload contracts have one backend owner. Frontend code
  consumes exported values rather than redefining them.
- Filesystem identifiers are validated before directory creation or recursive
  cleanup.

Read the Plugin-specific specs before changing multi-Skill evaluation or
training:

- `skilleval-plugin-evaluation.md`
- `skilleval-plugin-training.md`

## Testing Style

The primary suite is flat pytest under `tests/`:

- Name files `test_<area>.py` and test methods `test_<behavior>`.
- Group related cases with `class Test...`.
- Use `tmp_path` for filesystem state and `monkeypatch` for environment,
  dependency, and backend replacement.
- Keep tests deterministic with fixed seeds and explicit canned responses.
- Stub model calls and subprocess CLIs; unit tests must not spend model tokens
  or depend on installed agent binaries.
- Assert artifacts and observable failure reasons, not only return values.
- Add a regression test for every bug fix that can be reproduced locally.

Some older `skillopt_sleep` and plugin tests use `unittest.TestCase`. Maintain
their local style when editing them; new main-framework and Studio coverage
normally follows the pytest style shown in `tests/test_skilleval.py` and
`tests/test_studio_core.py`.

For boundary changes, test both sides:

- Config field: parser/default plus consumer.
- Persisted field: writer plus reader/projection.
- Studio API field: Pydantic model/router plus frontend type/usage.
- New environment: dataloader/adapter plus both CLI registries.
- Failure path: no backend call plus persisted/returned reason.

Avoid tautological tests. A test should fail if the production behavior under
test is removed; duplicating the same expression in the test is not useful.

## Verification Commands

Use `python3`; `python` is not on PATH on the standard development box.

```bash
python3 -m pytest tests/ -q
```

Run a focused file or test while iterating:

```bash
python3 -m pytest tests/test_skilleval.py -q
python3 -m pytest tests/test_scoring.py::TestComputeScore::test_single_result -q
```

Ruff is configured but may not be installed. If unavailable, use syntax
compilation for changed Python files:

```bash
python3 -m py_compile <changed-python-files>
```

For Studio frontend or API contract changes:

```bash
cd skillopt_studio/frontend && npm run build
```

Before completion:

```bash
git diff --check
```

## Review Checklist

1. Does the change respect package ownership and avoid a new cross-import?
2. Is input validated before destructive filesystem work or model calls?
3. Are errors surfaced in result/job artifacts rather than only printed?
4. Are persisted schemas backward-compatible or explicitly migrated?
5. Are secrets excluded or redacted from config, logs, and diagnostics?
6. Are shared constants/types reused instead of copied?
7. Do tests cover success, invalid input, failure visibility, and resume or
   compatibility behavior where relevant?
8. Were focused checks and the full suite run?

## Forbidden Patterns

- Silent broad exception catches in required execution paths.
- Direct vendor model calls outside `skillopt/model/`.
- Cross-imports from `skillopt_sleep` to `skillopt`.
- Hand-built JSON/YAML through string concatenation.
- Unsafe task/job IDs used as filesystem paths.
- Deleting or recreating a work directory before validating its identifier.
- Real network/model calls in unit tests.
- Unrelated refactors mixed into a behavioral fix.
- Generic helpers that duplicate an existing package-local abstraction.
