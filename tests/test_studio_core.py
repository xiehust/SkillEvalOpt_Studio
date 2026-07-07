"""Tests for skillopt_studio core: config, skill scanning/upload, tasksets, jobs, API.

No network and no model calls — subprocess jobs use short ``python3 -c`` commands.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillopt_studio.app import create_app
from skillopt_studio.config import StudioConfig
from skillopt_studio.jobs import JobManager
from skillopt_studio.models import SkillInfo
from skillopt_studio.skill_sources import scan_skills, upload_skill_zip
from skillopt_studio.tasksets import (
    delete_taskset,
    get_taskset,
    get_taskset_tasks,
    list_tasksets,
    save_taskset,
)

SOURCES = ("claude", "codex", "kiro", "agents")


@pytest.fixture
def studio_config(tmp_path: Path) -> StudioConfig:
    return StudioConfig(
        studio_root=tmp_path / "studio",
        skill_sources={name: tmp_path / "sources" / name for name in SOURCES},
    )


def make_skill(root: Path, dirname: str, skill_md: str = "# Skill\n\nA test skill.\n") -> Path:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return skill_dir


def make_zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def valid_tasks(prefix: str = "t", count: int = 2) -> bytes:
    items = [
        {"id": f"{prefix}{i}", "question": f"Q{i}?", "rubric": f"Answer must mention {i}."}
        for i in range(count)
    ]
    return json.dumps(items).encode("utf-8")


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestStudioConfig:
    def test_env_override_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKILLOPT_STUDIO_ROOT", str(tmp_path / "custom-root"))
        config = StudioConfig.from_env()
        assert config.studio_root == tmp_path / "custom-root"

    def test_env_override_sources(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "SKILLOPT_STUDIO_SKILL_SOURCES",
            f"claude={tmp_path / 'a'},codex={tmp_path / 'b'}",
        )
        config = StudioConfig.from_env()
        assert config.skill_sources == {"claude": tmp_path / "a", "codex": tmp_path / "b"}


class TestScanSkills:
    def test_scans_all_four_sources(self, studio_config):
        for source in SOURCES:
            make_skill(studio_config.skill_sources[source], f"{source}-skill")
        skills = scan_skills(studio_config)
        assert {s.source for s in skills} == set(SOURCES)
        assert {s.id for s in skills} == {f"{source}--{source}-skill" for source in SOURCES}

    def test_skips_dirs_without_skill_md(self, studio_config):
        make_skill(studio_config.skill_sources["claude"], "real-skill")
        empty = studio_config.skill_sources["claude"] / "not-a-skill"
        empty.mkdir(parents=True)
        (empty / "README.md").write_text("no skill here", encoding="utf-8")
        skills = scan_skills(studio_config)
        assert [s.name for s in skills] == ["real-skill"]

    def test_codex_system_sublayer(self, studio_config):
        make_skill(studio_config.skill_sources["codex"] / ".system", "sys-skill")
        skills = scan_skills(studio_config)
        assert [s.id for s in skills] == ["codex--sys-skill"]

    def test_symlink_resolved_and_not_duplicated(self, studio_config, tmp_path):
        real = make_skill(studio_config.skill_sources["claude"], "alpha")
        agents_root = studio_config.skill_sources["agents"]
        agents_root.mkdir(parents=True)
        (agents_root / "alpha-link").symlink_to(real, target_is_directory=True)
        external = make_skill(tmp_path / "elsewhere", "beta")
        (agents_root / "beta-link").symlink_to(external, target_is_directory=True)

        skills = scan_skills(studio_config)
        names = sorted(s.name for s in skills)
        assert names == ["alpha", "beta"]
        alpha = next(s for s in skills if s.name == "alpha")
        assert alpha.source == "claude"  # first source reaching the physical dir wins

    def test_description_from_frontmatter(self, studio_config):
        make_skill(
            studio_config.skill_sources["kiro"],
            "fm-skill",
            "---\nname: Fancy Name\ndescription: Does fancy things\n---\n# Heading\nBody\n",
        )
        (skill,) = scan_skills(studio_config)
        assert skill.description == "Does fancy things"
        assert skill.name == "Fancy Name"

    def test_description_fallback_first_content_line(self, studio_config):
        make_skill(
            studio_config.skill_sources["kiro"],
            "plain-skill",
            "# Big Title\n\nFirst real sentence of the doc.\nMore text.\n",
        )
        (skill,) = scan_skills(studio_config)
        assert skill.description == "First real sentence of the doc."

    def test_missing_source_roots_are_ok(self, studio_config):
        assert scan_skills(studio_config) == []

    def test_support_files_counted(self, studio_config):
        skill_dir = make_skill(studio_config.skill_sources["agents"], "with-helpers")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "helper.py").write_text("print('hi')\n", encoding="utf-8")
        (skill,) = scan_skills(studio_config)
        assert skill.files_count == 2
        assert skill.has_support_files is True


class TestUploadSkillZip:
    def test_valid_zip_root_level(self, studio_config):
        data = make_zip({"SKILL.md": "---\ndescription: uploaded skill\n---\n# Up\n"})
        info = upload_skill_zip(studio_config, data, "My Skill")
        assert isinstance(info, SkillInfo)
        assert info.source == "uploaded"
        assert (studio_config.skills_dir / "My-Skill" / "SKILL.md").is_file()
        assert any(s.id == info.id for s in scan_skills(studio_config))

    def test_valid_zip_single_top_dir_stripped(self, studio_config):
        data = make_zip({"pack/SKILL.md": "# P\n", "pack/scripts/run.py": "pass\n"})
        info = upload_skill_zip(studio_config, data, "pack")
        root = studio_config.skills_dir / "pack"
        assert (root / "SKILL.md").is_file()
        assert (root / "scripts" / "run.py").is_file()
        assert info.has_support_files is True

    def test_zip_slip_rejected_nothing_written(self, studio_config):
        data = make_zip({"../evil.txt": "pwned", "SKILL.md": "# ok\n"})
        with pytest.raises(ValueError, match="escapes"):
            upload_skill_zip(studio_config, data, "evil")
        assert not (studio_config.skills_dir / "evil").exists()
        assert not (studio_config.skills_dir / "evil.txt").exists()
        assert not (studio_config.studio_root / "evil.txt").exists()
        escaped = [p for p in studio_config.studio_root.rglob("*") if p.name == "evil.txt"]
        assert escaped == []

    def test_absolute_member_rejected(self, studio_config):
        data = make_zip({"/abs.txt": "x", "SKILL.md": "# ok\n"})
        with pytest.raises(ValueError, match="unsafe|escapes"):
            upload_skill_zip(studio_config, data, "abs")

    def test_missing_skill_md_rejected(self, studio_config):
        data = make_zip({"README.md": "not a skill"})
        with pytest.raises(ValueError, match="SKILL.md"):
            upload_skill_zip(studio_config, data, "readme-only")
        assert not (studio_config.skills_dir / "readme-only").exists()

    def test_oversize_rejected(self, studio_config):
        studio_config.max_skill_zip_bytes = 64
        data = make_zip({"SKILL.md": "x" * 4096})
        with pytest.raises(ValueError, match="limit"):
            upload_skill_zip(studio_config, data, "big")


class TestTaskSets:
    def test_save_and_get_single(self, studio_config):
        info = save_taskset(studio_config, "qa set", {"tasks": valid_tasks(count=3)}, "single")
        assert info.id == "qa-set"
        assert info.mode == "single"
        assert info.task_count == 3
        assert get_taskset(studio_config, "qa-set").name == "qa set"
        tasks = get_taskset_tasks(studio_config, "qa-set")
        assert [t["id"] for t in tasks["tasks"]] == ["t0", "t1", "t2"]
        assert list_tasksets(studio_config)[0].id == "qa-set"

    def test_missing_rubric_failfast_no_partial_dir(self, studio_config):
        bad = json.dumps([{"id": "t0", "question": "Q?"}]).encode("utf-8")
        with pytest.raises(ValueError, match="rubric"):
            save_taskset(studio_config, "bad", {"tasks": bad}, "single")
        assert not (studio_config.tasksets_dir / "bad").exists()
        assert list_tasksets(studio_config) == []

    def test_split_mode_counts(self, studio_config):
        files = {
            "train": valid_tasks("tr", 4),
            "val": valid_tasks("va", 2),
            "test": valid_tasks("te", 1),
        }
        info = save_taskset(studio_config, "split-set", files, "split")
        assert info.counts_by_split == {"train": 4, "val": 2, "test": 1}
        assert info.task_count == 7

    def test_split_mode_requires_train_and_val(self, studio_config):
        with pytest.raises(ValueError, match="val"):
            save_taskset(studio_config, "half", {"train": valid_tasks()}, "split")

    def test_duplicate_name_rejected(self, studio_config):
        save_taskset(studio_config, "dupe", {"tasks": valid_tasks()}, "single")
        with pytest.raises(ValueError, match="already exists"):
            save_taskset(studio_config, "dupe", {"tasks": valid_tasks()}, "single")

    def test_delete(self, studio_config):
        save_taskset(studio_config, "gone", {"tasks": valid_tasks()}, "single")
        assert delete_taskset(studio_config, "gone") is True
        assert get_taskset(studio_config, "gone") is None
        assert delete_taskset(studio_config, "gone") is False

    def test_chinese_name_slug(self, studio_config):
        info = save_taskset(studio_config, "算术 回归集", {"tasks": valid_tasks()}, "single")
        assert info.id == "算术-回归集"
        assert get_taskset(studio_config, "算术-回归集").name == "算术 回归集"
        assert (studio_config.tasksets_dir / "算术-回归集" / "tasks.json").is_file()


class TestJobManager:
    def test_echo_job_lifecycle_succeeds(self, studio_config):
        manager = JobManager(studio_config)
        job = manager.create_job("echo", {}, [sys.executable, "-c", "print('hello-studio')"])
        assert job.status == "queued"
        assert wait_until(lambda: manager.get_job(job.id).status == "succeeded")
        record = json.loads((studio_config.jobs_dir / job.id / "job.json").read_text(encoding="utf-8"))
        assert record["exit_code"] == 0
        assert record["started_at"] and record["finished_at"]
        assert "hello-studio" in manager.read_log(job.id)["content"]

    def test_nonzero_exit_failed(self, studio_config):
        manager = JobManager(studio_config)
        job = manager.create_job("echo", {}, [sys.executable, "-c", "import sys; sys.exit(3)"])
        assert wait_until(lambda: manager.get_job(job.id).status == "failed")
        final = manager.get_job(job.id)
        assert final.exit_code == 3
        assert "3" in (final.error or "")

    def test_cancel_running_job_kills_process_group(self, studio_config):
        manager = JobManager(studio_config)
        spawner = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "print(f'CHILD={child.pid}', flush=True)\n"
            "time.sleep(120)\n"
        )
        job = manager.create_job("echo", {}, [sys.executable, "-c", spawner])
        assert wait_until(lambda: "CHILD=" in manager.read_log(job.id)["content"])
        content = manager.read_log(job.id)["content"]
        child_pid = int(content.split("CHILD=")[1].splitlines()[0])
        assert pid_alive(child_pid)

        cancelled = manager.cancel(job.id)
        assert cancelled.status == "cancelled"
        assert manager.get_job(job.id).status == "cancelled"
        assert wait_until(lambda: not pid_alive(child_pid), timeout=10.0)

    def test_cancel_queued_job(self, studio_config):
        manager = JobManager(studio_config)  # max_concurrent_jobs=1
        blocker = manager.create_job("echo", {}, [sys.executable, "-c", "import time; time.sleep(60)"])
        assert wait_until(lambda: manager.get_job(blocker.id).status == "running")
        queued = manager.create_job("echo", {}, [sys.executable, "-c", "print('never runs')"])
        cancelled = manager.cancel(queued.id)
        assert cancelled.status == "cancelled"
        manager.cancel(blocker.id)
        assert manager.get_job(blocker.id).status == "cancelled"
        assert manager.get_job(queued.id).status == "cancelled"
        assert "never runs" not in manager.read_log(queued.id)["content"]

    def test_read_log_offset_incremental(self, studio_config):
        manager = JobManager(studio_config)
        job = manager.create_job("echo", {}, [sys.executable, "-c", "print('chunk-one')"])
        assert wait_until(lambda: manager.get_job(job.id).status == "succeeded")
        first = manager.read_log(job.id, 0)
        assert "chunk-one" in first["content"]
        assert first["next_offset"] > 0
        second = manager.read_log(job.id, first["next_offset"])
        assert second["content"] == ""
        assert second["next_offset"] == first["next_offset"]

    def test_history_survives_restart(self, studio_config):
        manager = JobManager(studio_config)
        job = manager.create_job("echo", {}, [sys.executable, "-c", "print('persist me')"])
        assert wait_until(lambda: manager.get_job(job.id).status == "succeeded")

        reloaded = JobManager(studio_config)  # fresh instance, same disk root
        listed = reloaded.list_jobs()
        assert [j.id for j in listed] == [job.id]
        assert reloaded.get_job(job.id).status == "succeeded"

    def test_cancel_unknown_job_raises(self, studio_config):
        manager = JobManager(studio_config)
        with pytest.raises(KeyError):
            manager.cancel("no-such-job")


class TestStudioApi:
    @pytest.fixture
    def client(self, studio_config):
        app = create_app(studio_config)
        with TestClient(app) as test_client:
            yield test_client

    def test_health(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_skills_list_and_detail(self, studio_config, client):
        make_skill(
            studio_config.skill_sources["claude"],
            "api-skill",
            "---\ndescription: via api\n---\n# API Skill\n",
        )
        listed = client.get("/api/skills").json()
        assert len(listed) == 1
        assert listed[0]["id"] == "claude--api-skill"
        assert listed[0]["description"] == "via api"

        detail = client.get("/api/skills/claude--api-skill")
        assert detail.status_code == 200
        body = detail.json()
        assert "# API Skill" in body["skill_md"]
        assert body["file_tree"] == ["SKILL.md"]

    def test_skill_detail_404(self, client):
        assert client.get("/api/skills/uploaded--nope").status_code == 404

    def test_upload_endpoint_and_zip_slip_400(self, studio_config, client):
        good = make_zip({"SKILL.md": "# uploaded via api\n"})
        response = client.post(
            "/api/skills/upload",
            files={"file": ("good.zip", good, "application/zip")},
            data={"name": "net-skill"},
        )
        assert response.status_code == 200
        assert response.json()["source"] == "uploaded"
        assert any(s["id"] == "uploaded--net-skill" for s in client.get("/api/skills").json())

        evil = make_zip({"../evil.txt": "pwn", "SKILL.md": "# x\n"})
        response = client.post(
            "/api/skills/upload",
            files={"file": ("evil.zip", evil, "application/zip")},
            data={"name": "evil"},
        )
        assert response.status_code == 400
        assert "escapes" in response.json()["detail"]
        assert not (studio_config.skills_dir / "evil").exists()

    def test_tasksets_crud_and_validation_400(self, client):
        response = client.post(
            "/api/tasksets",
            data={"name": "api set", "mode": "single"},
            files={"tasks": ("tasks.json", valid_tasks(count=2), "application/json")},
        )
        assert response.status_code == 200
        assert response.json()["task_count"] == 2

        listed = client.get("/api/tasksets").json()
        assert [t["id"] for t in listed] == ["api-set"]

        detail = client.get("/api/tasksets/api-set").json()
        assert [t["id"] for t in detail["tasks_by_split"]["tasks"]] == ["t0", "t1"]

        bad = json.dumps([{"id": "x", "question": "q"}]).encode("utf-8")
        response = client.post(
            "/api/tasksets",
            data={"name": "bad set", "mode": "single"},
            files={"tasks": ("tasks.json", bad, "application/json")},
        )
        assert response.status_code == 400
        assert "rubric" in response.json()["detail"]

        assert client.delete("/api/tasksets/api-set").json() == {"ok": True}
        assert client.get("/api/tasksets/api-set").status_code == 404
        assert client.delete("/api/tasksets/api-set").status_code == 404

    def test_jobs_echo_lifecycle_and_log(self, client):
        response = client.post("/api/jobs", json={"type": "echo", "params": {"message": "hi-from-api"}})
        assert response.status_code == 200
        job_id = response.json()["id"]

        assert wait_until(lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "succeeded")
        jobs = client.get("/api/jobs").json()
        assert [j["id"] for j in jobs] == [job_id]

        log = client.get(f"/api/jobs/{job_id}/log", params={"offset": 0}).json()
        assert "hi-from-api" in log["content"]
        tail = client.get(f"/api/jobs/{job_id}/log", params={"offset": log["next_offset"]}).json()
        assert tail["content"] == ""

    def test_unknown_job_type_400(self, client):
        response = client.post("/api/jobs", json={"type": "bogus", "params": {}})
        assert response.status_code == 400
        assert "unsupported" in response.json()["detail"]
        assert client.get("/api/jobs/definitely-missing").status_code == 404


class TestMainCli:
    def test_help_shows_defaults(self, capsys):
        from skillopt_studio.__main__ import main

        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0
        help_text = capsys.readouterr().out
        assert "--host" in help_text and "--port" in help_text and "--reload" in help_text
        assert "127.0.0.1" in help_text and "8321" in help_text
