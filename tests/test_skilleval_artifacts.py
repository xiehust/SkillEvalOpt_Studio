"""Artifact manifest and immutable evidence tests for SkillEval."""
from __future__ import annotations

import json
import os
import stat
import zipfile
from dataclasses import FrozenInstanceError

import pytest

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
