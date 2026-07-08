"""Built-in sample skills/tasksets: materialization, fidelity, read-only guards."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillopt_studio import samples, skill_sources, tasksets
from skillopt_studio.app import create_app
from skillopt_studio.config import StudioConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_SKILL_SLUGS = {
    "searchqa-gpt5.5", "alfworld-gpt5.5", "docvqa-gpt5.5", "livemath-gpt5.5",
    "officeqa-gpt5.5", "spreadsheetbench-gpt5.5", "logtriage", "logtriage-v2", "report",
}

EXPECTED_TASKSET_IDS = {
    "sample-logtriage", "sample-report", "sample-xlsx",
    "sample-searchqa", "sample-livemath", "sample-officeqa",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def config(tmp_path) -> StudioConfig:
    cfg = StudioConfig(studio_root=tmp_path / "studio", skill_sources={}, samples_enabled=True)
    samples.materialize_samples(cfg)
    return cfg


@pytest.fixture()
def client(config) -> TestClient:
    return TestClient(create_app(config))


class TestSampleSkills:
    def test_nine_sample_skills_scanned(self, config):
        found = {s.id: s for s in skill_sources.scan_skills(config) if s.source == "sample"}
        assert set(found) == {f"sample--{slug}" for slug in EXPECTED_SKILL_SLUGS}

    def test_ckpt_skill_md_byte_identical(self, config):
        pairs = {
            "searchqa-gpt5.5": "ckpt/searchqa/gpt5.5_skill.md",
            "alfworld-gpt5.5": "ckpt/alfworld/gpt5.5_skill.md",
            "docvqa-gpt5.5": "ckpt/docvqa/gpt5.5_skill.md",
            "livemath-gpt5.5": "ckpt/livemath/gpt5.5_skill.md",
            "officeqa-gpt5.5": "ckpt/officeqa/gpt5.5_skill.md",
            "spreadsheetbench-gpt5.5": "ckpt/spreadsheetbench/gpt5.5_skill.md",
            "report": "data/skilleval_demo/report_skill/initial.md",
        }
        for slug, source in pairs.items():
            materialized = config.samples_skills_dir / slug / "SKILL.md"
            assert _sha256(materialized) == _sha256(PROJECT_ROOT / source), slug

    def test_directory_skill_support_files_copied(self, config):
        v1 = config.samples_skills_dir / "logtriage"
        v2 = config.samples_skills_dir / "logtriage-v2"
        assert (v1 / "scripts" / "parse_logs.py").is_file()
        assert (v1 / "references" / "log-format.md").is_file()
        assert (v2 / "references" / "report-template.md").is_file()
        assert _sha256(v2 / "SKILL.md") == _sha256(
            PROJECT_ROOT / "data/skilleval_demo/logtriage_skill_v2/SKILL.md"
        )

    def test_sidecar_drives_name_and_is_hidden(self, config):
        skill = skill_sources.get_skill(config, "sample--searchqa-gpt5.5")
        assert skill is not None
        assert skill.name == "SearchQA 检索问答（论文 checkpoint）"
        assert "任务集" in skill.description
        assert skill.files_count == 1  # sidecar not counted

        detail = skill_sources.get_skill_detail(config, "sample--searchqa-gpt5.5")
        assert samples.SIDECAR_FILE not in detail.file_tree

        assert skill_sources.read_skill_file(config, "sample--searchqa-gpt5.5", samples.SIDECAR_FILE) is None

    def test_sidecar_404_via_api(self, client):
        response = client.get(
            "/api/skills/sample--searchqa-gpt5.5/files", params={"path": samples.SIDECAR_FILE}
        )
        assert response.status_code == 404

    def test_samples_disabled_by_default(self, tmp_path):
        cfg = StudioConfig(studio_root=tmp_path / "studio", skill_sources={})
        assert cfg.samples_enabled is False
        samples.materialize_samples(cfg)  # no-op
        assert not any(s.source == "sample" for s in skill_sources.scan_skills(cfg))

    def test_from_env_default_on_and_opt_out(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SKILLOPT_STUDIO_ROOT", str(tmp_path / "studio"))
        monkeypatch.delenv("SKILLOPT_STUDIO_SAMPLES", raising=False)
        assert StudioConfig.from_env().samples_enabled is True
        monkeypatch.setenv("SKILLOPT_STUDIO_SAMPLES", "0")
        assert StudioConfig.from_env().samples_enabled is False

    def test_materialization_idempotent(self, config):
        before = {
            s.id: _sha256(Path(s.path) / "SKILL.md") for s in skill_sources.scan_skills(config)
        }
        samples.materialize_samples(config)
        after = {
            s.id: _sha256(Path(s.path) / "SKILL.md") for s in skill_sources.scan_skills(config)
        }
        assert before == after and len(before) == 9

    def test_create_app_survives_materialization_failure(self, tmp_path, monkeypatch):
        def boom(cfg):
            raise RuntimeError("materialization exploded")

        monkeypatch.setattr(samples, "materialize_samples", boom)
        cfg = StudioConfig(studio_root=tmp_path / "studio", skill_sources={}, samples_enabled=True)
        app = create_app(cfg)
        assert TestClient(app).get("/api/health").json() == {"status": "ok"}


class TestSampleTaskSets:
    def test_sample_tasksets_with_expected_counts(self, config):
        found = {t.id: t for t in tasksets.list_tasksets(config) if t.sample}
        assert set(found) == EXPECTED_TASKSET_IDS
        assert found["sample-logtriage"].counts_by_split == {"train": 4, "val": 3, "test": 3}
        assert found["sample-report"].counts_by_split == {"train": 4, "val": 3, "test": 3}
        assert found["sample-xlsx"].counts_by_split == {"tasks": 3}
        for converted in ("sample-searchqa", "sample-livemath", "sample-officeqa"):
            assert found[converted].counts_by_split == {"train": 12, "val": 6, "test": 12}, converted

    def test_livemath_conversion_shape(self, config):
        task = tasksets.get_taskset_tasks(config, "sample-livemath")["train"][0]
        source = json.loads(
            (PROJECT_ROOT / "data/livemathematicianbench_split/train/items.json").read_text(
                encoding="utf-8"
            )
        )[0]
        assert task["id"] == source["id"].replace(":", "-")
        assert task["task_type"] == "livemath"
        assert source["question"] in task["question"]
        for choice in source["choices"]:
            assert choice["text"] in task["question"]
        assert source["correct_choice"]["label"] in task["rubric"]

    def test_officeqa_conversion_shape(self, config):
        task = tasksets.get_taskset_tasks(config, "sample-officeqa")["train"][0]
        assert task["task_type"] == "officeqa"
        assert "参考文档" in task["question"]
        assert len(task["question"]) > 500  # oracle context actually embedded
        assert "标准答案" in task["rubric"]

    def test_missing_livemath_source_skips_only_livemath(self, tmp_path, monkeypatch):
        monkeypatch.setattr(samples, "LIVEMATH_SOURCE", "data/does_not_exist_split")
        cfg = StudioConfig(studio_root=tmp_path / "studio", skill_sources={}, samples_enabled=True)
        samples.materialize_samples(cfg)
        ids = {t.id for t in tasksets.list_tasksets(cfg) if t.sample}
        assert ids == EXPECTED_TASKSET_IDS - {"sample-livemath"}

    def test_searchqa_conversion_shape(self, config):
        by_split = tasksets.get_taskset_tasks(config, "sample-searchqa")
        task = by_split["train"][0]
        source = json.loads(
            (PROJECT_ROOT / "data/searchqa_split/train/items.json").read_text(encoding="utf-8")
        )[0]
        assert task["id"] == source["id"]
        assert task["task_type"] == "searchqa"
        assert source["question"] in task["question"]
        assert "检索到的上下文" in task["question"]
        for answer in source["answers"]:
            assert answer in task["rubric"]
        assert "1.0" in task["rubric"] and "0.0" in task["rubric"]

    def test_missing_searchqa_source_skips_only_searchqa(self, tmp_path, monkeypatch):
        monkeypatch.setattr(samples, "SEARCHQA_SOURCE", "data/does_not_exist_split")
        cfg = StudioConfig(studio_root=tmp_path / "studio", skill_sources={}, samples_enabled=True)
        samples.materialize_samples(cfg)
        ids = {t.id for t in tasksets.list_tasksets(cfg) if t.sample}
        assert ids == EXPECTED_TASKSET_IDS - {"sample-searchqa"}

    def test_put_and_delete_sample_return_400(self, client):
        put = client.put(
            "/api/tasksets/sample-logtriage",
            json={"tasks_by_split": {"train": [], "val": []}},
        )
        assert put.status_code == 400
        assert "只读" in put.json()["detail"]

        delete = client.delete("/api/tasksets/sample-logtriage")
        assert delete.status_code == 400
        assert "只读" in delete.json()["detail"]

    def test_user_taskset_crud_unaffected(self, client):
        create = client.post(
            "/api/tasksets/items",
            json={
                "name": "mine",
                "mode": "single",
                "tasks_by_split": {"tasks": [{"id": "t1", "question": "q", "rubric": "r"}]},
            },
        )
        assert create.status_code == 200
        assert create.json()["sample"] is False
        assert client.delete("/api/tasksets/mine").status_code == 200

    def test_user_taskset_occupying_sample_id_never_overwritten(self, tmp_path):
        cfg = StudioConfig(studio_root=tmp_path / "studio", skill_sources={}, samples_enabled=True)
        user_tasks = [{"id": "mine", "question": "q", "rubric": "r"}]
        tasksets.create_taskset_from_items(
            cfg, "sample-xlsx", "single", {"tasks": user_tasks}
        )
        samples.materialize_samples(cfg)
        kept = tasksets.get_taskset_tasks(cfg, "sample-xlsx")
        assert kept["tasks"][0]["id"] == "mine"
        assert tasksets.get_taskset(cfg, "sample-xlsx").sample is False

    def test_rematerialization_preserves_user_tasksets(self, config):
        tasksets.create_taskset_from_items(
            config, "用户集", "single", {"tasks": [{"id": "u1", "question": "q", "rubric": "r"}]}
        )
        samples.materialize_samples(config)
        ids = {t.id for t in tasksets.list_tasksets(config)}
        assert "用户集" in ids and EXPECTED_TASKSET_IDS <= ids

    def test_samples_hidden_when_disabled(self, config):
        disabled = StudioConfig(
            studio_root=config.studio_root, skill_sources={}, samples_enabled=False
        )
        assert all(not t.sample for t in tasksets.list_tasksets(disabled))
        assert tasksets.get_taskset(disabled, "sample-logtriage") is None
        assert tasksets.get_taskset(disabled, "sample-logtriage", include_samples=True) is not None


class TestSampleRunnersIntegration:
    def test_build_eval_command_accepts_sample_ids(self, config, tmp_path, monkeypatch):
        from skillopt_studio import runners

        monkeypatch.setattr(runners, "cli_path", lambda backend: "/usr/bin/true")
        argv = runners.build_eval_command(
            config,
            {"skill_id": "sample--logtriage", "taskset_id": "sample-logtriage"},
            tmp_path / "job",
        )
        tasks_arg = argv[argv.index("--tasks") + 1]
        assert tasks_arg.endswith("test.json")
        skill_arg = argv[argv.index("--skill") + 1]
        assert skill_arg == str(config.samples_skills_dir / "logtriage")

    def test_build_train_command_accepts_sample_ids(self, config, tmp_path, monkeypatch):
        from skillopt_studio import runners

        monkeypatch.setattr(runners, "cli_path", lambda backend: "/usr/bin/true")
        job_dir = tmp_path / "job"
        argv = runners.build_train_command(
            config,
            {"skill_id": "sample--logtriage", "taskset_id": "sample-logtriage"},
            job_dir,
        )
        assert argv[0].endswith("python3") or "python" in argv[0]
        assert (job_dir / "config.yaml").is_file()
        for split in ("train", "val", "test"):
            assert (job_dir / "split" / split / "items.json").is_file()
