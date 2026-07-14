"""Trusted inspector registry, CLI, and Artifact MCP tests."""
from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ImageContent, TextContent
from PIL import Image as PillowImage

from skillopt.envs.skilleval import artifact_mcp
from skillopt.envs.skilleval import inspectors as inspectors_mod
from skillopt.envs.skilleval.inspectors import base as inspector_base
from skillopt.envs.skilleval.inspectors import (
    InspectionError,
    extract_artifact,
    inspect_artifact,
    inventory_artifacts,
    render_artifact,
)
from skillopt.envs.skilleval.inspectors.__main__ import main as artifactctl_main
from skillopt.envs.skilleval.inspectors._scratch import scratch_transaction
from skillopt.envs.skilleval.inspectors._secure_files import open_evidence_file
from skillopt.envs.skilleval.inspectors.base import (
    MAX_COMMAND_OUTPUT_CHARS,
    MAX_LOGICAL_COMPONENTS,
    MAX_LOGICAL_COMPONENT_BYTES,
    MAX_LOGICAL_PATH_BYTES,
    MAX_LOGICAL_PATH_CHARS,
    MAX_RENDER_PIXELS,
    MAX_RESPONSE_BYTES,
    MIN_RESPONSE_BYTES,
    RenderBudget,
    ResponseBudget,
    normalize_selectors,
    safe_run,
    validate_logical_path,
    validate_roots,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    evidence = tmp_path / "evidence"
    scratch = tmp_path / "scratch"
    evidence.mkdir()
    scratch.mkdir()
    return evidence, scratch


def test_package_metadata_targets_stable_mcp_and_python_310() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in pyproject
    assert '"mcp>=1.26,<2"' in pyproject
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "\nmcp>=1.26,<2\n" in f"\n{requirements}"


def _write_pdf(path: Path, body: bytes = b"content") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n" + body)


def _scratch_commit_worker(
    scratch: str,
    barrier,
    entered,
    results,
) -> None:
    try:
        barrier.wait(timeout=5)
        with scratch_transaction(
            scratch,
            max_bytes=100,
            max_entries=20,
            max_depth=4,
        ) as transaction:
            with entered.get_lock():
                entered.value += 1
            deadline = time.monotonic() + 0.5
            while entered.value < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            output = Path(transaction.proc_path) / "render.bin"
            output.write_bytes(b"x" * 80)
            transaction.commit_outputs([str(output)])
        results.put("ok")
    except BaseException as exc:
        results.put(f"error:{type(exc).__name__}:{exc}")


def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return True
        time.sleep(0.02)
    return not Path(f"/proc/{pid}").exists()


class _FakePdfInspector:
    calls: list[tuple] = []
    inspect_value = "UNTRUSTED_META"
    extract_value = "UNTRUSTED_TEXT"
    render_escape: str | None = None
    fail_message: str | None = None
    unreturned_scratch_bytes = 0
    render_bytes: bytes | None = None

    def _write_unreturned(self, scratch_dir):
        if self.unreturned_scratch_bytes:
            (Path(scratch_dir) / "unreturned.bin").write_bytes(
                b"x" * self.unreturned_scratch_bytes
            )

    def inspect(self, path, scratch_dir, *, response_budget):
        self.calls.append(("inspect", path, scratch_dir, response_budget))
        self._write_unreturned(scratch_dir)
        if self.fail_message is not None:
            raise InspectionError(self.fail_message)
        return {
            "filename": Path(path).name,
            "metadata": self.inspect_value,
        }

    def render(self, path, scratch_dir, selectors, budget):
        self.calls.append(("render", path, scratch_dir, selectors, budget))
        self._write_unreturned(scratch_dir)
        if self.fail_message is not None:
            raise InspectionError(self.fail_message)
        if self.render_escape is not None:
            return [self.render_escape]
        output = Path(scratch_dir) / "render-UNTRUSTED_META.png"
        if self.render_bytes is None:
            PillowImage.new("RGB", (2, 3), color=(25, 50, 75)).save(output)
        else:
            output.write_bytes(self.render_bytes)
        return [str(output)]

    def extract(self, path, scratch_dir, selectors, *, response_budget):
        self.calls.append(("extract", path, scratch_dir, selectors, response_budget))
        self._write_unreturned(scratch_dir)
        if self.fail_message is not None:
            raise InspectionError(self.fail_message)
        return {
            "filename": Path(path).name,
            "text": self.extract_value,
            "selectors": selectors,
        }


@pytest.fixture
def fake_pdf_inspector(monkeypatch):
    _FakePdfInspector.calls = []
    _FakePdfInspector.inspect_value = "UNTRUSTED_META"
    _FakePdfInspector.extract_value = "UNTRUSTED_TEXT"
    _FakePdfInspector.render_escape = None
    _FakePdfInspector.fail_message = None
    _FakePdfInspector.unreturned_scratch_bytes = 0
    _FakePdfInspector.render_bytes = None
    module = types.ModuleType("skillopt.envs.skilleval.inspectors.pdf_image")
    module.PdfInspector = _FakePdfInspector
    module.ImageInspector = _FakePdfInspector
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return _FakePdfInspector


class TestBudgetsAndSelectors:
    def test_budget_defaults_are_frozen_and_bounded(self) -> None:
        render = RenderBudget()
        response = ResponseBudget()

        assert render.max_pixels == 500_000_000
        assert response.max_bytes <= MAX_RESPONSE_BYTES
        assert response.max_extract_chars > 0
        with pytest.raises(Exception):
            render.max_pixels = 1  # type: ignore[misc]

    def test_response_budget_enforces_documented_minimum(self) -> None:
        assert MIN_RESPONSE_BYTES >= len(
            b'{"status":"error","error":"InspectionError: bounded failure"}'
        )
        assert ResponseBudget(max_bytes=MIN_RESPONSE_BYTES).max_bytes == (
            MIN_RESPONSE_BYTES
        )
        with pytest.raises(InspectionError, match="at least"):
            ResponseBudget(max_bytes=MIN_RESPONSE_BYTES - 1)

    def test_scratch_budget_is_part_of_inspector_contract(self) -> None:
        default = inspector_base.DEFAULT_SCRATCH_BYTES
        maximum = inspector_base.MAX_SCRATCH_BYTES

        assert default == 1024 * 1024 * 1024
        assert default <= maximum
        assert RenderBudget(max_scratch_bytes=default).max_scratch_bytes == default
        response = ResponseBudget(
            max_scratch_bytes=default,
            max_scratch_entries=10,
            max_scratch_depth=3,
        )
        assert response.max_scratch_bytes == default
        assert response.max_scratch_entries == 10
        assert response.max_scratch_depth == 3
        with pytest.raises(InspectionError, match="scratch"):
            RenderBudget(max_scratch_bytes=maximum + 1)
        with pytest.raises(InspectionError, match="entr"):
            ResponseBudget(max_scratch_entries=0)
        with pytest.raises(InspectionError, match="depth"):
            ResponseBudget(max_scratch_depth=0)

    @pytest.mark.parametrize(
        "selectors",
        [
            "page:1",
            [""],
            ["\x00"],
            [1],
            ["x" * 257],
            ["x"] * 257,
        ],
    )
    def test_invalid_selectors_are_rejected(self, selectors) -> None:
        with pytest.raises(InspectionError, match="selector"):
            normalize_selectors(selectors)

    def test_valid_selectors_are_copied(self) -> None:
        selectors = ["page:1", "sheet:Summary"]
        normalized = normalize_selectors(selectors)
        selectors.append("page:2")
        assert normalized == ["page:1", "sheet:Summary"]


class TestSafeRun:
    def test_uses_minimal_environment_and_explicit_cwd(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        home = tmp_path / "home"
        cwd = tmp_path / "cwd"
        home.mkdir()
        cwd.mkdir()
        script = (
            "import json, os; "
            "print(json.dumps({'keys': sorted(os.environ), "
            "'cwd': os.getcwd(), 'home': os.environ['HOME']}))"
        )

        proc = safe_run(
            [sys.executable, "-c", script],
            timeout=5,
            cwd=str(cwd),
            home=str(home),
        )
        payload = json.loads(proc.stdout)

        assert payload == {
            "keys": ["HOME", "LANG", "PATH"],
            "cwd": str(cwd),
            "home": str(home),
        }

    def test_never_invokes_a_shell(self, tmp_path) -> None:
        cwd = tmp_path / "cwd"
        home = tmp_path / "home"
        cwd.mkdir()
        home.mkdir()
        marker = tmp_path / "shell-ran"
        argument = f"; touch {marker}"

        proc = safe_run(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", argument],
            timeout=5,
            cwd=str(cwd),
            home=str(home),
        )

        assert argument in proc.stdout
        assert not marker.exists()

    @pytest.mark.parametrize("command", [[], [""], ["bad\x00arg"], [1]])
    def test_rejects_invalid_command_arguments(self, tmp_path, command) -> None:
        with pytest.raises(InspectionError, match="invalid inspector command"):
            safe_run(
                command,
                timeout=5,
                cwd=str(tmp_path),
                home=str(tmp_path),
            )

    def test_timeout_is_wrapped_and_bounded(self, tmp_path) -> None:
        with pytest.raises(InspectionError, match="timed out") as excinfo:
            safe_run(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                timeout=0.05,
                cwd=str(tmp_path),
                home=str(tmp_path),
            )
        assert len(str(excinfo.value)) < 1_000

    def test_oserror_is_wrapped(self, tmp_path) -> None:
        with pytest.raises(InspectionError, match="could not start"):
            safe_run(
                [str(tmp_path / "missing-command")],
                timeout=1,
                cwd=str(tmp_path),
                home=str(tmp_path),
            )

    def test_nonzero_diagnostic_is_bounded(self, tmp_path) -> None:
        script = "import sys; sys.stderr.write('S' * 200000); raise SystemExit(7)"
        with pytest.raises(InspectionError, match="exited 7") as excinfo:
            safe_run(
                [sys.executable, "-c", script],
                timeout=5,
                cwd=str(tmp_path),
                home=str(tmp_path),
            )
        assert len(str(excinfo.value)) <= MAX_COMMAND_OUTPUT_CHARS + 200

    def test_success_output_is_bounded(self, tmp_path) -> None:
        script = (
            "import sys; "
            "sys.stdout.write('O' * 200000); "
            "sys.stderr.write('E' * 200000)"
        )
        proc = safe_run(
            [sys.executable, "-c", script],
            timeout=5,
            cwd=str(tmp_path),
            home=str(tmp_path),
        )

        assert len(proc.stdout) <= MAX_COMMAND_OUTPUT_CHARS + 100
        assert len(proc.stderr) <= MAX_COMMAND_OUTPUT_CHARS + 100
        assert "truncated" in proc.stdout
        assert "truncated" in proc.stderr

    def test_normal_parent_exit_terminates_lingering_process_group(
        self, tmp_path
    ) -> None:
        pid_file = tmp_path / "child.pid"
        script = (
            "import pathlib, subprocess, sys; "
            "child = subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
        )
        child_pid = None
        started = time.monotonic()
        try:
            safe_run(
                [sys.executable, "-c", script, str(pid_file)],
                timeout=5,
                cwd=str(tmp_path),
                home=str(tmp_path),
            )
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    break
                if state == "Z":
                    break
                time.sleep(0.02)
            else:
                pytest.fail("lingering child process remained alive")
            assert time.monotonic() - started < 3
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_setsid_descendant_is_reaped_after_parent_exits(
        self, tmp_path
    ) -> None:
        pid_file = tmp_path / "setsid-child.pid"
        child_code = (
            "import os, pathlib, sys, time; "
            "os.setsid(); "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "time.sleep(60)"
        )
        parent_code = (
            "import pathlib, subprocess, sys, time; "
            "subprocess.Popen("
            "[sys.executable, '-c', sys.argv[1], sys.argv[2]], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL); "
            "pid_file = pathlib.Path(sys.argv[2]); "
            "[(time.sleep(0.01)) for _ in range(200) "
            "if not pid_file.exists()]"
        )
        child_pid = None
        try:
            safe_run(
                [
                    sys.executable,
                    "-c",
                    parent_code,
                    child_code,
                    str(pid_file),
                ],
                timeout=5,
                cwd=str(tmp_path),
                home=str(tmp_path),
            )
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and Path(
                f"/proc/{child_pid}"
            ).exists():
                time.sleep(0.02)
            assert not Path(f"/proc/{child_pid}").exists()
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @pytest.mark.parametrize(
        "termination_signal",
        [signal.SIGTERM, signal.SIGINT],
    )
    def test_caller_termination_removes_supervisor_and_payload(
        self, tmp_path, termination_signal
    ) -> None:
        payload_pid_file = tmp_path / "payload.pid"
        payload_code = (
            "import os, pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "time.sleep(60)"
        )
        caller_code = (
            "import sys; "
            "from skillopt.envs.skilleval.inspectors.base import safe_run; "
            "safe_run([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
            "timeout=120, cwd=sys.argv[3], home=sys.argv[3])"
        )
        caller = subprocess.Popen(
            [
                sys.executable,
                "-c",
                caller_code,
                payload_code,
                str(payload_pid_file),
                str(tmp_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        supervisor_pid = None
        payload_pid = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                children_path = Path(
                    f"/proc/{caller.pid}/task/{caller.pid}/children"
                )
                if children_path.exists():
                    children = children_path.read_text(
                        encoding="ascii"
                    ).split()
                    if children:
                        supervisor_pid = int(children[0])
                if payload_pid_file.exists():
                    payload_pid = int(
                        payload_pid_file.read_text(encoding="ascii")
                    )
                if supervisor_pid is not None and payload_pid is not None:
                    break
                time.sleep(0.02)
            assert supervisor_pid is not None
            assert payload_pid is not None

            os.kill(caller.pid, termination_signal)
            caller.wait(timeout=5)

            assert _wait_for_pid_exit(supervisor_pid)
            assert _wait_for_pid_exit(payload_pid)
        finally:
            if caller.poll() is None:
                caller.kill()
                caller.wait()
            for pid in (payload_pid, supervisor_pid):
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


class TestSecurePaths:
    def test_logical_path_boundaries_are_accepted(self) -> None:
        max_total = "/".join(["a" * 240] * 17)
        assert len(max_total) == MAX_LOGICAL_PATH_CHARS
        assert len(max_total.encode("utf-8")) == MAX_LOGICAL_PATH_BYTES
        assert validate_logical_path(max_total) == tuple(
            ["a" * 240] * 17
        )
        assert len(validate_logical_path("a/" * 63 + "a")) == (
            MAX_LOGICAL_COMPONENTS
        )
        assert validate_logical_path("a" * MAX_LOGICAL_COMPONENT_BYTES) == (
            "a" * MAX_LOGICAL_COMPONENT_BYTES,
        )

    @pytest.mark.parametrize(
        ("logical_path", "message"),
        [
            ("/".join(["a" * 240] * 16 + ["a" * 241]), "character"),
            ("/".join(["界" * 85] * 17), "UTF-8"),
            ("/".join(["a"] * 65), "component count"),
            ("界" * 86, "component.*UTF-8"),
            ("\ud800", "valid UTF-8"),
        ],
    )
    def test_logical_path_resource_limits_are_rejected(
        self, logical_path, message
    ) -> None:
        with pytest.raises(InspectionError, match=message):
            validate_logical_path(logical_path)

    @pytest.mark.parametrize(
        "logical_path",
        [
            "../outside.pdf",
            "nested/../outside.pdf",
            "/absolute.pdf",
            r"nested\report.pdf",
            "nested//report.pdf",
            "./report.pdf",
            "report.pdf\x00suffix",
            "",
        ],
    )
    def test_rejects_unsafe_logical_paths(self, tmp_path, logical_path) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")

        with pytest.raises(InspectionError, match="logical|relative"):
            inspect_artifact(
                logical_path,
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
            )

    def test_rejects_overlapping_roots(self, tmp_path) -> None:
        evidence = tmp_path / "evidence"
        scratch = evidence / "scratch"
        scratch.mkdir(parents=True)

        with pytest.raises(InspectionError, match="overlap"):
            validate_roots(str(evidence), str(scratch))

    def test_rejects_symlinked_root_component(self, tmp_path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        evidence = tmp_path / "evidence"
        evidence.symlink_to(real, target_is_directory=True)
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        with pytest.raises(InspectionError, match="symlink"):
            validate_roots(str(evidence), str(scratch))

    def test_rejects_symlinked_artifact_component(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        _write_pdf(outside / "report.pdf")
        (evidence / "linked").symlink_to(outside, target_is_directory=True)

        with pytest.raises(InspectionError, match="symlink"):
            with open_evidence_file(str(evidence), "linked/report.pdf"):
                pass

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
    def test_rejects_nonregular_artifact(self, tmp_path) -> None:
        evidence, _scratch = _roots(tmp_path)
        os.mkfifo(evidence / "pipe.pdf")

        with pytest.raises(InspectionError, match="regular"):
            with open_evidence_file(str(evidence), "pipe.pdf"):
                pass

    def test_unstable_path_resolvers_are_not_public(self) -> None:
        assert not hasattr(inspector_base, "resolve_evidence_path")
        assert not hasattr(inspector_base, "resolve_scratch_path")

    def test_rejects_render_output_escape(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        escaped = tmp_path / "escaped.png"
        PillowImage.new("RGB", (1, 1)).save(escaped)
        fake_pdf_inspector.render_escape = str(escaped)

        with pytest.raises(InspectionError, match="scratch"):
            render_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
            )

    def test_rejects_symlinked_render_output(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        outside = tmp_path / "outside.png"
        PillowImage.new("RGB", (1, 1)).save(outside)
        linked = scratch / "linked.png"
        linked.symlink_to(outside)
        fake_pdf_inspector.render_escape = str(linked)

        with pytest.raises(InspectionError, match="symlink"):
            render_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
            )

    def test_png_read_uses_open_directory_descriptors_during_replacement(
        self, tmp_path, monkeypatch
    ) -> None:
        _evidence, scratch = _roots(tmp_path)
        nested = scratch / "nested"
        nested.mkdir()
        original = nested / "render.png"
        PillowImage.new("RGB", (2, 3), color=(1, 2, 3)).save(original)
        outside = tmp_path / "outside"
        outside.mkdir()
        PillowImage.new("RGB", (9, 9), color=(4, 5, 6)).save(
            outside / "render.png"
        )
        held = scratch / "held"
        real_open = os.open
        replaced = False

        def replacing_open(path, flags, *args, **kwargs):
            nonlocal replaced
            dir_fd = kwargs.get("dir_fd")
            if not replaced and os.fspath(path) == "nested" and dir_fd is not None:
                descriptor = real_open(path, flags, *args, **kwargs)
                nested.rename(held)
                nested.symlink_to(outside, target_is_directory=True)
                replaced = True
                return descriptor
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", replacing_open)
        _data, details = artifact_mcp._read_png(
            str(original),
            str(scratch),
            remaining_pixels=100,
            remaining_bytes=10_000,
        )

        assert replaced is True
        assert details["width"] == 2
        assert details["height"] == 3

    @pytest.mark.parametrize("raw_suffix", ["nested/../render.png", "/render.png"])
    def test_rejects_non_normalized_absolute_render_output(
        self, tmp_path, fake_pdf_inspector, raw_suffix
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        output = scratch / "render.png"
        PillowImage.new("RGB", (1, 1)).save(output)
        if raw_suffix.startswith("/"):
            returned = f"{scratch}/{raw_suffix}"
        else:
            returned = f"{scratch}/{raw_suffix}"
        fake_pdf_inspector.render_escape = returned

        with pytest.raises(InspectionError, match="normalized|component"):
            render_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
            )


class TestRegistry:
    def test_unknown_format_is_rejected(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        (evidence / "data.bin").write_bytes(b"\x00\x01")

        with pytest.raises(InspectionError, match="unsupported"):
            inspect_artifact(
                "data.bin",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
            )

    def test_conflicting_content_and_suffix_are_rejected(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "workbook.xlsx")

        with pytest.raises(InspectionError, match="conflict"):
            inspect_artifact(
                "workbook.xlsx",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
            )

    def test_concrete_modules_are_loaded_only_when_used(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        sys.modules.pop("skillopt.envs.skilleval.inspectors.spreadsheet", None)
        sys.modules.pop("skillopt.envs.skilleval.inspectors.office", None)

        inventory_artifacts(str(evidence), str(scratch))
        assert "skillopt.envs.skilleval.inspectors.spreadsheet" not in sys.modules
        assert "skillopt.envs.skilleval.inspectors.office" not in sys.modules

        result = inspect_artifact(
            "report.pdf",
            evidence_dir=str(evidence),
            scratch_dir=str(scratch),
        )
        assert result["metadata"] == "UNTRUSTED_META"
        assert fake_pdf_inspector.calls[0][0] == "inspect"

    def test_missing_concrete_module_fails_only_for_its_kind(
        self, tmp_path, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        real_import = inspectors_mod.importlib.import_module

        def missing_pdf(name):
            if name.endswith(".pdf_image"):
                raise ModuleNotFoundError("optional parser missing")
            return real_import(name)

        monkeypatch.setattr(inspectors_mod.importlib, "import_module", missing_pdf)
        assert inventory_artifacts(str(evidence), str(scratch))[0]["kind"] == "pdf"
        with pytest.raises(InspectionError, match="unavailable"):
            inspect_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
            )

    def test_inventory_is_deterministic_and_complete(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "z-report.pdf", b"z")
        _write_pdf(evidence / "nested" / "a-report.pdf", b"a")
        (evidence / "notes.unknown").write_text("notes", encoding="utf-8")

        first = inventory_artifacts(str(evidence), str(scratch))
        second = inventory_artifacts(str(evidence), str(scratch))

        assert first == second
        assert [row["path"] for row in first] == [
            "nested/a-report.pdf",
            "notes.unknown",
            "z-report.pdf",
        ]
        assert first[0]["sha256"] == hashlib.sha256(
            (evidence / "nested" / "a-report.pdf").read_bytes()
        ).hexdigest()
        for row in first:
            assert set(row) == {
                "path",
                "size",
                "sha256",
                "mime",
                "kind",
                "unit_summary",
            }
            assert row["unit_summary"] == {
                "status": "not_inspected",
                "units": [],
            }

    def test_inventory_rejects_paths_deeper_than_tool_path_contract(
        self, tmp_path
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        too_deep = evidence.joinpath(*(["a"] * (MAX_LOGICAL_COMPONENTS + 1)))
        too_deep.mkdir(parents=True)
        _write_pdf(too_deep / "report.pdf")

        with pytest.raises(InspectionError, match="component count"):
            inventory_artifacts(str(evidence), str(scratch))

    def test_inspection_uses_stable_copy_when_evidence_directory_is_replaced(
        self, tmp_path, fake_pdf_inspector, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        nested = evidence / "nested"
        _write_pdf(nested / "report.pdf", b"ORIGINAL")
        outside = tmp_path / "outside"
        _write_pdf(outside / "report.pdf", b"ESCAPED")
        held = evidence / "held"
        real_detect = inspectors_mod.detect_artifact_kind
        replaced = False

        def replacing_detect(path):
            nonlocal replaced
            if not replaced:
                nested.rename(held)
                nested.symlink_to(outside, target_is_directory=True)
                replaced = True
            return real_detect(path)

        def reading_inspect(self, path, scratch_dir, *, response_budget):
            return {
                "payload": Path(path).read_bytes().decode(
                    "ascii",
                    errors="replace",
                )
            }

        monkeypatch.setattr(
            inspectors_mod,
            "detect_artifact_kind",
            replacing_detect,
        )
        monkeypatch.setattr(_FakePdfInspector, "inspect", reading_inspect)

        result = inspect_artifact(
            "nested/report.pdf",
            evidence_dir=str(evidence),
            scratch_dir=str(scratch),
        )

        assert replaced is True
        assert "ORIGINAL" in result["payload"]
        assert "ESCAPED" not in result["payload"]

    def test_staged_file_replacement_cannot_change_inspector_input(
        self, tmp_path, fake_pdf_inspector, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf", b"ORIGINAL")
        outside = tmp_path / "outside.pdf"
        _write_pdf(outside, b"ESCAPED")
        real_detect = inspectors_mod.detect_artifact_kind
        observed = {}

        def replace_after_detection(path):
            kind = real_detect(path)
            if path.startswith("/proc/self/fd/"):
                proc_dir, basename = os.path.split(path)
                staged = Path(os.readlink(proc_dir)) / basename
            else:
                staged = Path(path)
            staged.unlink()
            staged.symlink_to(outside)
            return kind

        def reading_inspect(self, path, scratch_dir, *, response_budget):
            payload = Path(path).read_bytes().decode(
                "ascii",
                errors="replace",
            )
            observed["payload"] = payload
            return {"payload": payload}

        monkeypatch.setattr(
            inspectors_mod,
            "detect_artifact_kind",
            replace_after_detection,
        )
        monkeypatch.setattr(_FakePdfInspector, "inspect", reading_inspect)

        with pytest.raises(InspectionError, match="scratch"):
            inspect_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
            )

        assert "ORIGINAL" in observed["payload"]
        assert "ESCAPED" not in observed["payload"]

    def test_legacy_detection_fallback_never_reopens_staged_name(
        self, tmp_path, fake_pdf_inspector, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        (evidence / "report.doc").write_bytes(ole + b"ORIGINAL")
        outside = tmp_path / "outside.doc"
        outside.write_bytes(ole + b"ESCAPED")
        office = types.ModuleType(
            "skillopt.envs.skilleval.inspectors.office"
        )
        office.OfficeInspector = _FakePdfInspector
        monkeypatch.setitem(sys.modules, office.__name__, office)
        real_open = os.open
        reopened = False
        observed = {}

        def tracking_open(path, flags, *args, **kwargs):
            nonlocal reopened
            if (
                isinstance(path, str)
                and path.startswith("/proc/self/fd/")
                and path.endswith("/report.doc")
            ):
                reopened = True
            return real_open(path, flags, *args, **kwargs)

        def replacing_detect(path, mime=None):
            if mime is not None:
                return "doc"
            proc_dir, basename = os.path.split(path)
            staged = Path(os.readlink(proc_dir)) / basename
            staged.unlink()
            staged.symlink_to(outside)
            return None

        def reading_inspect(self, path, scratch_dir, *, response_budget):
            observed["payload"] = Path(path).read_bytes()
            return {"ok": True}

        monkeypatch.setattr(inspectors_mod.os, "open", tracking_open)
        monkeypatch.setattr(
            inspectors_mod,
            "detect_artifact_kind",
            replacing_detect,
        )
        monkeypatch.setattr(_FakePdfInspector, "inspect", reading_inspect)

        with pytest.raises(InspectionError, match="scratch"):
            inspect_artifact(
                "report.doc",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
            )

        assert reopened is False
        assert b"ORIGINAL" in observed["payload"]
        assert b"ESCAPED" not in observed["payload"]

    def test_inventory_response_limit_fails_closed(self, tmp_path) -> None:
        evidence, scratch = _roots(tmp_path)
        for index in range(10):
            _write_pdf(evidence / f"report-{index}.pdf")

        with pytest.raises(InspectionError, match="response"):
            inventory_artifacts(
                str(evidence),
                str(scratch),
                max_response_bytes=100,
            )

    def test_registry_validates_json_and_response_bounds(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        fake_pdf_inspector.inspect_value = object()

        with pytest.raises(InspectionError, match="JSON-compatible"):
            inspect_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
            )

        fake_pdf_inspector.inspect_value = "x" * 2_000
        with pytest.raises(InspectionError, match="response"):
            inspect_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                max_response_bytes=100,
            )

    def test_render_and_extract_validate_selectors_and_budgets(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")

        paths = render_artifact(
            "report.pdf",
            evidence_dir=str(evidence),
            scratch_dir=str(scratch),
            selectors=["page:1"],
            max_pixels=1_000,
        )
        extracted = extract_artifact(
            "report.pdf",
            evidence_dir=str(evidence),
            scratch_dir=str(scratch),
            selectors=["page:1"],
            max_extract_chars=1_000,
        )

        assert Path(paths[0]).is_relative_to(scratch)
        assert extracted["selectors"] == ["page:1"]
        assert fake_pdf_inspector.calls[-2][3] == ["page:1"]
        assert fake_pdf_inspector.calls[-2][4] == RenderBudget(1_000)
        assert fake_pdf_inspector.calls[-1][3] == ["page:1"]

    @pytest.mark.parametrize(
        ("function", "kwargs"),
        [
            (render_artifact, {"max_pixels": 0}),
            (render_artifact, {"max_pixels": MAX_RENDER_PIXELS + 1}),
            (inspect_artifact, {"max_response_bytes": 0}),
            (inspect_artifact, {"max_response_bytes": MAX_RESPONSE_BYTES + 1}),
            (extract_artifact, {"max_extract_chars": 0}),
        ],
    )
    def test_registry_rejects_invalid_budgets(
        self, tmp_path, fake_pdf_inspector, function, kwargs
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")

        with pytest.raises(InspectionError, match="budget|positive|maximum"):
            function(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                **kwargs,
            )

    def test_scratch_budget_counts_preexisting_and_unreturned_files(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        (scratch / "preexisting.bin").write_bytes(b"p" * 65)

        with pytest.raises(InspectionError, match="scratch"):
            inspect_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                max_scratch_bytes=64,
            )
        assert fake_pdf_inspector.calls == []

        (scratch / "preexisting.bin").unlink()
        fake_pdf_inspector.unreturned_scratch_bytes = 128
        with pytest.raises(InspectionError, match="scratch"):
            render_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                max_scratch_bytes=100,
        )
        assert not (scratch / "unreturned.bin").exists()

    def test_scratch_budget_counts_staged_input_and_outputs_at_peak(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf", b"e" * 60)
        fake_pdf_inspector.unreturned_scratch_bytes = 50

        with pytest.raises(InspectionError, match="scratch"):
            inspect_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                max_scratch_bytes=100,
            )

        assert not (scratch / "unreturned.bin").exists()

    def test_scratch_transaction_limits_external_write_and_rolls_back(
        self, tmp_path, fake_pdf_inspector, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        escaped_marker = tmp_path / "writer-completed"
        script = (
            "from pathlib import Path; "
            "Path('oversized.bin').write_bytes(b'x' * (5 * 1024 * 1024)); "
            f"Path({str(escaped_marker)!r}).write_text('completed')"
        )

        def process_inspect(self, path, scratch_dir, *, response_budget):
            safe_run(
                [sys.executable, "-c", script],
                timeout=5,
                cwd=scratch_dir,
                home=scratch_dir,
            )
            return {"ok": True}

        monkeypatch.setattr(_FakePdfInspector, "inspect", process_inspect)

        with pytest.raises(InspectionError, match="scratch|exited"):
            inspect_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                max_scratch_bytes=1024,
            )

        assert not escaped_marker.exists()
        assert list(scratch.iterdir()) == []

    def test_scratch_root_escape_is_budgeted_and_rolled_back(
        self, tmp_path, fake_pdf_inspector, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf", b"")
        script = (
            "from pathlib import Path; "
            "Path('../escape-a.bin').write_bytes(b'a' * 40); "
            "Path('../escape-b.bin').write_bytes(b'b' * 40)"
        )

        def process_inspect(self, path, scratch_dir, *, response_budget):
            safe_run(
                [sys.executable, "-c", script],
                timeout=5,
                cwd=scratch_dir,
                home=scratch_dir,
            )
            return {"ok": True}

        monkeypatch.setattr(_FakePdfInspector, "inspect", process_inspect)

        with pytest.raises(InspectionError, match="scratch"):
            inspect_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                max_scratch_bytes=64,
            )

        assert list(scratch.iterdir()) == []

    def test_stable_evidence_fd_is_inherited_by_real_child(
        self, tmp_path, fake_pdf_inspector, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf", b"ORIGINAL")

        def process_inspect(self, path, scratch_dir, *, response_budget):
            process = safe_run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import sys; "
                        "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())"
                    ),
                    path,
                ],
                timeout=5,
                cwd=scratch_dir,
                home=scratch_dir,
            )
            return {"payload": process.stdout}

        monkeypatch.setattr(_FakePdfInspector, "inspect", process_inspect)

        result = inspect_artifact(
            "report.pdf",
            evidence_dir=str(evidence),
            scratch_dir=str(scratch),
        )

        assert "ORIGINAL" in result["payload"]
        assert list(scratch.iterdir()) == []

    def test_scratch_root_budget_serializes_process_transactions(
        self, tmp_path
    ) -> None:
        _evidence, scratch = _roots(tmp_path)
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        entered = context.Value("i", 0)
        results = context.Queue()
        processes = [
            context.Process(
                target=_scratch_commit_worker,
                args=(str(scratch), barrier, entered, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        try:
            for process in processes:
                process.join(timeout=10)
                assert not process.is_alive()
                assert process.exitcode == 0
            outcomes = sorted(
                [results.get(timeout=2), results.get(timeout=2)]
            )
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                    process.join()

        assert outcomes[0].startswith("error:InspectionError:scratch")
        assert outcomes[1] == "ok"
        files = [path for path in scratch.rglob("*") if path.is_file()]
        assert len(files) == 1
        assert files[0].stat().st_size == 80

    @pytest.mark.parametrize(
        ("limit_name", "limit_value", "script"),
        [
            (
                "max_scratch_entries",
                3,
                (
                    "from pathlib import Path; import time; "
                    "[(Path(f'entry-{i}').touch(), time.sleep(0.005)) "
                    "for i in range(100)]; "
                ),
            ),
            (
                "max_scratch_depth",
                2,
                (
                    "from pathlib import Path; import time; "
                    "Path('a/b/c').mkdir(parents=True); time.sleep(1); "
                ),
            ),
        ],
    )
    def test_external_watchdog_limits_entries_and_depth(
        self,
        tmp_path,
        fake_pdf_inspector,
        monkeypatch,
        limit_name,
        limit_value,
        script,
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        completed = tmp_path / f"{limit_name}-completed"
        command = script + f"Path({str(completed)!r}).write_text('completed')"

        def process_inspect(self, path, scratch_dir, *, response_budget):
            safe_run(
                [sys.executable, "-c", command],
                timeout=5,
                cwd=scratch_dir,
                home=scratch_dir,
            )
            return {"ok": True}

        monkeypatch.setattr(_FakePdfInspector, "inspect", process_inspect)

        with pytest.raises(InspectionError, match="scratch"):
            inspect_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                **{limit_name: limit_value},
            )

        assert not completed.exists()
        assert list(scratch.iterdir()) == []

    @pytest.mark.parametrize(
        ("limit_name", "limit_value", "writer"),
        [
            (
                "max_scratch_entries",
                3,
                lambda root: [
                    (root / f"entry-{index}").write_bytes(b"")
                    for index in range(4)
                ],
            ),
            (
                "max_scratch_depth",
                2,
                lambda root: (root / "a" / "b" / "c").mkdir(parents=True),
            ),
        ],
    )
    def test_scratch_transaction_limits_entries_and_depth(
        self,
        tmp_path,
        fake_pdf_inspector,
        monkeypatch,
        limit_name,
        limit_value,
        writer,
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")

        def writing_inspect(self, path, scratch_dir, *, response_budget):
            writer(Path(scratch_dir))
            return {"ok": True}

        monkeypatch.setattr(_FakePdfInspector, "inspect", writing_inspect)

        with pytest.raises(InspectionError, match="scratch"):
            inspect_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                **{limit_name: limit_value},
            )

        assert list(scratch.iterdir()) == []

    def test_success_commits_only_returned_render_outputs(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        fake_pdf_inspector.unreturned_scratch_bytes = 10

        outputs = render_artifact(
            "report.pdf",
            evidence_dir=str(evidence),
            scratch_dir=str(scratch),
        )

        files = sorted(path for path in scratch.rglob("*") if path.is_file())
        assert files == [Path(outputs[0])]
        assert files[0].exists()

    def test_render_commit_respects_root_depth_budget(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")

        with pytest.raises(InspectionError, match="scratch depth"):
            render_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                max_scratch_depth=1,
            )

        assert list(scratch.iterdir()) == []

    def test_failed_response_budget_rolls_back_committed_outputs(
        self, tmp_path, fake_pdf_inspector, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")

        def render_many(self, path, scratch_dir, selectors, budget):
            outputs = []
            for index in range(16):
                output = Path(scratch_dir) / (
                    f"{index:02d}-" + "x" * 80 + ".png"
                )
                PillowImage.new("RGB", (1, 1)).save(output)
                outputs.append(str(output))
            return outputs

        monkeypatch.setattr(_FakePdfInspector, "render", render_many)

        with pytest.raises(InspectionError, match="response"):
            render_artifact(
                "report.pdf",
                evidence_dir=str(evidence),
                scratch_dir=str(scratch),
                max_response_bytes=MIN_RESPONSE_BYTES,
            )

        assert list(scratch.iterdir()) == []

    def test_failed_transaction_entry_does_not_leak_root_descriptor(
        self, tmp_path
    ) -> None:
        _evidence, scratch = _roots(tmp_path)
        (scratch / "existing.bin").write_bytes(b"x")
        before = set(os.listdir("/proc/self/fd"))

        for _ in range(3):
            with pytest.raises(InspectionError, match="scratch"):
                with scratch_transaction(
                    str(scratch),
                    max_bytes=0,
                    max_entries=10,
                    max_depth=2,
                ):
                    pytest.fail("over-budget transaction entered")

        assert set(os.listdir("/proc/self/fd")) == before


class TestArtifactCtl:
    def _run(self, capsys, argv):
        code = artifactctl_main(argv)
        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        assert len(lines) == 1
        assert captured.err == ""
        return code, json.loads(lines[0])

    def test_inventory_command(self, tmp_path, capsys) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")

        code, payload = self._run(
            capsys,
            [
                "inventory",
                "--evidence",
                str(evidence),
                "--scratch",
                str(scratch),
            ],
        )

        assert code == 0
        assert payload["status"] == "ok"
        assert payload["result"][0]["path"] == "report.pdf"

    @pytest.mark.parametrize(
        ("command", "patched_name", "result"),
        [
            ("inspect", "inspect_artifact", {"metadata": "ok"}),
            ("render", "render_artifact", ["/trusted/scratch/render.png"]),
            ("extract", "extract_artifact", {"text": "ok"}),
        ],
    )
    def test_artifact_commands_emit_one_json_object(
        self,
        tmp_path,
        capsys,
        monkeypatch,
        command,
        patched_name,
        result,
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        cli_module = sys.modules[
            "skillopt.envs.skilleval.inspectors.__main__"
        ]
        monkeypatch.setattr(cli_module, patched_name, lambda *args, **kwargs: result)
        argv = [
            command,
            "report.pdf",
            "--evidence",
            str(evidence),
            "--scratch",
            str(scratch),
            "--selector",
            "page:1",
        ]
        if command == "render":
            argv.extend(["--max-pixels", "1000"])

        code, payload = self._run(capsys, argv)

        assert code == 0
        assert payload == {"status": "ok", "result": result}

    def test_operation_error_is_one_bounded_json_object(
        self, tmp_path, capsys
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        (evidence / "UNTRUSTED.bin").write_bytes(b"unknown")

        code, payload = self._run(
            capsys,
            [
                "inspect",
                "UNTRUSTED.bin",
                "--evidence",
                str(evidence),
                "--scratch",
                str(scratch),
            ],
        )

        assert code == 2
        assert payload["status"] == "error"
        assert payload["error"].startswith("InspectionError:")
        assert len(json.dumps(payload)) < 2_000

    def test_selected_response_budget_bounds_compact_error(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        cli_module = sys.modules[
            "skillopt.envs.skilleval.inspectors.__main__"
        ]
        monkeypatch.setattr(
            cli_module,
            "inspect_artifact",
            lambda *args, **kwargs: {"text": "界" * 10_000},
        )

        code = artifactctl_main(
            [
                "inspect",
                "report.pdf",
                "--evidence",
                str(evidence),
                "--scratch",
                str(scratch),
                "--max-response-bytes",
                str(MIN_RESPONSE_BYTES),
            ]
        )
        captured = capsys.readouterr()

        assert code == 2
        assert captured.err == ""
        assert len(captured.out.splitlines()) == 1
        assert len(captured.out.rstrip("\n").encode("utf-8")) <= (
            MIN_RESPONSE_BYTES
        )
        assert json.loads(captured.out)["status"] == "error"

    def test_selected_response_budget_bounds_success(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        cli_module = sys.modules[
            "skillopt.envs.skilleval.inspectors.__main__"
        ]
        monkeypatch.setattr(
            cli_module,
            "inspect_artifact",
            lambda *args, **kwargs: {"text": "ok"},
        )

        code = artifactctl_main(
            [
                "inspect",
                "report.pdf",
                "--evidence",
                str(evidence),
                "--scratch",
                str(scratch),
                "--max-response-bytes",
                str(MIN_RESPONSE_BYTES),
            ]
        )
        captured = capsys.readouterr()

        assert code == 0
        assert captured.err == ""
        assert len(captured.out.rstrip("\n").encode("utf-8")) <= (
            MIN_RESPONSE_BYTES
        )
        assert json.loads(captured.out)["status"] == "ok"

    def test_response_budget_below_minimum_rejects_before_dispatch(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        called = False
        cli_module = sys.modules[
            "skillopt.envs.skilleval.inspectors.__main__"
        ]

        def inspect_should_not_run(*args, **kwargs):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(
            cli_module,
            "inspect_artifact",
            inspect_should_not_run,
        )
        code, payload = self._run(
            capsys,
            [
                "inspect",
                "report.pdf",
                "--evidence",
                str(evidence),
                "--scratch",
                str(scratch),
                "--max-response-bytes",
                str(MIN_RESPONSE_BYTES - 1),
            ],
        )

        assert code == 2
        assert payload["status"] == "error"
        assert "at least" in payload["error"]
        assert called is False

    def test_path_limit_is_checked_before_root_filesystem_access(
        self, tmp_path, capsys
    ) -> None:
        code, payload = self._run(
            capsys,
            [
                "inspect",
                "a" * (MAX_LOGICAL_COMPONENT_BYTES + 1),
                "--evidence",
                str(tmp_path / "missing-evidence"),
                "--scratch",
                str(tmp_path / "missing-scratch"),
            ],
        )

        assert code == 2
        assert payload["status"] == "error"
        assert "component" in payload["error"]
        assert "does not exist" not in payload["error"]

    def test_parse_error_is_json_only(self, capsys) -> None:
        code, payload = self._run(capsys, ["inspect"])
        assert code == 2
        assert payload["status"] == "error"
        assert "required" in payload["error"]

    @pytest.mark.parametrize("argv", [["--help"], ["inspect", "--help"]])
    def test_help_is_one_success_json_object(self, capsys, argv) -> None:
        code, payload = self._run(capsys, argv)
        assert code == 0
        assert payload["status"] == "ok"
        assert "usage:" in payload["result"]["help"]

    def test_selected_response_budget_bounds_help(self, capsys) -> None:
        argv = [
            "render",
            "--max-response-bytes",
            str(MIN_RESPONSE_BYTES),
            "--help",
        ]

        code = artifactctl_main(argv)
        captured = capsys.readouterr()

        assert code == 0
        assert captured.err == ""
        assert len(captured.out.splitlines()) == 1
        assert len(captured.out.rstrip("\n").encode("utf-8")) <= (
            MIN_RESPONSE_BYTES
        )
        assert json.loads(captured.out)["status"] == "ok"

    def test_selected_response_budget_bounds_argparse_error(self, capsys) -> None:
        argv = [
            "inspect",
            "report.pdf",
            "--evidence",
            "/missing/evidence",
            "--scratch",
            "/missing/scratch",
            "--max-response-bytes",
            str(MIN_RESPONSE_BYTES),
            "--unknown=" + "x" * 2_000,
        ]

        code = artifactctl_main(argv)
        captured = capsys.readouterr()

        assert code == 2
        assert captured.err == ""
        assert len(captured.out.splitlines()) == 1
        assert len(captured.out.rstrip("\n").encode("utf-8")) <= (
            MIN_RESPONSE_BYTES
        )
        assert json.loads(captured.out)["status"] == "error"

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["unknown-command"],
            ["inventory", "--unknown-option"],
        ],
    )
    def test_all_argparse_errors_are_one_error_json_object(
        self, capsys, argv
    ) -> None:
        code, payload = self._run(capsys, argv)
        assert code == 2
        assert payload["status"] == "error"
        assert payload["error"].startswith("InspectionError:")

    @pytest.mark.parametrize(
        "argv",
        [
            ["render", "x.pdf", "--max-pixels", "0"],
            ["render", "x.pdf", "--max-pixels", str(MAX_RENDER_PIXELS + 1)],
            ["inspect", "x.pdf", "--max-response-bytes", "0"],
            [
                "inspect",
                "x.pdf",
                "--max-response-bytes",
                str(MAX_RESPONSE_BYTES + 1),
            ],
        ],
    )
    def test_cli_rejects_out_of_range_budgets(self, tmp_path, capsys, argv) -> None:
        evidence, scratch = _roots(tmp_path)
        argv.extend(
            ["--evidence", str(evidence), "--scratch", str(scratch)]
        )
        code, payload = self._run(capsys, argv)
        assert code == 2
        assert payload["status"] == "error"

    def test_cli_passes_scratch_budget(self, tmp_path, capsys, monkeypatch) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        cli_module = sys.modules[
            "skillopt.envs.skilleval.inspectors.__main__"
        ]
        observed = {}

        def fake_inspect(*args, **kwargs):
            observed.update(kwargs)
            return {"ok": True}

        monkeypatch.setattr(cli_module, "inspect_artifact", fake_inspect)
        code, payload = self._run(
            capsys,
            [
                "inspect",
                "report.pdf",
                "--evidence",
                str(evidence),
                "--scratch",
                str(scratch),
                "--max-scratch-bytes",
                "1234",
                "--max-scratch-entries",
                "12",
                "--max-scratch-depth",
                "4",
            ],
        )

        assert code == 0
        assert payload["status"] == "ok"
        assert observed["max_scratch_bytes"] == 1234
        assert observed["max_scratch_entries"] == 12
        assert observed["max_scratch_depth"] == 4


def _assert_untrusted_location(value, needles, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            _assert_untrusted_location(child, needles, (*path, key))
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_untrusted_location(child, needles, (*path, index))
        return
    if not isinstance(value, str):
        return
    for needle in needles:
        if needle in value:
            assert "untrusted_evidence" in path
            assert any(part in path for part in ("result", "error"))


def _structured_payload(result):
    assert result.structuredContent is not None
    payload = result.structuredContent
    assert set(payload) == {"untrusted_evidence"}
    return payload


def _serialized_mcp_result_bytes(result) -> int:
    return len(
        result.model_dump_json(
            by_alias=True,
            exclude_none=True,
        ).encode("utf-8")
    )


class TestArtifactMcp:
    @pytest.mark.anyio
    async def test_real_stdio_initialize_list_call_and_image(
        self, tmp_path
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        server_script = tmp_path / "artifact_stdio_server.py"
        server_script.write_text(
            """
import os
import sys
import types
from pathlib import Path

from PIL import Image as PillowImage

class FakePdfInspector:
    def inspect(self, path, scratch_dir, *, response_budget):
        return {"filename": Path(path).name, "metadata": "STDIO_META"}

    def render(self, path, scratch_dir, selectors, budget):
        output = Path(scratch_dir) / "stdio-render.png"
        PillowImage.new("RGB", (2, 3), color=(10, 20, 30)).save(output)
        return [str(output)]

    def extract(self, path, scratch_dir, selectors, *, response_budget):
        return {"text": "STDIO_TEXT"}


module = types.ModuleType("skillopt.envs.skilleval.inspectors.pdf_image")
module.PdfInspector = FakePdfInspector
module.ImageInspector = FakePdfInspector
sys.modules[module.__name__] = module

from skillopt.envs.skilleval.artifact_mcp import create_server

server = create_server(
    os.environ["TEST_ARTIFACT_EVIDENCE"],
    os.environ["TEST_ARTIFACT_SCRATCH"],
)
server.run(transport="stdio")
""".lstrip(),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        repo_root = str(Path(__file__).resolve().parents[1])
        environment.update(
            {
                "TEST_ARTIFACT_EVIDENCE": str(evidence),
                "TEST_ARTIFACT_SCRATCH": str(scratch),
                "PYTHONPATH": os.pathsep.join(
                    filter(
                        None,
                        (repo_root, environment.get("PYTHONPATH")),
                    )
                ),
            }
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(server_script)],
            env=environment,
            cwd=repo_root,
        )
        stderr_path = tmp_path / "stdio-server.stderr"
        with stderr_path.open("w+", encoding="utf-8") as stderr:
            async with stdio_client(
                parameters,
                errlog=stderr,
            ) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream,
                    write_stream,
                ) as client:
                    initialized = await client.initialize()
                    tools = await client.list_tools()
                    inventory = await client.call_tool(
                        "artifact_inventory",
                        {},
                    )
                    rendered = await client.call_tool(
                        "artifact_render",
                        {"path": "report.pdf", "max_pixels": 100},
                    )
            stderr.seek(0)
            server_stderr = stderr.read()

        assert initialized.serverInfo.name == "skillopt-artifact"
        assert sorted(tool.name for tool in tools.tools) == [
            "artifact_extract",
            "artifact_inspect",
            "artifact_inventory",
            "artifact_render",
        ]
        assert _structured_payload(inventory)["untrusted_evidence"][
            "status"
        ] == "ok"
        assert _structured_payload(rendered)["untrusted_evidence"][
            "status"
        ] == "ok"
        images = [
            content
            for content in rendered.content
            if isinstance(content, ImageContent)
        ]
        assert len(images) == 1
        assert images[0].mimeType == "image/png"
        assert "Traceback" not in server_stderr
        assert "report.pdf" not in server_stderr
        assert "STDIO_META" not in server_stderr

    @pytest.mark.anyio
    async def test_lists_exactly_four_tools_and_no_resources_or_prompts(
        self, tmp_path
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        server = artifact_mcp.create_server(str(evidence), str(scratch))

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            tools = await client.list_tools()
            resources = await client.list_resources()
            prompts = await client.list_prompts()

        assert sorted(tool.name for tool in tools.tools) == [
            "artifact_extract",
            "artifact_inspect",
            "artifact_inventory",
            "artifact_render",
        ]
        assert resources.resources == []
        assert prompts.prompts == []
        assert client.get_server_capabilities().tools is not None
        inspect_tool = next(
            tool for tool in tools.tools if tool.name == "artifact_inspect"
        )
        assert "POSIX-relative" in inspect_tool.description
        assert str(MAX_LOGICAL_PATH_BYTES) in inspect_tool.description
        assert str(MAX_LOGICAL_COMPONENTS) in inspect_tool.description
        assert str(MAX_LOGICAL_COMPONENT_BYTES) in inspect_tool.description
        path_schema = inspect_tool.inputSchema["properties"]["path"]
        assert path_schema["description"].startswith(
            "Normalized POSIX-relative"
        )
        for tool_name in (
            "artifact_inspect",
            "artifact_render",
            "artifact_extract",
        ):
            tool = next(tool for tool in tools.tools if tool.name == tool_name)
            assert tool.inputSchema["required"] == ["path"]
        for tool in tools.tools:
            assert tool.inputSchema["additionalProperties"] is False

    @pytest.mark.anyio
    async def test_calls_representative_tools_and_envelopes_artifact_strings(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        artifact_name = "UNTRUSTED_FILENAME.pdf"
        _write_pdf(evidence / artifact_name)
        server = artifact_mcp.create_server(str(evidence), str(scratch))
        needles = {
            artifact_name,
            "UNTRUSTED_META",
            "UNTRUSTED_TEXT",
        }

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            results = [
                await client.call_tool("artifact_inventory", {}),
                await client.call_tool(
                    "artifact_inspect",
                    {"path": artifact_name},
                ),
                await client.call_tool(
                    "artifact_extract",
                    {"path": artifact_name, "selectors": ["page:1"]},
                ),
            ]

        for result in results:
            payload = _structured_payload(result)
            _assert_untrusted_location(payload, needles)
            for content in result.content:
                if isinstance(content, TextContent):
                    text_payload = json.loads(content.text)
                    _assert_untrusted_location(text_payload, needles)

    @pytest.mark.anyio
    async def test_unsafe_path_and_artifact_error_are_enveloped(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        server = artifact_mcp.create_server(str(evidence), str(scratch))

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            unsafe = await client.call_tool(
                "artifact_inspect",
                {"path": "../UNTRUSTED_ESCAPE.pdf"},
            )
            fake_pdf_inspector.fail_message = "UNTRUSTED_ERROR"
            failed = await client.call_tool(
                "artifact_inspect",
                {"path": "report.pdf"},
            )

        for result, needle in (
            (unsafe, "UNTRUSTED_ESCAPE"),
            (failed, "UNTRUSTED_ERROR"),
        ):
            payload = _structured_payload(result)
            _assert_untrusted_location(payload, {needle})
            envelope = payload["untrusted_evidence"]
            assert envelope["status"] == "error"
            assert "error" in envelope
            assert result.isError is True

    @pytest.mark.anyio
    async def test_server_maxima_reject_oversized_tool_budget(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        server = artifact_mcp.create_server(
            str(evidence),
            str(scratch),
            max_render_pixels=100,
            max_response_bytes=1_000,
            max_extract_chars=500,
        )

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(
                "artifact_render",
                {"path": "report.pdf", "max_pixels": 101},
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "error"
        assert "maximum" in envelope["error"]
        assert result.isError is True

    @pytest.mark.anyio
    async def test_response_budget_uses_minimum_of_request_and_server(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        server_max = MIN_RESPONSE_BYTES + 128
        server = artifact_mcp.create_server(
            str(evidence),
            str(scratch),
            max_response_bytes=server_max,
        )

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(
                "artifact_inspect",
                {
                    "path": "report.pdf",
                    "max_response_bytes": MAX_RESPONSE_BYTES,
                },
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "ok"
        text = next(
            content.text
            for content in result.content
            if isinstance(content, TextContent)
        )
        assert len(text.encode("utf-8")) <= server_max

    @pytest.mark.anyio
    async def test_selected_response_budget_bounds_mcp_error(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        fake_pdf_inspector.inspect_value = "界" * 10_000
        server = artifact_mcp.create_server(str(evidence), str(scratch))

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(
                "artifact_inspect",
                {
                    "path": "report.pdf",
                    "max_response_bytes": MIN_RESPONSE_BYTES,
                },
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "error"
        assert result.isError is True
        text = next(
            content.text
            for content in result.content
            if isinstance(content, TextContent)
        )
        assert len(text.encode("utf-8")) <= MIN_RESPONSE_BYTES
        assert json.loads(text) == result.structuredContent

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "tool_name",
        ["artifact_render", "artifact_extract"],
    )
    async def test_argument_validation_errors_are_bounded_and_enveloped(
        self, tmp_path, fake_pdf_inspector, tool_name
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        server = artifact_mcp.create_server(str(evidence), str(scratch))
        invalid_selectors = [
            {"UNTRUSTED_SELECTOR": index}
            for index in range(256)
        ]

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            assert client.get_server_capabilities().tools is not None
            tools = await client.list_tools()
            assert tool_name in {tool.name for tool in tools.tools}
            result = await client.call_tool(
                tool_name,
                {
                    "path": "report.pdf",
                    "selectors": invalid_selectors,
                    "max_response_bytes": MIN_RESPONSE_BYTES,
                },
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "error"
        assert "selector" in envelope["error"]
        assert result.isError is True
        text = next(
            content.text
            for content in result.content
            if isinstance(content, TextContent)
        )
        assert _serialized_mcp_result_bytes(result) <= MIN_RESPONSE_BYTES
        assert json.loads(text) == result.structuredContent
        assert fake_pdf_inspector.calls == []

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "tool_name",
        ["artifact_inspect", "artifact_render", "artifact_extract"],
    )
    async def test_missing_path_errors_are_bounded_and_enveloped(
        self, tmp_path, fake_pdf_inspector, tool_name
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        server = artifact_mcp.create_server(str(evidence), str(scratch))

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            tools = await client.list_tools()
            tool = next(tool for tool in tools.tools if tool.name == tool_name)
            assert tool.inputSchema["required"] == ["path"]
            result = await client.call_tool(
                tool_name,
                {"max_response_bytes": MIN_RESPONSE_BYTES},
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "error"
        assert "path" in envelope["error"]
        assert result.isError is True
        text = next(
            content.text
            for content in result.content
            if isinstance(content, TextContent)
        )
        assert _serialized_mcp_result_bytes(result) <= MIN_RESPONSE_BYTES
        assert json.loads(text) == result.structuredContent
        assert fake_pdf_inspector.calls == []

    @pytest.mark.anyio
    async def test_unknown_argument_is_bounded_enveloped_and_not_dispatched(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        server = artifact_mcp.create_server(str(evidence), str(scratch))
        unknown_name = "UNTRUSTED_UNKNOWN_" + "x" * 1_000

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(
                "artifact_inspect",
                {
                    "path": "report.pdf",
                    "max_response_bytes": MIN_RESPONSE_BYTES,
                    unknown_name: "UNTRUSTED_VALUE" * 1_000,
                },
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "error"
        assert "unknown" in envelope["error"].lower()
        assert result.isError is True
        assert _serialized_mcp_result_bytes(result) <= MIN_RESPONSE_BYTES
        assert fake_pdf_inspector.calls == []

    @pytest.mark.anyio
    async def test_scratch_budget_is_exposed_and_enforced(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        fake_pdf_inspector.unreturned_scratch_bytes = 128
        server = artifact_mcp.create_server(
            str(evidence),
            str(scratch),
            max_scratch_bytes=100,
            max_scratch_entries=10,
            max_scratch_depth=4,
        )

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            tools = await client.list_tools()
            inspect_tool = next(
                tool for tool in tools.tools
                if tool.name == "artifact_inspect"
            )
            assert "max_scratch_bytes" in inspect_tool.inputSchema["properties"]
            assert "max_scratch_entries" in inspect_tool.inputSchema["properties"]
            assert "max_scratch_depth" in inspect_tool.inputSchema["properties"]
            result = await client.call_tool(
                "artifact_inspect",
                {
                    "path": "report.pdf",
                    "max_response_bytes": 4_096,
                    "max_scratch_bytes": 100,
                    "max_scratch_entries": 10,
                    "max_scratch_depth": 4,
                },
            )
            inventory_result = await client.call_tool(
                "artifact_inventory",
                {
                    "max_response_bytes": 4_096,
                    "max_scratch_bytes": 100,
                    "max_scratch_entries": 10,
                    "max_scratch_depth": 4,
                },
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "error"
        assert "scratch" in envelope["error"]
        assert result.isError is True
        assert _serialized_mcp_result_bytes(result) <= 4_096
        assert list(scratch.iterdir()) == []
        inventory_envelope = _structured_payload(inventory_result)[
            "untrusted_evidence"
        ]
        assert inventory_envelope["status"] == "ok"
        assert inventory_result.isError is not True
        assert (
            _serialized_mcp_result_bytes(inventory_result)
            <= 4_096
        )

    @pytest.mark.anyio
    async def test_mcp_response_budget_below_minimum_skips_inspector(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        server = artifact_mcp.create_server(str(evidence), str(scratch))

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(
                "artifact_inspect",
                {
                    "path": "report.pdf",
                    "max_response_bytes": MIN_RESPONSE_BYTES - 1,
                },
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "error"
        assert "at least" in envelope["error"]
        assert result.isError is True
        assert fake_pdf_inspector.calls == []

    @pytest.mark.anyio
    async def test_mcp_path_limit_rejects_before_inspector(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        server = artifact_mcp.create_server(str(evidence), str(scratch))

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(
                "artifact_inspect",
                {"path": "界" * 86},
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "error"
        assert "component" in envelope["error"]
        assert result.isError is True
        assert fake_pdf_inspector.calls == []

    @pytest.mark.anyio
    async def test_render_returns_png_image_content_without_host_path(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "UNTRUSTED_FILENAME.pdf")
        server = artifact_mcp.create_server(str(evidence), str(scratch))

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(
                "artifact_render",
                {
                    "path": "UNTRUSTED_FILENAME.pdf",
                    "selectors": ["page:1"],
                    "max_pixels": 100,
                },
            )

        payload = _structured_payload(result)
        envelope = payload["untrusted_evidence"]
        assert envelope["status"] == "ok"
        serialized = json.dumps(payload)
        assert str(scratch) not in serialized
        images = [
            content for content in result.content if isinstance(content, ImageContent)
        ]
        assert len(images) == 1
        assert images[0].mimeType == "image/png"
        image_metadata = envelope["result"]["images"][0]
        assert image_metadata == {
            "index": 0,
            "mime": "image/png",
            "width": 2,
            "height": 3,
            "bytes": len(base64.b64decode(images[0].data)),
        }
        assert list(scratch.iterdir()) == []

    @pytest.mark.anyio
    async def test_render_does_not_reopen_replaced_scratch_root(
        self, tmp_path, fake_pdf_inspector, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        held = tmp_path / "held-scratch"
        real_read_png = artifact_mcp._read_png
        replacement_performed = False

        def replace_root_then_read(source, scratch_dir, **kwargs):
            nonlocal replacement_performed
            scratch.rename(held)
            scratch.mkdir()
            PillowImage.new("RGB", (9, 9), color=(200, 1, 1)).save(
                scratch / "render-UNTRUSTED_META.png"
            )
            replacement_performed = True
            return real_read_png(source, scratch_dir, **kwargs)

        monkeypatch.setattr(
            artifact_mcp,
            "_read_png",
            replace_root_then_read,
        )
        server = artifact_mcp.create_server(str(evidence), str(scratch))

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(
                "artifact_render",
                {"path": "report.pdf", "max_pixels": 100},
            )

        assert replacement_performed is True
        envelope = _structured_payload(result)["untrusted_evidence"]
        if envelope["status"] == "ok":
            assert envelope["result"]["images"][0]["width"] == 2
            assert envelope["result"]["images"][0]["height"] == 3
        else:
            assert result.isError is True
            assert not any(
                isinstance(content, ImageContent) for content in result.content
            )

    @pytest.mark.anyio
    async def test_render_full_result_budget_rejects_before_reading_media(
        self, tmp_path, fake_pdf_inspector, monkeypatch
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        large = scratch / "large.png"
        pixels = os.urandom(128 * 128 * 3)
        PillowImage.frombytes("RGB", (128, 128), pixels).save(
            large,
            compress_level=0,
        )
        fake_pdf_inspector.render_bytes = large.read_bytes()
        read_called = False
        image_called = False

        def unexpected_read(*args, **kwargs):
            nonlocal read_called
            read_called = True
            raise AssertionError("oversized media must not be read")

        def unexpected_image(*args, **kwargs):
            nonlocal image_called
            image_called = True
            raise AssertionError("oversized media must not be encoded")

        monkeypatch.setattr(artifact_mcp, "_read_png", unexpected_read)
        monkeypatch.setattr(artifact_mcp, "Image", unexpected_image)
        server = artifact_mcp.create_server(str(evidence), str(scratch))

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(
                "artifact_render",
                {
                    "path": "report.pdf",
                    "max_pixels": 128 * 128,
                    "max_response_bytes": MIN_RESPONSE_BYTES,
                },
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "error"
        assert "response" in envelope["error"].lower()
        assert result.isError is True
        assert _serialized_mcp_result_bytes(result) <= MIN_RESPONSE_BYTES
        assert read_called is False
        assert image_called is False
        assert not any(
            isinstance(content, ImageContent) for content in result.content
        )

    @pytest.mark.anyio
    async def test_render_rejects_non_png_and_oversized_media(
        self, tmp_path, fake_pdf_inspector
    ) -> None:
        evidence, scratch = _roots(tmp_path)
        _write_pdf(evidence / "report.pdf")
        bad = scratch / "bad.png"
        bad.write_text("not an image", encoding="utf-8")
        fake_pdf_inspector.render_bytes = bad.read_bytes()
        server = artifact_mcp.create_server(
            str(evidence),
            str(scratch),
            max_media_bytes=10,
        )

        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as client:
            result = await client.call_tool(
                "artifact_render",
                {"path": "report.pdf"},
            )

        envelope = _structured_payload(result)["untrusted_evidence"]
        assert envelope["status"] == "error"
        assert result.isError is True
        assert not any(
            isinstance(content, ImageContent) for content in result.content
        )
