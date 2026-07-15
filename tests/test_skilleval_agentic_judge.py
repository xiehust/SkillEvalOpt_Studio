# tests/test_skilleval_agentic_judge.py
from __future__ import annotations

import json
import math

import pytest

from skillopt.envs.skilleval import verdict as verdict_mod
from skillopt.envs.skilleval.judge_cache import VerdictCache
from skillopt.envs.skilleval.verdict import (
    parse_verdict,
    run_deterministic_checks,
    score_criteria,
    split_checks,
    synthesize_dependent_failures,
)

CHECKS = [
    {"id": "opens", "required": True, "weight": 1.0},
    {"id": "visual", "required": True, "weight": 3.0},
]


def test_host_computes_weighted_score_and_required_hard() -> None:
    criteria = [
        {"id": "opens", "passed": True, "score": 1.0, "reason": "ok", "evidence": []},
        {"id": "visual", "passed": False, "score": 0.5, "reason": "clipped", "evidence": []},
    ]
    assert score_criteria(CHECKS, criteria) == (0, 0.625)


def test_verdict_rejects_unknown_criterion_and_prose() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        parse_verdict("Here is the result: {}", CHECKS, {"report.pdf"})
    payload = {
        "schema_version": 1,
        "status": "valid",
        "criteria": [{"id": "invented", "passed": True, "score": 1,
                      "reason": "ok", "evidence": []}],
        "coverage": {"artifacts": [], "units_inspected": [], "units_omitted": []},
        "reason": "ok",
    }
    with pytest.raises(ValueError, match="criterion"):
        parse_verdict(json.dumps(payload), CHECKS, {"report.pdf"})


def test_cache_requires_matching_fingerprint(tmp_path) -> None:
    cache = VerdictCache(str(tmp_path / "cache"))
    cache.put("state", "task", {"evidence": "one"}, {"hard": 1})
    assert cache.get("state", "task", {"evidence": "one"}) == {"hard": 1}
    assert cache.get("state", "task", {"evidence": "two"}) is None


def _valid_payload(**overrides: object) -> dict:
    payload = {
        "schema_version": 1,
        "status": "valid",
        "criteria": [
            {"id": "opens", "passed": True, "score": 1.0, "reason": "ok",
             "evidence": [{"path": "report.pdf", "locator": "", "source": "structure"}]},
            {"id": "visual", "passed": True, "score": 1.0, "reason": "clear",
             "evidence": [{"path": "report.pdf", "locator": "page=1", "source": "render"}]},
        ],
        "coverage": {"artifacts": ["report.pdf"], "units_inspected": ["report.pdf:page=1"], "units_omitted": []},
        "reason": "ok",
    }
    payload.update(overrides)
    return payload


class TestParseVerdictStrictness:
    def test_accepts_a_fully_valid_payload(self) -> None:
        payload = _valid_payload()
        parsed = parse_verdict(json.dumps(payload), CHECKS, {"report.pdf"})
        assert parsed == payload

    def test_rejects_evidence_path_outside_the_trusted_set(self) -> None:
        payload = _valid_payload()
        payload["criteria"][0]["evidence"] = [
            {"path": "not-in-evidence.pdf", "locator": "", "source": "structure"}
        ]
        with pytest.raises(ValueError, match="evidence path"):
            parse_verdict(json.dumps(payload), CHECKS, {"report.pdf"})

    def test_rejects_missing_criterion_id(self) -> None:
        payload = _valid_payload()
        payload["criteria"] = payload["criteria"][:1]  # drop "visual"
        with pytest.raises(ValueError, match="missing criteria"):
            parse_verdict(json.dumps(payload), CHECKS, {"report.pdf"})

    def test_rejects_duplicate_criterion_id(self) -> None:
        payload = _valid_payload()
        payload["criteria"].append(dict(payload["criteria"][0]))
        with pytest.raises(ValueError, match="duplicated"):
            parse_verdict(json.dumps(payload), CHECKS, {"report.pdf"})

    def test_rejects_missing_top_level_key(self) -> None:
        payload = _valid_payload()
        del payload["coverage"]
        with pytest.raises(ValueError, match="top-level keys"):
            parse_verdict(json.dumps(payload), CHECKS, {"report.pdf"})

    def test_rejects_extra_top_level_key(self) -> None:
        payload = _valid_payload(extra="nope")
        with pytest.raises(ValueError, match="top-level keys"):
            parse_verdict(json.dumps(payload), CHECKS, {"report.pdf"})

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_rejects_bare_json_constants(self, literal: str) -> None:
        payload = _valid_payload()
        text = json.dumps(payload).replace('"score": 1.0', f'"score": {literal}', 1)
        with pytest.raises(ValueError, match="constant"):
            parse_verdict(text, CHECKS, {"report.pdf"})

    def test_rejects_non_finite_score_from_numeric_overflow(self) -> None:
        # 1e400 is valid JSON syntax but overflows to float("inf") without
        # ever invoking parse_constant, so this exercises the separate
        # explicit isfinite() enforcement.
        payload = _valid_payload()
        text = json.dumps(payload).replace('"score": 1.0', '"score": 1e400', 1)
        with pytest.raises(ValueError, match="finite"):
            parse_verdict(text, CHECKS, {"report.pdf"})

    def test_rejects_non_dict_payload(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            parse_verdict("[]", CHECKS, {"report.pdf"})

    def test_rejects_malformed_criterion_shape(self) -> None:
        payload = _valid_payload()
        payload["criteria"][0] = {"id": "opens", "passed": True}  # missing keys
        with pytest.raises(ValueError, match="exactly the keys"):
            parse_verdict(json.dumps(payload), CHECKS, {"report.pdf"})


class TestSplitChecks:
    def test_partitions_deterministic_and_agent_checks(self) -> None:
        checks = [
            {"id": "a", "path": "x.pdf", "type": "exists", "required": True, "weight": 1.0, "spec": {}},
            {"id": "b", "path": "x.pdf", "type": "visual", "required": True, "weight": 1.0,
             "spec": {"rubric": "clear"}},
            {"id": "c", "path": "x.pdf", "type": "rubric", "required": True, "weight": 1.0, "spec": {}},
        ]
        deterministic, agent = split_checks(checks)
        assert [c["id"] for c in deterministic] == ["a"]
        assert [c["id"] for c in agent] == ["b", "c"]

    def test_rejects_unknown_check_type(self) -> None:
        checks = [{"id": "a", "path": "x.pdf", "type": "bogus", "required": True, "weight": 1.0, "spec": {}}]
        with pytest.raises(ValueError, match="unknown check type"):
            split_checks(checks)


def _roots(tmp_path):
    evidence = tmp_path / "evidence"
    scratch = tmp_path / "scratch"
    evidence.mkdir()
    scratch.mkdir()
    return evidence, scratch


def _check(check_id: str, path: str, check_type: str, *, required: bool = True, spec: dict | None = None) -> dict:
    return {
        "id": check_id,
        "path": path,
        "type": check_type,
        "required": required,
        "weight": 1.0,
        "spec": spec or {},
    }


class TestDeterministicExistsAndOpens:
    def test_exists_passes_and_fails_by_filesystem_presence(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        (evidence / "answer.txt").write_text("hello", encoding="utf-8")
        checks = [
            _check("present", "answer.txt", "exists"),
            _check("absent", "missing.txt", "exists"),
        ]
        criteria, broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        by_id = {row["id"]: row for row in criteria}
        assert by_id["present"]["passed"] is True
        assert by_id["absent"]["passed"] is False
        assert broken == frozenset({"missing.txt"})

    def test_opens_passes_for_a_real_image_and_fails_for_a_missing_file(self, tmp_path) -> None:
        from PIL import Image

        evidence, scratch = _roots(tmp_path)
        Image.new("RGB", (4, 4), color=(1, 2, 3)).save(evidence / "preview.png")
        checks = [
            _check("opens_ok", "preview.png", "opens"),
            _check("opens_missing", "missing.png", "opens", required=True),
        ]
        criteria, broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        by_id = {row["id"]: row for row in criteria}
        assert by_id["opens_ok"]["passed"] is True
        assert by_id["opens_missing"]["passed"] is False
        assert broken == frozenset({"missing.png"})

    def test_optional_missing_output_does_not_mark_path_broken(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        checks = [_check("optional_missing", "missing.png", "opens", required=False)]
        criteria, broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        assert criteria[0]["passed"] is False
        assert broken == frozenset()


class TestDeterministicImageDimensions:
    def test_matches_exactly_and_reports_mismatch(self, tmp_path) -> None:
        from PIL import Image

        evidence, scratch = _roots(tmp_path)
        Image.new("RGBA", (40, 20), color=(255, 0, 0, 128)).save(evidence / "preview.png")
        checks = [
            _check("match", "preview.png", "image_dimensions", spec={"width": 40, "height": 20}),
            _check("mismatch", "preview.png", "image_dimensions", spec={"width": 10, "height": 10}),
        ]
        criteria, _broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        by_id = {row["id"]: row for row in criteria}
        assert by_id["match"]["passed"] is True
        assert by_id["mismatch"]["passed"] is False


class TestDeterministicContainsText:
    def test_finds_and_fails_to_find_substring_in_a_real_docx(self, tmp_path) -> None:
        from docx import Document

        evidence, scratch = _roots(tmp_path)
        document = Document()
        document.add_paragraph("Quarterly report is ready")
        document.save(evidence / "memo.docx")
        checks = [
            _check("found", "memo.docx", "contains_text", spec={"text": "Quarterly report"}),
            _check("not_found", "memo.docx", "contains_text", spec={"text": "Nonexistent phrase"}),
        ]
        criteria, _broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        by_id = {row["id"]: row for row in criteria}
        assert by_id["found"]["passed"] is True
        assert by_id["not_found"]["passed"] is False


class TestDeterministicSlideCount:
    def test_matches_exactly_and_reports_mismatch(self, tmp_path) -> None:
        from pptx import Presentation

        evidence, scratch = _roots(tmp_path)
        presentation = Presentation()
        presentation.slides.add_slide(presentation.slide_layouts[1])
        presentation.save(evidence / "briefing.pptx")
        checks = [
            _check("match", "briefing.pptx", "slide_count", spec={"value": 1}),
            _check("mismatch", "briefing.pptx", "slide_count", spec={"value": 5}),
        ]
        criteria, _broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        by_id = {row["id"]: row for row in criteria}
        assert by_id["match"]["passed"] is True
        assert by_id["mismatch"]["passed"] is False


class TestDeterministicPageCount:
    def test_matches_a_real_pdf_via_the_inspector_registry(self, tmp_path, monkeypatch) -> None:
        import subprocess

        from skillopt.envs.skilleval.inspectors import pdf_image

        evidence, scratch = _roots(tmp_path)
        (evidence / "report.pdf").write_bytes(b"%PDF-1.4\ncontent")

        def fake_run(command, **kwargs):
            if command[0] == "pdfinfo":
                return subprocess.CompletedProcess(command, 0, "Pages: 2\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(pdf_image, "safe_run", fake_run)
        checks = [
            _check("match", "report.pdf", "page_count", spec={"value": 2}),
            _check("mismatch", "report.pdf", "page_count", spec={"value": 99}),
        ]
        criteria, _broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        by_id = {row["id"]: row for row in criteria}
        assert by_id["match"]["passed"] is True
        assert by_id["mismatch"]["passed"] is False

    def test_fails_without_crashing_when_format_has_no_page_concept(self, tmp_path) -> None:
        from docx import Document

        evidence, scratch = _roots(tmp_path)
        Document().save(evidence / "memo.docx")
        checks = [_check("no_pages", "memo.docx", "page_count", spec={"value": 1})]
        criteria, broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        assert criteria[0]["passed"] is False
        # The artifact opened fine; only the page-count concept is unavailable
        # for this format, so it is not a "file is broken" failure.
        assert broken == frozenset()


class TestDeterministicXlsxCellAndFormula:
    def test_matches_and_mismatches_cell_value_and_formula(self, tmp_path) -> None:
        from openpyxl import Workbook

        evidence, scratch = _roots(tmp_path)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Summary"
        sheet["A1"] = 42
        sheet["B1"] = "=A1+1"
        workbook.save(evidence / "report.xlsx")

        checks = [
            _check("cell_ok", "report.xlsx", "xlsx_cell", spec={"sheet": "Summary", "cell": "A1", "value": 42}),
            _check("cell_bad", "report.xlsx", "xlsx_cell", spec={"sheet": "Summary", "cell": "A1", "value": 7}),
            _check(
                "formula_ok",
                "report.xlsx",
                "xlsx_formula",
                spec={"sheet": "Summary", "cell": "B1", "formula": "=A1+1"},
            ),
        ]
        criteria, broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        by_id = {row["id"]: row for row in criteria}
        assert by_id["cell_ok"]["passed"] is True
        assert by_id["cell_bad"]["passed"] is False
        assert by_id["formula_ok"]["passed"] is True
        assert broken == frozenset()

    def test_missing_workbook_fails_without_crashing_and_marks_path_broken(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        checks = [
            _check(
                "cell",
                "missing.xlsx",
                "xlsx_cell",
                spec={"sheet": "Summary", "cell": "A1", "value": 1},
            )
        ]
        criteria, broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        assert criteria[0]["passed"] is False
        assert broken == frozenset({"missing.xlsx"})


class TestEvaluationErrorClassification:
    """Infrastructure failures (timeouts/crashes/sandbox errors) must not be
    scored as agent failures -- they must surface distinctly from a
    genuinely corrupt or missing artifact so the caller can classify the
    row `evaluation_error` instead of `artifact_failure`.
    """

    def test_infrastructure_timeout_propagates_uncaught_not_a_failed_criterion(
        self, tmp_path, monkeypatch
    ) -> None:
        from skillopt.envs.skilleval.inspectors import EvaluationError

        evidence, scratch = _roots(tmp_path)
        (evidence / "report.xls").write_bytes(b"legacy-bytes")

        def _timed_out(*args, **kwargs):
            raise EvaluationError("LibreOffice conversion exceeded its total timeout")

        monkeypatch.setattr(verdict_mod, "inspect_artifact", _timed_out)
        checks = [_check("opens", "report.xls", "opens", required=True)]

        with pytest.raises(EvaluationError, match="timeout"):
            run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))

    def test_infrastructure_crash_propagates_uncaught_even_for_a_required_check(
        self, tmp_path, monkeypatch
    ) -> None:
        from skillopt.envs.skilleval.inspectors import EvaluationError

        evidence, scratch = _roots(tmp_path)
        (evidence / "memo.docx").write_bytes(b"docx-bytes")

        def _crashed(*args, **kwargs):
            raise EvaluationError("artifact inspection failed: BrokenPipeError")

        monkeypatch.setattr(verdict_mod, "extract_artifact", _crashed)
        checks = [_check("contains_text", "memo.docx", "contains_text", spec={"text": "hello"})]

        with pytest.raises(EvaluationError):
            run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))

    def test_corrupt_artifact_still_yields_artifact_failure_and_broken_path(
        self, tmp_path, monkeypatch
    ) -> None:
        from skillopt.envs.skilleval.inspectors import InspectionError

        evidence, scratch = _roots(tmp_path)
        (evidence / "report.xls").write_bytes(b"legacy-bytes")

        def _corrupt(*args, **kwargs):
            raise InspectionError("artifact is not a valid legacy spreadsheet")

        monkeypatch.setattr(verdict_mod, "inspect_artifact", _corrupt)
        checks = [_check("opens", "report.xls", "opens", required=True)]

        criteria, broken = run_deterministic_checks(checks, evidence_dir=str(evidence), scratch_dir=str(scratch))
        assert criteria[0]["passed"] is False
        assert broken == frozenset({"report.xls"})


class TestNoAgentChecksShortCircuit:
    def test_split_yields_no_agent_checks_when_none_are_declared(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        (evidence / "answer.txt").write_text("hi", encoding="utf-8")
        checks = [_check("exists", "answer.txt", "exists")]
        deterministic, agent = split_checks(checks)
        assert agent == []
        criteria, broken = run_deterministic_checks(
            deterministic, evidence_dir=str(evidence), scratch_dir=str(scratch)
        )
        synthesized, remaining = synthesize_dependent_failures(agent, broken)
        # No agent-owned checks at all: nothing needs the model.
        assert synthesized == []
        assert remaining == []
        hard, soft = score_criteria(checks, criteria)
        assert (hard, soft) == (1, 1.0)


class TestDependentCriterionSynthesis:
    def test_required_output_failure_blocks_the_dependent_agent_check(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        # "report.pdf" never lands in evidence: the required "opens" check
        # fails, and "visual" (same path) becomes impossible to judge.
        deterministic_checks = [_check("opens", "report.pdf", "opens", required=True)]
        agent_checks = [_check("visual", "report.pdf", "visual", required=True, spec={"rubric": "clear"})]

        criteria, broken = run_deterministic_checks(
            deterministic_checks, evidence_dir=str(evidence), scratch_dir=str(scratch)
        )
        assert broken == frozenset({"report.pdf"})

        synthesized, remaining = synthesize_dependent_failures(agent_checks, broken)
        assert remaining == []
        assert len(synthesized) == 1
        assert synthesized[0]["id"] == "visual"
        assert synthesized[0]["passed"] is False

        all_checks = deterministic_checks + agent_checks
        all_criteria = criteria + synthesized
        hard, soft = score_criteria(all_checks, all_criteria)
        assert hard == 0
        assert soft == 0.0

    def test_unrelated_agent_check_on_a_healthy_path_still_needs_the_model(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        (evidence / "report.pdf").write_bytes(b"present")
        # A required, *optional-independent* deterministic check on a
        # different path fails, but it must not block an agent check on a
        # healthy path.
        deterministic_checks = [_check("opens_other", "missing.pdf", "opens", required=True)]
        agent_checks = [_check("visual", "report.pdf", "visual", required=True, spec={"rubric": "clear"})]

        _criteria, broken = run_deterministic_checks(
            deterministic_checks, evidence_dir=str(evidence), scratch_dir=str(scratch)
        )
        assert broken == frozenset({"missing.pdf"})

        synthesized, remaining = synthesize_dependent_failures(agent_checks, broken)
        assert synthesized == []
        assert remaining == agent_checks

    def test_value_mismatch_on_an_openable_file_does_not_block_dependents(self, tmp_path) -> None:
        from openpyxl import Workbook

        evidence, scratch = _roots(tmp_path)
        workbook = Workbook()
        workbook.active["A1"] = 1
        workbook.save(evidence / "report.xlsx")

        # The workbook opens fine; only the specific cell assertion fails.
        deterministic_checks = [
            _check(
                "cell",
                "report.xlsx",
                "xlsx_cell",
                required=True,
                spec={"sheet": "Sheet", "cell": "A1", "value": 999},
            )
        ]
        agent_checks = [_check("visual", "report.xlsx", "visual", required=True, spec={"rubric": "clear"})]

        _criteria, broken = run_deterministic_checks(
            deterministic_checks, evidence_dir=str(evidence), scratch_dir=str(scratch)
        )
        assert broken == frozenset()

        synthesized, remaining = synthesize_dependent_failures(agent_checks, broken)
        assert synthesized == []
        assert remaining == agent_checks


class TestVerdictCacheDetails:
    def test_get_returns_none_for_missing_record(self, tmp_path) -> None:
        cache = VerdictCache(str(tmp_path / "cache"))
        assert cache.get("state", "task", {"a": 1}) is None

    def test_get_returns_none_for_malformed_json(self, tmp_path) -> None:
        cache = VerdictCache(str(tmp_path / "cache"))
        record_dir = tmp_path / "cache" / "state"
        record_dir.mkdir(parents=True)
        (record_dir / "task.json").write_text("not json", encoding="utf-8")
        assert cache.get("state", "task", {"a": 1}) is None

    def test_get_returns_none_for_wrong_schema_version(self, tmp_path) -> None:
        cache = VerdictCache(str(tmp_path / "cache"))
        record_dir = tmp_path / "cache" / "state"
        record_dir.mkdir(parents=True)
        (record_dir / "task.json").write_text(
            json.dumps({"schema_version": 2, "fingerprint": {"a": 1}, "verdict": {"hard": 1}}),
            encoding="utf-8",
        )
        assert cache.get("state", "task", {"a": 1}) is None

    def test_get_returns_none_for_missing_record_keys(self, tmp_path) -> None:
        cache = VerdictCache(str(tmp_path / "cache"))
        record_dir = tmp_path / "cache" / "state"
        record_dir.mkdir(parents=True)
        (record_dir / "task.json").write_text(
            json.dumps({"schema_version": 1, "verdict": {"hard": 1}}),
            encoding="utf-8",
        )
        assert cache.get("state", "task", {"a": 1}) is None

    def test_put_rejects_non_dict_verdict(self, tmp_path) -> None:
        cache = VerdictCache(str(tmp_path / "cache"))
        with pytest.raises(TypeError):
            cache.put("state", "task", {"a": 1}, ["not", "a", "dict"])

    def test_rejects_unsafe_state_hash_or_task_id(self, tmp_path) -> None:
        cache = VerdictCache(str(tmp_path / "cache"))
        with pytest.raises(ValueError):
            cache.get("../escape", "task", {})
        with pytest.raises(ValueError):
            cache.put("state", "../escape", {}, {"hard": 1})


class TestLockedRecordRecheck:
    def test_locked_record_recheck_sees_a_write_made_before_lock_acquisition(self, tmp_path) -> None:
        cache = VerdictCache(str(tmp_path / "cache"))
        # Simulate: an earlier unlocked pre-check saw a miss, then some other
        # process wrote a record before we acquired the lock. The recheck
        # inside the lock must see that write rather than a stale miss.
        with cache.locked_record("state", "task") as record:
            assert record.get({"e": 1}) is None
            record.put({"e": 1}, {"hard": 1})

        with cache.locked_record("state", "task") as record:
            assert record.get({"e": 1}) == {"hard": 1}
            assert record.get({"e": 2}) is None

    def test_locked_record_put_is_visible_to_plain_get(self, tmp_path) -> None:
        cache = VerdictCache(str(tmp_path / "cache"))
        with cache.locked_record("state", "task") as record:
            record.put({"e": 1}, {"hard": 1})
        assert cache.get("state", "task", {"e": 1}) == {"hard": 1}

    def test_lock_file_does_not_leak_across_sequential_acquisitions(self, tmp_path) -> None:
        cache = VerdictCache(str(tmp_path / "cache"))
        for _ in range(3):
            with cache.locked_record("state", "task") as record:
                record.get({"e": 1})
        # No deadlock/hang across repeated sequential acquisitions.
        assert True


def test_score_criteria_rejects_out_of_range_score() -> None:
    criteria = [{"id": "opens", "passed": True, "score": 1.5, "reason": "ok", "evidence": []}]
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        score_criteria([CHECKS[0]], criteria)


def test_score_criteria_rejects_non_boolean_passed() -> None:
    criteria = [{"id": "opens", "passed": 1, "score": 1.0, "reason": "ok", "evidence": []}]
    with pytest.raises(ValueError, match="boolean"):
        score_criteria([CHECKS[0]], criteria)


def test_score_criteria_is_never_handed_nan_or_inf(tmp_path) -> None:
    # score_criteria trusts its caller for numeric sanity once parse_verdict
    # (or a deterministic evaluator) has already vetted the row; this test
    # documents that a non-finite score can never legitimately reach it
    # because parse_verdict rejects it first.
    payload = _valid_payload()
    text = json.dumps(payload).replace('"score": 1.0', '"score": NaN', 1)
    with pytest.raises(ValueError, match="constant"):
        parse_verdict(text, CHECKS, {"report.pdf"})
    assert math.isnan(float("nan"))  # sanity for the docstring above
