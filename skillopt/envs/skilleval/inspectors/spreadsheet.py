"""Bounded spreadsheet structure, checks, and LibreOffice rendering."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree

import openpyxl
from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter

from skillopt.envs.skilleval import artifacts as artifact_security

from ._scratch import current_scratch_transaction, scratch_transaction
from .base import (
    DEFAULT_SCRATCH_BYTES,
    DEFAULT_SCRATCH_DEPTH,
    DEFAULT_SCRATCH_ENTRIES,
    InspectionError,
    RenderBudget,
    ResponseBudget,
    bounded_diagnostic,
    safe_run,
    validate_json_result,
)

_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MAX_OOXML_ENTRIES = 10_000
_MAX_OOXML_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_MAX_CONTENT_TYPES_BYTES = 2 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200.0
_ZIP_STREAM_CHUNK = 1024 * 1024
_CELL_PAGE_SIZE = 128
_LIBREOFFICE_TIMEOUT_SECONDS = 120
_SUPPORTED_ZIP_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_XLSX_WORKBOOK_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "spreadsheetml.sheet.main+xml"
)
_CELL_SELECTOR_RE = re.compile(
    r"^sheet:([^:]+?)(?::page:([1-9][0-9]*))?$"
)
_PDF_SELECTOR_RE = re.compile(r"^page:[1-9][0-9]*$")
_CELL_REF_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})$")
_WINDOWS_ROOT_RE = re.compile(r"^[A-Za-z]:")
_PROC_FD_RE = re.compile(r"^/proc/self/fd/([0-9]+)$")


def _member_name(info: zipfile.ZipInfo) -> str:
    name = getattr(info, "orig_filename", info.filename)
    if not isinstance(name, str) or name != info.filename:
        raise InspectionError("unsafe OOXML entry name")
    return name


def _validate_member_name(info: zipfile.ZipInfo) -> str:
    name = _member_name(info)
    if not artifact_security._safe_zip_member(name):
        raise InspectionError(f"unsafe OOXML entry: {bounded_diagnostic(name)!r}")
    logical_name = name[:-1] if info.is_dir() else name
    parts = logical_name.split("/")
    if (
        not logical_name
        or any(part in {"", ".", ".."} for part in parts)
        or _WINDOWS_ROOT_RE.match(parts[0])
    ):
        raise InspectionError(f"unsafe OOXML entry: {bounded_diagnostic(name)!r}")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InspectionError("unsafe OOXML entry encoding") from exc
    if len(encoded) > 4_096 or any(
        len(part.encode("utf-8")) > 255 for part in parts
    ):
        raise InspectionError("unsafe OOXML entry exceeds path limits")
    return name


def _validate_member_type(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & (0x1 | 0x40):
        raise InspectionError("encrypted OOXML entries are unsupported")
    if info.compress_type not in _SUPPORTED_ZIP_COMPRESSION:
        raise InspectionError(
            f"unsupported OOXML compression method {info.compress_type}"
        )
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if info.create_system == 3 and file_type:
        expected = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
        if file_type != expected:
            raise InspectionError("unsupported non-regular OOXML entry")
    if info.file_size < 0 or info.compress_size < 0:
        raise InspectionError("invalid OOXML entry size metadata")
    if info.file_size:
        if info.compress_size == 0:
            raise InspectionError("suspicious OOXML compression ratio")
        ratio = info.file_size / info.compress_size
        if ratio > _MAX_COMPRESSION_RATIO:
            raise InspectionError(
                "suspicious OOXML compression ratio: "
                f"{ratio:.1f} > {_MAX_COMPRESSION_RATIO:.1f}"
            )


def _validate_content_types(payload: bytes, names: set[str]) -> None:
    if len(payload) > _MAX_CONTENT_TYPES_BYTES:
        raise InspectionError("OOXML content types exceed size limit")
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise InspectionError("OOXML content types contain forbidden declarations")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise InspectionError("OOXML content types are malformed") from exc

    workbook_types = {
        element.attrib.get("ContentType")
        for element in root
        if element.tag.rsplit("}", 1)[-1] == "Override"
        and element.attrib.get("PartName") == "/xl/workbook.xml"
    }
    if (
        workbook_types != {_XLSX_WORKBOOK_CONTENT_TYPE}
        or "xl/workbook.xml" not in names
    ):
        raise InspectionError("OOXML content types do not describe an XLSX workbook")


def _open_stable_descriptor(path: str, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        proc_fd = _PROC_FD_RE.fullmatch(os.fspath(path))
        descriptor = (
            os.dup(int(proc_fd.group(1)))
            if proc_fd is not None
            else os.open(os.fspath(path), flags)
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise InspectionError(f"{label} must be a regular file")
        return descriptor
    except InspectionError:
        raise
    except OSError as exc:
        raise InspectionError(
            f"{label} could not be opened: {bounded_diagnostic(exc)}"
        ) from exc


def preflight_xlsx(path: str) -> None:
    """Validate an XLSX ZIP before a parser opens it."""
    descriptor = _open_stable_descriptor(path, "OOXML workbook")
    try:
        if not artifact_security._preflight_zip_descriptor(descriptor):
            raise InspectionError("OOXML ZIP central directory failed preflight")

        with os.fdopen(os.dup(descriptor), "rb") as source:
            try:
                with zipfile.ZipFile(source) as archive:
                    members = archive.infolist()
                    if len(members) > _MAX_OOXML_ENTRIES:
                        raise InspectionError(
                            "OOXML entry count exceeds maximum "
                            f"{_MAX_OOXML_ENTRIES}"
                        )

                    names: set[str] = set()
                    collision_keys: set[str] = set()
                    declared_total = 0
                    content_types_info: zipfile.ZipInfo | None = None
                    for member in members:
                        name = _validate_member_name(member)
                        collision_key = unicodedata.normalize(
                            "NFC", name
                        ).casefold()
                        if name in names or collision_key in collision_keys:
                            raise InspectionError(
                                "duplicate or colliding OOXML entry name"
                            )
                        names.add(name)
                        collision_keys.add(collision_key)
                        declared_total += member.file_size
                        if declared_total > _MAX_OOXML_UNCOMPRESSED_BYTES:
                            raise InspectionError(
                                "OOXML declared uncompressed bytes exceed maximum "
                                f"{_MAX_OOXML_UNCOMPRESSED_BYTES}"
                            )
                        _validate_member_type(member)
                        if name == "[Content_Types].xml":
                            content_types_info = member

                    if content_types_info is None:
                        raise InspectionError("OOXML content types are missing")
                    if content_types_info.file_size > _MAX_CONTENT_TYPES_BYTES:
                        raise InspectionError(
                            "OOXML content types exceed size limit"
                        )

                    actual_total = 0
                    content_types = bytearray()
                    for member in members:
                        if member.is_dir():
                            if member.file_size != 0:
                                raise InspectionError(
                                    "OOXML directory has invalid size metadata"
                                )
                            continue
                        actual_entry = 0
                        with archive.open(member, "r") as entry:
                            while True:
                                chunk = entry.read(_ZIP_STREAM_CHUNK)
                                if not chunk:
                                    break
                                actual_entry += len(chunk)
                                actual_total += len(chunk)
                                if (
                                    actual_entry > member.file_size
                                    or actual_total
                                    > _MAX_OOXML_UNCOMPRESSED_BYTES
                                ):
                                    raise InspectionError(
                                        "OOXML actual uncompressed bytes exceed "
                                        "declared or configured limits"
                                    )
                                if member is content_types_info:
                                    content_types.extend(chunk)
                        if actual_entry != member.file_size:
                            raise InspectionError(
                                "OOXML entry size metadata does not match content"
                            )
            except InspectionError:
                raise
            except (
                OSError,
                RuntimeError,
                NotImplementedError,
                ValueError,
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
            ) as exc:
                raise InspectionError(
                    f"OOXML ZIP could not be validated: {bounded_diagnostic(exc)}"
                ) from exc
        if actual_total != declared_total:
            raise InspectionError(
                "OOXML total size metadata does not match content"
            )
        _validate_content_types(bytes(content_types), names)
    finally:
        os.close(descriptor)


def _is_legacy_xls(path: str) -> bool:
    descriptor = _open_stable_descriptor(path, "spreadsheet")
    try:
        return os.pread(descriptor, len(_OLE_SIGNATURE), 0) == _OLE_SIGNATURE
    finally:
        os.close(descriptor)


def _find_libreoffice() -> str:
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if executable is None:
        raise InspectionError("LibreOffice is not installed or not on PATH")
    return executable


def _write_libreoffice_profile(profile: Path) -> None:
    user = profile / "user"
    user.mkdir(parents=True)
    config = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
    <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
    <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>
  </item>
  <item oor:path="/org.openoffice.Office.Calc/Content/Update">
    <prop oor:name="Link" oor:op="fuse"><value>2</value></prop>
  </item>
</oor:items>
"""
    (user / "registrymodifications.xcu").write_text(config, encoding="utf-8")
    fontconfig = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <dir>/usr/share/fonts</dir>
  <dir>/usr/local/share/fonts</dir>
  <cachedir>/var/cache/fontconfig</cachedir>
  <include ignore_missing="yes">/etc/fonts/conf.d</include>
  <config><rescan><int>0</int></rescan></config>
</fontconfig>
"""
    (profile / "fontconfig.xml").write_text(fontconfig, encoding="utf-8")


def _convert_with_libreoffice(
    path: str,
    scratch_dir: str,
    target_format: str,
) -> str:
    if current_scratch_transaction() is None:
        raise InspectionError(
            "LibreOffice conversion requires an active scratch transaction"
        )
    if target_format not in {"xlsx", "pdf"}:
        raise InspectionError("unsupported LibreOffice conversion target")

    profile = Path(tempfile.mkdtemp(prefix="lo-profile-", dir=scratch_dir))
    output_dir = Path(tempfile.mkdtemp(prefix="lo-output-", dir=scratch_dir))
    _write_libreoffice_profile(profile)
    env_executable = shutil.which("env")
    if env_executable is None:
        raise InspectionError("env executable is unavailable for LibreOffice")
    input_descriptor = _open_stable_descriptor(
        path,
        "LibreOffice spreadsheet input",
    )
    input_path = f"/proc/self/fd/{input_descriptor}"
    command = [
        env_executable,
        f"FONTCONFIG_FILE={profile / 'fontconfig.xml'}",
        _find_libreoffice(),
        "--headless",
        "--invisible",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--norestore",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to",
        target_format,
        "--outdir",
        str(output_dir),
        input_path,
    ]
    try:
        safe_run(
            command,
            timeout=_LIBREOFFICE_TIMEOUT_SECONDS,
            cwd=scratch_dir,
            home=scratch_dir,
            pass_fds=(input_descriptor,),
        )

        expected = output_dir / f"{input_descriptor}.{target_format}"
        try:
            output_info = expected.lstat()
        except FileNotFoundError as exc:
            raise InspectionError(
                "LibreOffice did not create the expected output "
                f"{expected.name!r}"
            ) from exc
        if not stat.S_ISREG(output_info.st_mode) or output_info.st_nlink != 1:
            raise InspectionError(
                "LibreOffice output is not a single-link regular file"
            )
        if output_info.st_size <= 0:
            raise InspectionError("LibreOffice output is empty")
        if target_format == "pdf":
            try:
                with expected.open("rb") as source:
                    signature = source.read(5)
            except OSError as exc:
                raise InspectionError(
                    "LibreOffice PDF output could not be read: "
                    f"{bounded_diagnostic(exc)}"
                ) from exc
            if signature != b"%PDF-":
                raise InspectionError("LibreOffice output is not a PDF")
        return str(expected)
    finally:
        os.close(input_descriptor)


@contextmanager
def _opened_workbooks(path: str):
    descriptor = -1
    formula_book = None
    cached_book = None
    try:
        descriptor = _open_stable_descriptor(path, "spreadsheet workbook")
        preflight_xlsx(f"/proc/self/fd/{descriptor}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as formula_source:
            formula_book = openpyxl.load_workbook(
                formula_source,
                data_only=False,
                read_only=False,
                keep_links=False,
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as cached_source:
            cached_book = openpyxl.load_workbook(
                cached_source,
                data_only=True,
                read_only=False,
                keep_links=False,
            )
        yield formula_book, cached_book
    except InspectionError:
        raise
    except Exception as exc:
        raise InspectionError(
            f"spreadsheet workbook could not be parsed: {bounded_diagnostic(exc)}"
        ) from exc
    finally:
        if cached_book is not None:
            cached_book.close()
        if formula_book is not None:
            formula_book.close()
        if descriptor >= 0:
            os.close(descriptor)


def _json_scalar(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    return str(value)


def _anchor_metadata(anchor) -> dict:
    if isinstance(anchor, str):
        return {"from": anchor}
    start = getattr(anchor, "_from", None)
    end = getattr(anchor, "to", None)
    metadata = {}
    if start is not None:
        metadata["from"] = (
            f"{get_column_letter(start.col + 1)}{start.row + 1}"
        )
    if end is not None:
        metadata["to"] = f"{get_column_letter(end.col + 1)}{end.row + 1}"
    return metadata


def _sheet_inventory(sheet, total_cells: int, page: int | None) -> dict:
    charts = [
        {
            "type": type(chart).__name__,
            "anchor": _anchor_metadata(getattr(chart, "anchor", None)),
        }
        for chart in getattr(sheet, "_charts", ())
    ]
    images = [
        {
            "kind": "image",
            "type": type(image).__name__,
            "anchor": _anchor_metadata(getattr(image, "anchor", None)),
        }
        for image in getattr(sheet, "_images", ())
    ]
    drawings = [
        {"kind": "chart", **chart}
        for chart in charts
    ] + images
    return {
        "name": sheet.title,
        "max_row": sheet.max_row,
        "max_column": sheet.max_column,
        "used_range": sheet.calculate_dimension(),
        "merged_ranges": [
            str(value) for value in sheet.merged_cells.ranges
        ],
        "charts": charts,
        "drawings": drawings,
        "cells": {},
        "cell_page": {
            "page": page,
            "page_size": _CELL_PAGE_SIZE,
            "total": total_cells,
            "returned": 0,
            "omitted": total_cells,
            "omitted_due_to_budget": 0,
        },
    }


def _iter_content_cells(sheet):
    for cell in sheet._cells.values():
        if getattr(cell, "value", None) is not None:
            yield cell


def _cell_detail(formula_cell, cached_sheet) -> dict:
    cached_cell = cached_sheet._cells.get(
        (formula_cell.row, formula_cell.column)
    )
    cached_value = None if cached_cell is None else cached_cell.value
    formula = (
        formula_cell.value
        if formula_cell.data_type == "f"
        else None
    )
    return {
        "coordinate": formula_cell.coordinate,
        "value": _json_scalar(cached_value),
        "cached_value": _json_scalar(cached_value),
        "formula": formula,
        "number_format": formula_cell.number_format,
        "style_id": formula_cell.style_id,
    }


def _text_chars(value) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(len(key) + _text_chars(item) for key, item in value.items())
    return 0


def _json_bytes(value) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _selector_pages(
    selectors: list[str],
    sheet_names: set[str],
) -> dict[str, int]:
    pages: dict[str, int] = {}
    for selector in selectors:
        match = _CELL_SELECTOR_RE.fullmatch(selector)
        if match is None:
            raise InspectionError(
                "spreadsheet selector must be "
                "'sheet:<name>' or 'sheet:<name>:page:<n>'"
            )
        name = match.group(1)
        if name not in sheet_names:
            raise InspectionError(f"spreadsheet selector names missing sheet {name!r}")
        if name in pages:
            raise InspectionError(
                f"spreadsheet selector repeats sheet {name!r}"
            )
        pages[name] = int(match.group(2) or "1")
    return pages


def _build_structure(
    formula_book,
    cached_book,
    pages: dict[str, int | None],
    budget: ResponseBudget,
    *,
    enforce_extract_chars: bool,
) -> dict:
    cached_sheets = {sheet.title: sheet for sheet in cached_book.worksheets}
    rows: list[dict] = []
    source_sheets = {
        sheet.title: sheet for sheet in formula_book.worksheets
    }
    for name, page in pages.items():
        formula_sheet = source_sheets[name]
        total = sum(1 for _cell in _iter_content_cells(formula_sheet))
        rows.append(_sheet_inventory(formula_sheet, total, page))

    result = {"kind": "spreadsheet", "opens": True, "sheets": rows}
    used_bytes = _json_bytes(result)
    used_chars = _text_chars(result)
    if used_bytes > budget.max_bytes or (
        enforce_extract_chars and used_chars > budget.max_extract_chars
    ):
        return validate_json_result(
            result,
            budget,
            enforce_extract_chars=enforce_extract_chars,
        )

    byte_limit = max(0, budget.max_bytes - 128)
    char_limit = max(0, budget.max_extract_chars - 128)
    for row in rows:
        page = row["cell_page"]["page"]
        if page is None:
            continue
        formula_sheet = source_sheets[row["name"]]
        cached_sheet = cached_sheets[row["name"]]
        start = (page - 1) * _CELL_PAGE_SIZE
        stop = start + _CELL_PAGE_SIZE
        for index, formula_cell in enumerate(
            _iter_content_cells(formula_sheet)
        ):
            if index < start:
                continue
            if index >= stop:
                break
            detail = _cell_detail(formula_cell, cached_sheet)
            coordinate = formula_cell.coordinate
            fragment = {coordinate: detail}
            byte_cost = _json_bytes(fragment) - 2
            if row["cells"]:
                byte_cost += 1
            char_cost = len(coordinate) + _text_chars(detail)
            if (
                used_bytes + byte_cost > byte_limit
                or (
                    enforce_extract_chars
                    and used_chars + char_cost > char_limit
                )
            ):
                row["cell_page"]["omitted_due_to_budget"] += 1
                continue
            row["cells"][coordinate] = detail
            row["cell_page"]["returned"] += 1
            used_bytes += byte_cost
            used_chars += char_cost
        row["cell_page"]["omitted"] = (
            row["cell_page"]["total"]
            - row["cell_page"]["returned"]
        )

    return validate_json_result(
        result,
        budget,
        enforce_extract_chars=enforce_extract_chars,
    )


def _prepare_xlsx(path: str, scratch_dir: str) -> str:
    if _is_legacy_xls(path):
        return _convert_with_libreoffice(path, scratch_dir, "xlsx")
    return path


def _validate_render_selectors(selectors: list[str]) -> None:
    for selector in selectors:
        if _PDF_SELECTOR_RE.fullmatch(selector) is None:
            raise InspectionError(
                "spreadsheet render selector must be 'page:<n>'"
            )


class SpreadsheetInspector:
    """Inspect and render XLS/XLSX artifacts within registry budgets."""

    def inspect(
        self,
        path: str,
        scratch_dir: str,
        *,
        response_budget: ResponseBudget,
    ):
        xlsx_path = _prepare_xlsx(path, scratch_dir)
        with _opened_workbooks(xlsx_path) as (formula_book, cached_book):
            pages = {sheet.title: 1 for sheet in formula_book.worksheets}
            return _build_structure(
                formula_book,
                cached_book,
                pages,
                response_budget,
                enforce_extract_chars=False,
            )

    def extract(
        self,
        path: str,
        scratch_dir: str,
        selectors: list[str],
        *,
        response_budget: ResponseBudget,
    ):
        xlsx_path = _prepare_xlsx(path, scratch_dir)
        with _opened_workbooks(xlsx_path) as (formula_book, cached_book):
            sheet_names = {
                sheet.title for sheet in formula_book.worksheets
            }
            selected = _selector_pages(selectors, sheet_names)
            pages: dict[str, int | None]
            if selected:
                pages = {
                    sheet.title: selected[sheet.title]
                    for sheet in formula_book.worksheets
                    if sheet.title in selected
                }
            else:
                pages = {
                    sheet.title: None
                    for sheet in formula_book.worksheets
                }
            return _build_structure(
                formula_book,
                cached_book,
                pages,
                response_budget,
                enforce_extract_chars=True,
            )

    def render(
        self,
        path: str,
        scratch_dir: str,
        selectors: list[str],
        budget: RenderBudget,
    ) -> list[str]:
        _validate_render_selectors(selectors)
        pdf_path = _convert_with_libreoffice(path, scratch_dir, "pdf")
        try:
            from .pdf_image import PdfInspector
        except (ImportError, ModuleNotFoundError) as exc:
            raise InspectionError(
                "PDF renderer is unavailable for spreadsheet rendering"
            ) from exc
        return PdfInspector().render(pdf_path, scratch_dir, selectors, budget)


def _canonical_cell_ref(value: object) -> tuple[str, int, int]:
    if not isinstance(value, str):
        raise InspectionError("spreadsheet check cell must be a string")
    match = _CELL_REF_RE.fullmatch(value)
    if match is None:
        raise InspectionError(f"invalid spreadsheet cell reference {value!r}")
    row, column = coordinate_to_tuple(value.replace("$", ""))
    if row > 1_048_576 or column > 16_384:
        raise InspectionError(f"spreadsheet cell reference is out of range {value!r}")
    return f"{get_column_letter(column)}{row}", row, column


def _criterion(
    check: dict,
    sheet_name: str,
    cell_ref: str,
    passed: bool,
    reason: str,
) -> dict:
    return {
        "id": check["id"],
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "reason": reason,
        "evidence": [
            {
                "path": check["path"],
                "locator": f"sheet={sheet_name},cell={cell_ref}",
                "source": "structure",
            }
        ],
    }


def _formula_body(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value[1:] if value.startswith("=") else value


def _exact_cell_equal(actual: object, expected: object) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual == expected
    return type(actual) is type(expected) and actual == expected


@contextmanager
def _check_xlsx_path(path: str):
    descriptor = _open_stable_descriptor(path, "spreadsheet check input")
    try:
        stable_path = f"/proc/self/fd/{descriptor}"
        if (
            os.pread(descriptor, len(_OLE_SIGNATURE), 0)
            != _OLE_SIGNATURE
        ):
            yield stable_path
            return
        with tempfile.TemporaryDirectory(
            prefix="skillopt-xls-check-"
        ) as scratch_root:
            with scratch_transaction(
                scratch_root,
                max_bytes=DEFAULT_SCRATCH_BYTES,
                max_entries=DEFAULT_SCRATCH_ENTRIES,
                max_depth=DEFAULT_SCRATCH_DEPTH,
            ) as transaction:
                yield _convert_with_libreoffice(
                    stable_path,
                    transaction.proc_path,
                    "xlsx",
                )
    finally:
        os.close(descriptor)


def evaluate_xlsx_check(path: str, check: dict) -> dict:
    """Evaluate one deterministic XLS/XLSX cell or formula criterion."""
    if not isinstance(check, dict):
        raise InspectionError("spreadsheet check must be an object")
    check_id = check.get("id")
    check_path = check.get("path")
    check_type = check.get("type")
    spec = check.get("spec")
    if not isinstance(check_id, str) or not check_id:
        raise InspectionError("spreadsheet check id must be a non-empty string")
    if not isinstance(check_path, str) or not check_path:
        raise InspectionError("spreadsheet check path must be a non-empty string")
    if check_type not in {"xlsx_cell", "xlsx_formula"}:
        raise InspectionError(f"unsupported spreadsheet check type {check_type!r}")
    if not isinstance(spec, dict):
        raise InspectionError("spreadsheet check spec must be an object")
    sheet_name = spec.get("sheet")
    if not isinstance(sheet_name, str) or not sheet_name:
        raise InspectionError("spreadsheet check sheet must be a non-empty string")
    cell_ref, row, column = _canonical_cell_ref(spec.get("cell"))

    with _check_xlsx_path(path) as xlsx_path:
        with _opened_workbooks(xlsx_path) as (formula_book, cached_book):
            if sheet_name not in formula_book.sheetnames:
                return _criterion(
                    check,
                    sheet_name,
                    cell_ref,
                    False,
                    f"worksheet {sheet_name!r} is missing",
                )
            formula_sheet = formula_book[sheet_name]
            cached_sheet = cached_book[sheet_name]
            formula_cell = formula_sheet._cells.get((row, column))
            cached_cell = cached_sheet._cells.get((row, column))

            if check_type == "xlsx_formula":
                expected = spec.get("formula")
                if not isinstance(expected, str):
                    raise InspectionError(
                        "xlsx_formula check formula must be a string"
                    )
                actual = None if formula_cell is None else formula_cell.value
                if actual is None:
                    return _criterion(
                        check,
                        sheet_name,
                        cell_ref,
                        False,
                        f"cell {cell_ref} is missing or has no formula",
                    )
                passed = (
                    formula_cell.data_type == "f"
                    and _formula_body(actual) == _formula_body(expected)
                )
                reason = (
                    "formula matches exactly"
                    if passed
                    else "formula mismatch: expected "
                    f"{bounded_diagnostic(expected, 200)!r}, found "
                    f"{bounded_diagnostic(actual, 200)!r}"
                )
            else:
                if "value" not in spec:
                    raise InspectionError(
                        "xlsx_cell check value is required"
                    )
                if formula_cell is None and cached_cell is None:
                    return _criterion(
                        check,
                        sheet_name,
                        cell_ref,
                        False,
                        f"cell {cell_ref} is missing",
                    )
                actual = (
                    cached_cell.value
                    if cached_cell is not None
                    else formula_cell.value
                )
                if actual is None:
                    return _criterion(
                        check,
                        sheet_name,
                        cell_ref,
                        False,
                        f"cell {cell_ref} is missing or blank",
                    )
                expected = spec["value"]
                passed = _exact_cell_equal(actual, expected)
                reason = (
                    "cell value matches exactly"
                    if passed
                    else "cell value mismatch: expected "
                    f"{bounded_diagnostic(expected, 200)!r}, found "
                    f"{bounded_diagnostic(actual, 200)!r}"
                )
            return _criterion(
                check,
                sheet_name,
                cell_ref,
                passed,
                reason,
            )


__all__ = [
    "SpreadsheetInspector",
    "evaluate_xlsx_check",
    "preflight_xlsx",
]
