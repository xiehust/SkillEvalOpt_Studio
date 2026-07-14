"""Tests for the optional SkillEval judge contract."""
from __future__ import annotations

import json
import math

import pytest

from skillopt.envs.skilleval.contracts import JUDGE_MODES, normalize_judge_contract
from skillopt.envs.skilleval.dataloader import load_tasks


_CHECK_SPECS = {
    "exists": {},
    "opens": {},
    "contains_text": {"text": "Quarterly total"},
    "xlsx_cell": {"sheet": "Summary", "cell": "B12", "value": 42},
    "xlsx_formula": {"sheet": "Summary", "cell": "B12", "formula": "=SUM(B2:B11)"},
    "page_count": {"value": 3},
    "slide_count": {"value": 5},
    "image_dimensions": {"width": 1200, "height": 800},
    "visual": {"rubric": "Labels are readable and do not overlap."},
}
_REQUIRED_SPEC_FIELDS = {
    "contains_text": ("text",),
    "xlsx_cell": ("sheet", "cell", "value"),
    "xlsx_formula": ("sheet", "cell", "formula"),
    "page_count": ("value",),
    "slide_count": ("value",),
    "image_dimensions": ("width", "height"),
    "visual": ("rubric",),
}


def _item(**overrides) -> dict:
    item = {"id": "task_1"}
    item.update(overrides)
    return item


def _check(**overrides) -> dict:
    check = {
        "id": "artifact",
        "path": "outputs/report.xlsx",
        "type": "opens",
        "spec": {},
    }
    check.update(overrides)
    return check


def _task(**overrides) -> dict:
    task = {
        "id": "task_1",
        "question": "Create report.xlsx",
        "rubric": "The workbook is accurate and readable.",
    }
    task.update(overrides)
    return task


def _write_tasks(tmp_path, task: dict) -> str:
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps([task]), encoding="utf-8")
    return str(path)


class TestNormalizeJudgeMode:
    def test_registered_modes_are_exact(self) -> None:
        assert JUDGE_MODES == {"auto", "agentic", "chat"}

    def test_defaults_to_auto_and_marks_mode_implicit(self) -> None:
        mode, checks, explicit = normalize_judge_contract(0, _item())
        assert mode == "auto"
        assert checks == []
        assert explicit is False

    @pytest.mark.parametrize("mode", ["auto", "agentic", "chat"])
    def test_preserves_allowed_explicit_mode(self, mode) -> None:
        normalized_mode, _, explicit = normalize_judge_contract(
            0,
            _item(judge_mode=mode),
        )
        assert normalized_mode == mode
        assert explicit is True

    @pytest.mark.parametrize("mode", ["", "automatic", " auto ", 1, None])
    def test_rejects_invalid_explicit_mode(self, mode) -> None:
        with pytest.raises(ValueError, match="judge_mode"):
            normalize_judge_contract(2, _item(judge_mode=mode))


class TestNormalizeArtifactChecks:
    def test_normalizes_structured_check(self) -> None:
        raw = _check(
            id=" formula ",
            type="xlsx_formula",
            spec={"sheet": "Summary", "cell": "B12", "formula": "=SUM(B2:B11)"},
            ignored="drop me",
        )
        _, checks, _ = normalize_judge_contract(0, _item(artifact_checks=[raw]))
        assert checks == [
            {
                "id": "formula",
                "path": "outputs/report.xlsx",
                "type": "xlsx_formula",
                "required": True,
                "weight": 1.0,
                "spec": {
                    "sheet": "Summary",
                    "cell": "B12",
                    "formula": "=SUM(B2:B11)",
                },
            }
        ]
        assert set(checks[0]) == {"id", "path", "type", "required", "weight", "spec"}

    @pytest.mark.parametrize(("check_type", "spec"), _CHECK_SPECS.items())
    def test_accepts_every_registered_check_type(self, check_type, spec) -> None:
        _, checks, _ = normalize_judge_contract(
            0,
            _item(artifact_checks=[_check(type=check_type, spec=spec)]),
        )
        assert checks[0]["type"] == check_type
        assert checks[0]["spec"] == spec

    def test_normalizes_explicit_required_and_weight(self) -> None:
        _, checks, _ = normalize_judge_contract(
            0,
            _item(artifact_checks=[_check(required=False, weight=2)]),
        )
        assert checks[0]["required"] is False
        assert checks[0]["weight"] == 2.0
        assert isinstance(checks[0]["weight"], float)

    @pytest.mark.parametrize("checks", [None, {}, "opens", ()])
    def test_rejects_non_list_artifact_checks(self, checks) -> None:
        with pytest.raises(ValueError, match="artifact_checks"):
            normalize_judge_contract(0, _item(artifact_checks=checks))

    @pytest.mark.parametrize("check", [None, "opens", 1, []])
    def test_rejects_non_object_check(self, check) -> None:
        with pytest.raises(ValueError, match=r"artifact_checks\[0\].*object"):
            normalize_judge_contract(0, _item(artifact_checks=[check]))

    @pytest.mark.parametrize("check_id", ["", "   ", 1, None])
    def test_rejects_invalid_check_id(self, check_id) -> None:
        with pytest.raises(ValueError, match=r"artifact_checks\[0\].*id"):
            normalize_judge_contract(
                0,
                _item(artifact_checks=[_check(id=check_id)]),
            )

    def test_rejects_duplicate_trimmed_check_ids(self) -> None:
        checks = [_check(id="same"), _check(id=" same ", path="other.xlsx")]
        with pytest.raises(ValueError, match="duplicate.*same"):
            normalize_judge_contract(0, _item(artifact_checks=checks))

    @pytest.mark.parametrize("check_type", ["unknown", "", None, 1, []])
    def test_rejects_unknown_check_type(self, check_type) -> None:
        with pytest.raises(ValueError, match="artifact.*type"):
            normalize_judge_contract(
                0,
                _item(artifact_checks=[_check(type=check_type)]),
            )

    @pytest.mark.parametrize("spec", [None, [], "value", 1])
    def test_rejects_non_object_spec(self, spec) -> None:
        with pytest.raises(ValueError, match="artifact.*spec.*object"):
            normalize_judge_contract(
                0,
                _item(artifact_checks=[_check(spec=spec)]),
            )

    @pytest.mark.parametrize(
        ("check_type", "missing_field"),
        [
            (check_type, field)
            for check_type, fields in _REQUIRED_SPEC_FIELDS.items()
            for field in fields
        ],
    )
    def test_rejects_each_missing_required_spec_field(
        self,
        check_type,
        missing_field,
    ) -> None:
        spec = dict(_CHECK_SPECS[check_type])
        del spec[missing_field]
        with pytest.raises(ValueError, match=rf"artifact.*missing.*{missing_field}"):
            normalize_judge_contract(
                0,
                _item(artifact_checks=[_check(type=check_type, spec=spec)]),
            )

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "   ",
            "/tmp/report.xlsx",
            "~/report.xlsx",
            "~user/report.xlsx",
            r"outputs\report.xlsx",
            "outputs/\x00report.xlsx",
            ".",
            "..",
            "./report.xlsx",
            "outputs/./report.xlsx",
            "outputs/../report.xlsx",
            "outputs//report.xlsx",
            "outputs/",
            "C:/report.xlsx",
            "C:report.xlsx",
            "Z:",
            r"\\server\share\report.xlsx",
            ".agents/report.xlsx",
            ".claude/report.xlsx",
            ".codex/report.xlsx",
            ".git/report.xlsx",
            "task.md",
            "task.md/notes.txt",
        ],
    )
    def test_rejects_unsafe_or_runtime_colliding_path(self, path) -> None:
        with pytest.raises(ValueError, match="artifact.*path"):
            normalize_judge_contract(
                0,
                _item(artifact_checks=[_check(path=path)]),
            )

    @pytest.mark.parametrize("required", [None, 0, 1, "true", []])
    def test_rejects_non_boolean_required(self, required) -> None:
        with pytest.raises(ValueError, match="artifact.*required.*boolean"):
            normalize_judge_contract(
                0,
                _item(artifact_checks=[_check(required=required)]),
            )

    @pytest.mark.parametrize(
        "weight",
        [0, 0.0, -1, -0.5, True, False, math.nan, math.inf, -math.inf, "1", None],
    )
    def test_rejects_invalid_weight(self, weight) -> None:
        with pytest.raises(ValueError, match="artifact.*weight"):
            normalize_judge_contract(
                0,
                _item(artifact_checks=[_check(weight=weight)]),
            )


class TestJudgeContractLoading:
    def test_load_tasks_normalizes_explicit_contract(self, tmp_path) -> None:
        task = load_tasks(
            _write_tasks(
                tmp_path,
                _task(
                    judge_mode="agentic",
                    artifact_checks=[
                        _check(
                            id=" image ",
                            path="images/chart.png",
                            type="image_dimensions",
                            spec={"width": 1200, "height": 800},
                        )
                    ],
                ),
            )
        )[0]
        assert task["judge_mode"] == "agentic"
        assert task["_judge_mode_explicit"] is True
        assert task["artifact_checks"][0] == {
            "id": "image",
            "path": "images/chart.png",
            "type": "image_dimensions",
            "required": True,
            "weight": 1.0,
            "spec": {"width": 1200, "height": 800},
        }
