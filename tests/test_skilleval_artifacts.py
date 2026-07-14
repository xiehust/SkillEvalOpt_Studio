"""Artifact manifest and immutable evidence tests for SkillEval."""
from __future__ import annotations

import json
import os
import stat
import zipfile
from dataclasses import FrozenInstanceError

import pytest

from skillopt.envs.skilleval import artifacts as artifacts_mod
from skillopt.envs.skilleval.artifacts import (
    EvidenceLimitError,
    ManifestEntry,
    build_manifest,
    create_evidence_snapshot,
    detect_artifact_kind,
    diff_manifests,
    is_binary_output,
    verify_evidence_snapshot,
)


def _write_ooxml(path, kind: str, *, unsafe_name: str | None = None) -> None:
    details = {
        "xlsx": (
            "xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        ),
        "docx": (
            "word/document.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        ),
        "pptx": (
            "ppt/presentation.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        ),
    }
    member, content_type = details[kind]
    content_types = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/{member}" ContentType="{content_type}"/>'
        "</Types>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr(member, "<root/>")
        if unsafe_name is not None:
            archive.writestr(unsafe_name, "unsafe")


def _outputs_after(work, mutate) -> list[dict]:
    before = build_manifest(str(work))
    mutate()
    return diff_manifests(before, build_manifest(str(work)))


class TestManifest:
    def test_diff_classifies_outputs_and_skips_runtime_entries(self, tmp_path) -> None:
        root = tmp_path / "work"
        (root / "nested").mkdir(parents=True)
        (root / "input.txt").write_text("seed", encoding="utf-8")
        (root / "unchanged.txt").write_text("same", encoding="utf-8")
        (root / "deleted.txt").write_text("gone", encoding="utf-8")
        (root / "task.md").write_text("prompt", encoding="utf-8")
        for internal in (".agents", ".claude", ".codex", ".git"):
            (root / internal).mkdir()
            (root / internal / "runtime.txt").write_text("runtime", encoding="utf-8")
        before = build_manifest(str(root))

        (root / "input.txt").write_text("changed", encoding="utf-8")
        (root / "deleted.txt").unlink()
        (root / "nested" / "report.pdf").write_bytes(b"%PDF-1.4\n")
        (root / ".agents" / "new-runtime.txt").write_text("runtime", encoding="utf-8")
        after = build_manifest(str(root))
        diff = diff_manifests(before, after)

        assert list(after) == ["input.txt", "nested/report.pdf", "unchanged.txt"]
        assert [(row["path"], row["change"]) for row in diff] == [
            ("input.txt", "modified"),
            ("nested/report.pdf", "created"),
        ]
        assert after["input.txt"].size == len(b"changed")
        assert len(after["input.txt"].sha256) == 64

    def test_manifest_entry_is_frozen(self) -> None:
        entry = ManifestEntry("a", 1, "0" * 64, "text/plain", None)
        with pytest.raises(FrozenInstanceError):
            entry.size = 2  # type: ignore[misc]

    def test_rejects_symlink_file(self, tmp_path) -> None:
        root = tmp_path / "work"
        root.mkdir()
        target = tmp_path / "outside"
        target.write_text("secret", encoding="utf-8")
        (root / "link").symlink_to(target)

        with pytest.raises(ValueError, match="symlink"):
            build_manifest(str(root))

    def test_rejects_symlinked_root_task_file(self, tmp_path) -> None:
        root = tmp_path / "work"
        root.mkdir()
        target = tmp_path / "outside"
        target.write_text("secret", encoding="utf-8")
        (root / "task.md").symlink_to(target)

        with pytest.raises(ValueError, match="symlink"):
            build_manifest(str(root))

    def test_rejects_symlink_directory(self, tmp_path) -> None:
        root = tmp_path / "work"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "linked-dir").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="symlink directory"):
            build_manifest(str(root))

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
    def test_rejects_nonregular_entry(self, tmp_path) -> None:
        root = tmp_path / "work"
        root.mkdir()
        os.mkfifo(root / "pipe")

        with pytest.raises(ValueError, match="non-regular"):
            build_manifest(str(root))

    def test_rejects_hard_linked_file(self, tmp_path) -> None:
        root = tmp_path / "work"
        root.mkdir()
        output = root / "report.pdf"
        output.write_bytes(b"%PDF-1.4\n")
        os.link(output, root / "report-copy.pdf")

        with pytest.raises(ValueError, match="single-link"):
            build_manifest(str(root))

    @pytest.mark.parametrize("name", [".agents", ".claude", ".codex", ".git"])
    def test_reserved_runtime_name_as_regular_file_is_excluded(
        self, tmp_path, name
    ) -> None:
        root = tmp_path / "work"
        root.mkdir()
        (root / name).write_text("runtime", encoding="utf-8")

        assert build_manifest(str(root)) == {}

    def test_parent_symlink_swap_is_rejected(
        self, tmp_path, monkeypatch
    ) -> None:
        root = tmp_path / "work"
        nested = root / "nested"
        nested.mkdir(parents=True)
        original = b"%PDF-1.4\noriginal"
        (nested / "report.pdf").write_bytes(original)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "report.pdf").write_bytes(b"\x89PNG\r\n\x1a\nreplacement")
        moved = tmp_path / "original-nested"
        real_open = os.open
        swapped = False

        def racing_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if os.fspath(path).endswith("report.pdf") and not swapped:
                swapped = True
                os.rename(nested, moved)
                os.symlink(outside, nested, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(artifacts_mod.os, "open", racing_open)

        with pytest.raises(ValueError, match="changed|symlink"):
            build_manifest(str(root))
        assert swapped is True

    def test_directory_symlink_inserted_after_traversal_is_rejected(
        self, tmp_path, monkeypatch
    ) -> None:
        root = tmp_path / "work"
        nested = root / "nested"
        nested.mkdir(parents=True)
        (nested / "report.pdf").write_bytes(b"%PDF-1.4\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        moved = tmp_path / "original-nested"
        real_manifest_entry = artifacts_mod._manifest_entry_from_descriptor
        swapped = False

        def racing_manifest_entry(descriptor, rel, *, require_single_link):
            nonlocal swapped
            entry = real_manifest_entry(
                descriptor,
                rel,
                require_single_link=require_single_link,
            )
            if rel == "nested/report.pdf" and not swapped:
                os.rename(nested, moved)
                os.symlink(outside, nested, target_is_directory=True)
                swapped = True
            return entry

        monkeypatch.setattr(
            artifacts_mod,
            "_manifest_entry_from_descriptor",
            racing_manifest_entry,
        )

        with pytest.raises(ValueError, match="changed|symlink"):
            build_manifest(str(root))
        assert swapped is True


class TestArtifactKind:
    @pytest.mark.parametrize(
        ("mime", "expected"),
        [
            (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "xlsx",
            ),
            ("application/vnd.ms-excel", "xls"),
            (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "docx",
            ),
            ("application/msword", "doc"),
            ("application/pdf", "pdf"),
            ("image/png", "image"),
            (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "pptx",
            ),
            ("application/vnd.ms-powerpoint", "ppt"),
        ],
    )
    def test_specific_supported_mime_wins(self, tmp_path, mime, expected) -> None:
        path = tmp_path / "conflicting.bin"
        path.write_bytes(b"not the expected signature")
        assert detect_artifact_kind(str(path), mime) == expected

    @pytest.mark.parametrize(
        ("name", "payload", "expected"),
        [
            ("unknown.bin", b"%PDF-1.7\n", "pdf"),
            ("unknown.bin", b"\x89PNG\r\n\x1a\npayload", "image"),
            ("unknown.bin", b"\xff\xd8\xff\xe0payload", "image"),
            ("unknown.bin", b"RIFF\x04\x00\x00\x00WEBP", "image"),
            ("unknown.bin", b"II*\x00payload", "image"),
        ],
    )
    def test_generic_mime_uses_signature(self, tmp_path, name, payload, expected) -> None:
        path = tmp_path / name
        path.write_bytes(payload)
        assert detect_artifact_kind(str(path), "application/octet-stream") == expected

    @pytest.mark.parametrize("kind", ["xlsx", "docx", "pptx"])
    def test_generic_zip_inspects_ooxml_container(self, tmp_path, kind) -> None:
        path = tmp_path / "artifact.bin"
        _write_ooxml(path, kind)
        assert detect_artifact_kind(str(path), "application/zip") == kind

    def test_generic_ole_and_unavailable_mime_fall_back_to_supported_suffix(
        self, tmp_path
    ) -> None:
        ole = tmp_path / "legacy.xls"
        ole.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1payload")
        assert detect_artifact_kind(str(ole), "application/x-ole-storage") == "xls"

        unknown = tmp_path / "legacy.doc"
        unknown.write_bytes(b"payload")
        assert detect_artifact_kind(str(unknown), "application/octet-stream") == "doc"

    def test_specific_conflicting_mime_is_not_overridden_by_suffix(self, tmp_path) -> None:
        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF-1.7\n")
        assert detect_artifact_kind(str(path), "text/plain") is None

    def test_unavailable_file_command_uses_signature_before_suffix(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "report.xlsx"
        path.write_bytes(b"%PDF-1.7\n")

        def unavailable(*args, **kwargs):
            raise FileNotFoundError("file utility unavailable")

        monkeypatch.setattr(artifacts_mod.subprocess, "run", unavailable)

        assert detect_artifact_kind(str(path)) == "pdf"

    def test_unsafe_or_corrupt_ooxml_is_unsupported(self, tmp_path) -> None:
        unsafe = tmp_path / "unsafe.xlsx"
        _write_ooxml(unsafe, "xlsx", unsafe_name="../escape")
        corrupt = tmp_path / "corrupt.xlsx"
        corrupt.write_bytes(b"PK\x03\x04not-a-zip")

        assert detect_artifact_kind(str(unsafe), "application/zip") is None
        assert detect_artifact_kind(str(corrupt), "application/zip") is None

    def test_unknown_octet_stream_is_not_binary_output(self, tmp_path) -> None:
        path = tmp_path / "unknown.bin"
        path.write_bytes(b"\x00\x01\x02")
        assert detect_artifact_kind(str(path), "application/octet-stream") is None
        assert is_binary_output({"mime": "application/octet-stream", "kind": None}) is False
        assert is_binary_output({"mime": "text/plain", "kind": "pdf"}) is True


class TestEvidenceSnapshot:
    def test_copies_only_declared_outputs_and_writes_manifest_to_scratch(
        self, tmp_path
    ) -> None:
        work = tmp_path / "work"
        (work / "nested").mkdir(parents=True)
        (work / "input.txt").write_text("seed", encoding="utf-8")
        outputs = _outputs_after(
            work,
            lambda: (work / "nested" / "report.pdf").write_bytes(b"%PDF-1.4\n"),
        )
        judge_root = tmp_path / "judge"

        snapshot = create_evidence_snapshot(
            str(work), outputs, str(judge_root), max_bytes=1024
        )

        evidence_file = judge_root / "evidence" / "nested" / "report.pdf"
        assert evidence_file.read_bytes() == b"%PDF-1.4\n"
        assert not (judge_root / "evidence" / "input.txt").exists()
        assert not (work / "artifact-manifest.json").exists()
        manifest_path = judge_root / "scratch" / "artifact-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert [row["path"] for row in manifest] == ["nested/report.pdf"]
        assert stat.S_IMODE(evidence_file.stat().st_mode) == 0o444
        assert stat.S_IMODE((judge_root / "evidence").stat().st_mode) == 0o555
        assert stat.S_IMODE((judge_root / "evidence" / "nested").stat().st_mode) == 0o555
        (judge_root / "scratch" / "writable.txt").write_text("ok", encoding="utf-8")
        verify_evidence_snapshot(snapshot)

    def test_ignores_rows_not_marked_created_or_modified(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        (work / "input.txt").write_text("seed", encoding="utf-8")
        entry = build_manifest(str(work))["input.txt"]
        row = {
            "path": entry.path,
            "size": entry.size,
            "sha256": entry.sha256,
            "mime": entry.mime,
            "kind": entry.kind,
            "change": "unchanged",
        }

        snapshot = create_evidence_snapshot(
            str(work), [row], str(tmp_path / "judge"), max_bytes=1024
        )

        assert snapshot.files == ()
        assert not (tmp_path / "judge" / "evidence" / "input.txt").exists()

    def test_modified_seed_file_is_an_output(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        (work / "input.txt").write_text("seed", encoding="utf-8")
        outputs = _outputs_after(
            work, lambda: (work / "input.txt").write_text("changed", encoding="utf-8")
        )

        create_evidence_snapshot(
            str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
        )

        assert (tmp_path / "judge" / "evidence" / "input.txt").read_text() == "changed"

    def test_enforces_actual_aggregate_byte_limit(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work,
            lambda: [
                (work / name).write_bytes(b"1234")
                for name in ("first.pdf", "second.pdf")
            ],
        )
        for row in outputs:
            row["size"] = 0

        with pytest.raises(EvidenceLimitError, match="configured limit 7"):
            create_evidence_snapshot(
                str(work), outputs, str(tmp_path / "judge"), max_bytes=7
            )

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("size", 999, "size mismatch"),
            ("sha256", "0" * 64, "hash mismatch"),
        ],
    )
    def test_rejects_source_metadata_mismatch(
        self, tmp_path, field, value, message
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        outputs[0][field] = value

        with pytest.raises(ValueError, match=message):
            create_evidence_snapshot(
                str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
            )

    def test_rejects_source_changed_after_manifest(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        (work / "report.pdf").write_bytes(b"%PDF-2.0\nchanged")

        with pytest.raises(ValueError, match="mismatch"):
            create_evidence_snapshot(
                str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
            )

    def test_workspace_ancestor_swap_cannot_copy_outside_file(
        self, tmp_path, monkeypatch
    ) -> None:
        workspace_parent = tmp_path / "workspace-parent"
        work = workspace_parent / "work"
        work.mkdir(parents=True)
        (work / "report.pdf").write_bytes(b"%PDF-1.4\noriginal")
        outside_parent = tmp_path / "outside-parent"
        outside_work = outside_parent / "work"
        outside_work.mkdir(parents=True)
        (outside_work / "report.pdf").write_bytes(b"%PDF-1.4\noutside")
        outside_entry = build_manifest(str(outside_work))["report.pdf"]
        outputs = [
            {
                "path": outside_entry.path,
                "size": outside_entry.size,
                "sha256": outside_entry.sha256,
                "mime": outside_entry.mime,
                "kind": outside_entry.kind,
                "change": "created",
            }
        ]
        moved_parent = tmp_path / "original-workspace-parent"
        real_prepare = artifacts_mod._prepare_judge_dirs
        swapped = False

        def racing_prepare(judge_root):
            nonlocal swapped
            os.rename(workspace_parent, moved_parent)
            os.symlink(outside_parent, workspace_parent, target_is_directory=True)
            swapped = True
            return real_prepare(judge_root)

        monkeypatch.setattr(
            artifacts_mod,
            "_prepare_judge_dirs",
            racing_prepare,
        )

        with pytest.raises(ValueError, match="mismatch"):
            create_evidence_snapshot(
                str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
            )
        assert swapped is True

    def test_rejects_path_escape(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"%PDF-1.4\n")

        with pytest.raises(ValueError, match="relative artifact path"):
            create_evidence_snapshot(
                str(work),
                [
                    {
                        "path": "../outside.pdf",
                        "change": "created",
                        "size": outside.stat().st_size,
                        "sha256": "0" * 64,
                    }
                ],
                str(tmp_path / "judge"),
                max_bytes=1024,
            )

    def test_rejects_nested_runtime_path(self, tmp_path) -> None:
        work = tmp_path / "work"
        runtime_dir = work / "nested" / ".agents"
        runtime_dir.mkdir(parents=True)
        output = runtime_dir / "hidden.pdf"
        output.write_bytes(b"%PDF-1.4\n")

        with pytest.raises(ValueError, match="runtime path"):
            create_evidence_snapshot(
                str(work),
                [
                    {
                        "path": "nested/.agents/hidden.pdf",
                        "change": "created",
                        "size": output.stat().st_size,
                        "sha256": "0" * 64,
                    }
                ],
                str(tmp_path / "judge"),
                max_bytes=1024,
            )

    def test_rejects_symlink_swap(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        output = work / "report.pdf"
        outputs = _outputs_after(work, lambda: output.write_bytes(b"%PDF-1.4\n"))
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"%PDF-1.4\n")
        output.unlink()
        output.symlink_to(outside)

        with pytest.raises(ValueError, match="symlink|regular file"):
            create_evidence_snapshot(
                str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
            )

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
    def test_rejects_fifo_swap_without_blocking(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        output = work / "report.pdf"
        outputs = _outputs_after(work, lambda: output.write_bytes(b"%PDF-1.4\n"))
        output.unlink()
        os.mkfifo(output)

        with pytest.raises(ValueError, match="single-link regular file"):
            create_evidence_snapshot(
                str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
            )

    def test_rejects_hard_link(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        output = work / "report.pdf"
        outputs = _outputs_after(work, lambda: output.write_bytes(b"%PDF-1.4\n"))
        os.link(output, tmp_path / "second-link.pdf")

        with pytest.raises(ValueError, match="single-link regular file"):
            create_evidence_snapshot(
                str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
            )

    def test_rejects_hard_link_added_after_initial_source_validation(
        self, tmp_path, monkeypatch
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        output = work / "report.pdf"
        outputs = _outputs_after(work, lambda: output.write_bytes(b"%PDF-1.4\n"))
        late_link = tmp_path / "late-link.pdf"
        real_open_destination = artifacts_mod._open_destination_file
        linked = False

        def racing_open_destination(root_descriptor, parts):
            nonlocal linked
            if not linked:
                linked = True
                os.link(output, late_link)
            return real_open_destination(root_descriptor, parts)

        monkeypatch.setattr(
            artifacts_mod,
            "_open_destination_file",
            racing_open_destination,
        )

        with pytest.raises(ValueError, match="changed|single-link"):
            create_evidence_snapshot(
                str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
            )
        assert linked is True

    def test_rejects_source_name_replaced_after_copy(
        self, tmp_path, monkeypatch
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        output = work / "report.pdf"
        payload = b"%PDF-1.4\noriginal"
        outputs = _outputs_after(work, lambda: output.write_bytes(payload))
        replaced = tmp_path / "original-report.pdf"
        real_lock = artifacts_mod._lock_evidence_directories
        swapped = False

        def racing_lock(evidence_descriptor):
            nonlocal swapped
            real_lock(evidence_descriptor)
            output.rename(replaced)
            output.write_bytes(payload)
            swapped = True

        monkeypatch.setattr(
            artifacts_mod,
            "_lock_evidence_directories",
            racing_lock,
        )

        with pytest.raises(ValueError, match="source|changed"):
            create_evidence_snapshot(
                str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
            )
        assert swapped is True

    def test_rejects_source_growth_without_writing_past_limit(
        self, tmp_path, monkeypatch
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        output = work / "report.pdf"
        payload = b"%PDF"
        outputs = _outputs_after(work, lambda: output.write_bytes(payload))
        judge_root = tmp_path / "judge"
        real_write_all = artifacts_mod._write_all
        largest_destination = 0
        grown = False

        def racing_write_all(descriptor, data):
            nonlocal largest_destination, grown
            real_write_all(descriptor, data)
            largest_destination = max(
                largest_destination,
                os.fstat(descriptor).st_size,
            )
            if not grown:
                with output.open("ab") as handle:
                    handle.write(b"-oversized")
                grown = True

        monkeypatch.setattr(artifacts_mod, "_write_all", racing_write_all)

        with pytest.raises(ValueError, match="grew|changed"):
            create_evidence_snapshot(
                str(work), outputs, str(judge_root), max_bytes=len(payload)
            )
        assert grown is True
        assert largest_destination <= len(payload)
        assert not judge_root.exists()

    def test_rejects_symlinked_judge_root_without_touching_target(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("keep", encoding="utf-8")
        judge_root = tmp_path / "judge"
        judge_root.symlink_to(victim, target_is_directory=True)

        with pytest.raises(ValueError, match="symlink"):
            create_evidence_snapshot(
                str(work), outputs, str(judge_root), max_bytes=1024
            )
        assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"

    def test_rejects_symlinked_judge_parent_without_creating_through_it(
        self, tmp_path
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        victim = tmp_path / "victim"
        victim.mkdir()
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(victim, target_is_directory=True)
        judge_root = linked_parent / "created-through-link" / "judge"

        with pytest.raises(ValueError, match="symlink|real directory"):
            create_evidence_snapshot(
                str(work), outputs, str(judge_root), max_bytes=1024
            )
        assert not (victim / "created-through-link").exists()

    def test_judge_parent_swap_cannot_delete_outside_victim(
        self, tmp_path, monkeypatch
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        anchor = tmp_path / "anchor"
        parent = anchor / "parent"
        stale_judge = parent / "judge"
        stale_judge.mkdir(parents=True)
        (stale_judge / "stale.txt").write_text("stale", encoding="utf-8")
        moved_parent = anchor / "original-parent"
        victim_parent = tmp_path / "victim-parent"
        victim_judge = victim_parent / "judge"
        victim_judge.mkdir(parents=True)
        victim_file = victim_judge / "keep.txt"
        victim_file.write_text("keep", encoding="utf-8")
        real_ensure = artifacts_mod._ensure_real_directory
        swapped = False

        def racing_ensure(path):
            nonlocal swapped
            descriptor = real_ensure(path)
            os.rename(parent, moved_parent)
            os.symlink(victim_parent, parent, target_is_directory=True)
            swapped = True
            return descriptor

        monkeypatch.setattr(
            artifacts_mod,
            "_ensure_real_directory",
            racing_ensure,
        )

        with pytest.raises(ValueError, match="changed|symlink|directory"):
            create_evidence_snapshot(
                str(work), outputs, str(parent / "judge"), max_bytes=1024
            )
        assert swapped is True
        assert victim_file.read_text(encoding="utf-8") == "keep"

    def test_rejects_returned_judge_root_replacement_without_deleting_it(
        self, tmp_path, monkeypatch
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        judge_root = tmp_path / "judge"
        moved_root = tmp_path / "original-judge"
        replacement_marker = judge_root / "keep.txt"
        real_lock = artifacts_mod._lock_evidence_directories
        swapped = False

        def racing_lock(evidence_descriptor):
            nonlocal swapped
            real_lock(evidence_descriptor)
            judge_root.rename(moved_root)
            (judge_root / "evidence").mkdir(parents=True)
            (judge_root / "scratch").mkdir()
            replacement_marker.write_text("keep", encoding="utf-8")
            swapped = True

        monkeypatch.setattr(
            artifacts_mod,
            "_lock_evidence_directories",
            racing_lock,
        )

        with pytest.raises(ValueError, match="directory path changed"):
            create_evidence_snapshot(
                str(work), outputs, str(judge_root), max_bytes=1024
            )
        assert swapped is True
        assert replacement_marker.read_text(encoding="utf-8") == "keep"

    @pytest.mark.parametrize("component", ["evidence", "scratch"])
    def test_rejects_returned_child_directory_replacement(
        self, tmp_path, monkeypatch, component
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        judge_root = tmp_path / "judge"
        real_lock = artifacts_mod._lock_evidence_directories
        swapped = False

        def racing_lock(evidence_descriptor):
            nonlocal swapped
            real_lock(evidence_descriptor)
            target = judge_root / component
            target.rename(judge_root / f"original-{component}")
            target.mkdir()
            swapped = True

        monkeypatch.setattr(
            artifacts_mod,
            "_lock_evidence_directories",
            racing_lock,
        )

        with pytest.raises(ValueError, match="directory path changed"):
            create_evidence_snapshot(
                str(work), outputs, str(judge_root), max_bytes=1024
            )
        assert swapped is True

    def test_detects_evidence_content_and_path_mutation(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        snapshot = create_evidence_snapshot(
            str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
        )
        evidence = tmp_path / "judge" / "evidence" / "report.pdf"
        os.chmod(evidence, 0o644)
        evidence.write_bytes(b"changed")
        with pytest.raises(RuntimeError, match="evidence changed"):
            verify_evidence_snapshot(snapshot)

        evidence.write_bytes(b"%PDF-1.4\n")
        os.chmod(tmp_path / "judge" / "evidence", 0o755)
        (tmp_path / "judge" / "evidence" / "extra.txt").write_text(
            "extra", encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="evidence changed"):
            verify_evidence_snapshot(snapshot)

    def test_detects_added_task_file_in_evidence(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        snapshot = create_evidence_snapshot(
            str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
        )
        evidence = tmp_path / "judge" / "evidence"
        os.chmod(evidence, 0o755)
        (evidence / "task.md").write_text("injected", encoding="utf-8")

        with pytest.raises(RuntimeError, match="evidence changed"):
            verify_evidence_snapshot(snapshot)

    def test_detects_added_runtime_tree_in_evidence(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        snapshot = create_evidence_snapshot(
            str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
        )
        evidence = tmp_path / "judge" / "evidence"
        os.chmod(evidence, 0o755)
        runtime = evidence / ".agents"
        runtime.mkdir()
        (runtime / "runtime.json").write_text("{}", encoding="utf-8")

        with pytest.raises(RuntimeError, match="evidence changed"):
            verify_evidence_snapshot(snapshot)

    def test_detects_evidence_file_hard_link_mutation(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        snapshot = create_evidence_snapshot(
            str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
        )
        os.link(
            tmp_path / "judge" / "evidence" / "report.pdf",
            tmp_path / "evidence-hard-link.pdf",
        )

        with pytest.raises(RuntimeError, match="evidence changed"):
            verify_evidence_snapshot(snapshot)

    def test_detects_evidence_file_mode_mutation(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        snapshot = create_evidence_snapshot(
            str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
        )
        os.chmod(tmp_path / "judge" / "evidence" / "report.pdf", 0o644)

        with pytest.raises(RuntimeError, match="evidence changed"):
            verify_evidence_snapshot(snapshot)

    def test_detects_evidence_directory_mode_mutation(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        snapshot = create_evidence_snapshot(
            str(work), outputs, str(tmp_path / "judge"), max_bytes=1024
        )
        os.chmod(tmp_path / "judge" / "evidence", 0o755)

        with pytest.raises(RuntimeError, match="evidence changed"):
            verify_evidence_snapshot(snapshot)

    def test_rejects_destination_hard_link_added_during_directory_lock(
        self, tmp_path, monkeypatch
    ) -> None:
        work = tmp_path / "work"
        work.mkdir()
        outputs = _outputs_after(
            work, lambda: (work / "report.pdf").write_bytes(b"%PDF-1.4\n")
        )
        judge_root = tmp_path / "judge"
        late_link = tmp_path / "late-evidence-link.pdf"
        real_fchmod = artifacts_mod.os.fchmod
        linked = False

        def racing_fchmod(descriptor, mode):
            nonlocal linked
            if mode == 0o555 and stat.S_ISDIR(os.fstat(descriptor).st_mode) and not linked:
                os.link(judge_root / "evidence" / "report.pdf", late_link)
                linked = True
            return real_fchmod(descriptor, mode)

        monkeypatch.setattr(
            artifacts_mod.os,
            "fchmod",
            racing_fchmod,
        )

        with pytest.raises((ValueError, RuntimeError), match="single-link|changed"):
            create_evidence_snapshot(
                str(work), outputs, str(judge_root), max_bytes=1024
            )
        assert linked is True
