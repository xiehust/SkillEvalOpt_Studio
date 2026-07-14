"""Content-routed registry for trusted artifact inspectors."""
from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from dataclasses import asdict

from skillopt.envs.skilleval.artifacts import (
    build_manifest,
    detect_artifact_kind,
)

from .base import (
    DEFAULT_EXTRACT_CHARS,
    DEFAULT_RESPONSE_BYTES,
    DEFAULT_SCRATCH_BYTES,
    InspectionError,
    MAX_RENDER_PIXELS,
    RenderBudget,
    ResponseBudget,
    bounded_diagnostic,
    normalize_selectors,
    resolve_scratch_path,
    validate_json_result,
    validate_logical_path,
    validate_roots,
)
from ._scratch import enforce_scratch_budget
from ._secure_files import staged_evidence_path

_INSPECTOR_SPECS = {
    "xlsx": (".spreadsheet", "SpreadsheetInspector"),
    "xls": (".spreadsheet", "SpreadsheetInspector"),
    "pdf": (".pdf_image", "PdfInspector"),
    "image": (".pdf_image", "ImageInspector"),
    "doc": (".office", "OfficeInspector"),
    "docx": (".office", "OfficeInspector"),
    "ppt": (".office", "OfficeInspector"),
    "pptx": (".office", "OfficeInspector"),
}
_SUFFIX_KINDS = {
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".pdf": "pdf",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".tif": "image",
    ".tiff": "image",
    ".doc": "doc",
    ".docx": "docx",
    ".ppt": "ppt",
    ".pptx": "pptx",
}
_MAX_RENDER_FILES = 256


def _response_budget(
    max_response_bytes: int,
    max_extract_chars: int = DEFAULT_EXTRACT_CHARS,
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES,
) -> ResponseBudget:
    return ResponseBudget(
        max_bytes=max_response_bytes,
        max_extract_chars=max_extract_chars,
        max_scratch_bytes=max_scratch_bytes,
    )


def _artifact_kind(path: str, logical_path: str) -> str:
    try:
        kind = detect_artifact_kind(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InspectionError(
            "artifact format could not be detected: "
            f"{bounded_diagnostic(exc)}"
        ) from exc
    if kind is None or kind not in _INSPECTOR_SPECS:
        raise InspectionError(
            f"unsupported artifact format: {logical_path!r}"
        )
    suffix = os.path.splitext(logical_path)[1].lower()
    suffix_kind = _SUFFIX_KINDS.get(suffix)
    if suffix_kind is not None and suffix_kind != kind:
        raise InspectionError(
            "artifact content conflicts with its filename format: "
            f"{logical_path!r}"
        )
    return kind


def _load_inspector(kind: str):
    module_name, class_name = _INSPECTOR_SPECS[kind]
    full_name = f"{__name__}{module_name}"
    try:
        module = importlib.import_module(full_name)
        inspector_class = getattr(module, class_name)
        return inspector_class()
    except (AttributeError, ImportError, ModuleNotFoundError) as exc:
        raise InspectionError(
            f"inspector unavailable for artifact kind {kind!r}: "
            f"{bounded_diagnostic(exc)}"
        ) from exc


@contextmanager
def _operation_context(
    logical_path: str,
    evidence_dir: str,
    scratch_dir: str,
    max_scratch_bytes: int,
):
    evidence, scratch = validate_roots(evidence_dir, scratch_dir)
    with enforce_scratch_budget(scratch, max_scratch_bytes) as scratch_guard:
        with staged_evidence_path(
            evidence,
            logical_path,
            scratch,
            scratch_guard,
        ) as path:
            try:
                kind = _artifact_kind(path, logical_path)
                yield path, scratch, _load_inspector(kind)
            finally:
                scratch_guard.check()


def inventory_artifacts(
    evidence_dir: str,
    scratch_dir: str,
    *,
    max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES,
) -> list[dict]:
    """Return a deterministic compact manifest for all evidence files."""
    evidence, _scratch = validate_roots(evidence_dir, scratch_dir)
    budget = _response_budget(
        max_response_bytes,
        max_scratch_bytes=max_scratch_bytes,
    )
    with enforce_scratch_budget(_scratch, budget.max_scratch_bytes):
        try:
            manifest = build_manifest(evidence)
        except (OSError, RuntimeError, ValueError) as exc:
            raise InspectionError(
                f"evidence inventory failed: {bounded_diagnostic(exc)}"
            ) from exc
    rows = []
    for entry in manifest.values():
        validate_logical_path(entry.path)
        row = asdict(entry)
        row["unit_summary"] = {
            "status": "not_inspected",
            "units": [],
        }
        rows.append(row)
    validate_json_result(rows, budget)
    return rows


def inspect_artifact(
    logical_path: str,
    *,
    evidence_dir: str,
    scratch_dir: str,
    max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES,
):
    """Inspect one evidence-relative artifact through its content handler."""
    budget = _response_budget(
        max_response_bytes,
        max_scratch_bytes=max_scratch_bytes,
    )
    with _operation_context(
        logical_path,
        evidence_dir,
        scratch_dir,
        budget.max_scratch_bytes,
    ) as (path, scratch, inspector):
        try:
            result = inspector.inspect(
                path,
                scratch,
                response_budget=budget,
            )
        except InspectionError:
            raise
        except Exception as exc:
            raise InspectionError(
                f"artifact inspection failed: {bounded_diagnostic(exc)}"
            ) from exc
    return validate_json_result(result, budget)


def render_artifact(
    logical_path: str,
    *,
    evidence_dir: str,
    scratch_dir: str,
    selectors: list[str] | None = None,
    max_pixels: int = MAX_RENDER_PIXELS,
    max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES,
) -> list[str]:
    """Render selected units and return validated scratch-local file paths."""
    normalized = normalize_selectors(selectors)
    render_budget = RenderBudget(
        max_pixels=max_pixels,
        max_scratch_bytes=max_scratch_bytes,
    )
    response_budget = _response_budget(
        max_response_bytes,
        max_scratch_bytes=max_scratch_bytes,
    )
    with _operation_context(
        logical_path,
        evidence_dir,
        scratch_dir,
        render_budget.max_scratch_bytes,
    ) as (path, scratch, inspector):
        try:
            outputs = inspector.render(
                path,
                scratch,
                normalized,
                render_budget,
            )
        except InspectionError:
            raise
        except Exception as exc:
            raise InspectionError(
                f"artifact render failed: {bounded_diagnostic(exc)}"
            ) from exc
        if not isinstance(outputs, list) or len(outputs) > _MAX_RENDER_FILES:
            raise InspectionError(
                f"renderer must return at most {_MAX_RENDER_FILES} paths"
            )
        validated: list[str] = []
        for output in outputs:
            if not isinstance(output, str):
                raise InspectionError("renderer returned a non-string path")
            validated.append(resolve_scratch_path(scratch, output))
        validate_json_result(validated, response_budget)
        return validated


def extract_artifact(
    logical_path: str,
    *,
    evidence_dir: str,
    scratch_dir: str,
    selectors: list[str] | None = None,
    max_extract_chars: int = DEFAULT_EXTRACT_CHARS,
    max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES,
):
    """Extract bounded content from selected artifact units."""
    normalized = normalize_selectors(selectors)
    budget = _response_budget(
        max_response_bytes,
        max_extract_chars,
        max_scratch_bytes,
    )
    with _operation_context(
        logical_path,
        evidence_dir,
        scratch_dir,
        budget.max_scratch_bytes,
    ) as (path, scratch, inspector):
        try:
            result = inspector.extract(
                path,
                scratch,
                normalized,
                response_budget=budget,
            )
        except InspectionError:
            raise
        except Exception as exc:
            raise InspectionError(
                f"artifact extraction failed: {bounded_diagnostic(exc)}"
            ) from exc
    return validate_json_result(
        result,
        budget,
        enforce_extract_chars=True,
    )


__all__ = [
    "InspectionError",
    "RenderBudget",
    "ResponseBudget",
    "extract_artifact",
    "inspect_artifact",
    "inventory_artifacts",
    "render_artifact",
]
