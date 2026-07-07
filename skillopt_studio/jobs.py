"""Subprocess-backed job manager with on-disk persistence.

Every job is a directory ``studio_root/jobs/<id>/`` holding ``job.json``
(the JobInfo record, updated on every transition) and ``log.txt`` (merged
stdout+stderr).  Reading state always goes through the disk record, so job
history survives a backend restart.

Execution model: a single daemon worker thread drains a FIFO queue with
``max_concurrent_jobs`` slots (default 1 — SkillOpt runs are heavyweight).
Child processes start in their own session (``start_new_session=True``) so
cancellation can ``os.killpg`` the whole tree: SIGTERM, a 3s grace period,
then SIGKILL.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from skillopt_studio.config import StudioConfig
from skillopt_studio.models import JobInfo

_JOB_FILE = "job.json"
_LOG_FILE = "log.txt"

_CANCEL_GRACE_SECONDS = 3.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobManager:
    def __init__(self, config: StudioConfig, autostart: bool = True) -> None:
        self.config = config
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.RLock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancel_requested: set[str] = set()
        self._workers: list[threading.Thread] = []
        if autostart:
            self.start()

    # ── lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._workers:
                return
            for index in range(max(1, self.config.max_concurrent_jobs)):
                worker = threading.Thread(
                    target=self._worker_loop, name=f"studio-job-worker-{index}", daemon=True
                )
                worker.start()
                self._workers.append(worker)

    def allocate_job_id(self, job_type: str) -> str:
        """Reserve an id + directory before the command is built (runners need
        the job dir path inside their argv)."""
        job_id = f"{job_type}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self._job_dir(job_id).mkdir(parents=True)
        return job_id

    def create_job(
        self,
        job_type: str,
        params: dict,
        cmd: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        out_root: str | None = None,
        job_id: str | None = None,
    ) -> JobInfo:
        if not cmd:
            raise ValueError("cmd must be a non-empty argv list")
        if job_id is None:
            job_id = self.allocate_job_id(job_type)
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "id": job_id,
            "type": job_type,
            "status": "queued",
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "params": params,
            "out_root": out_root,
            "exit_code": None,
            "error": None,
            # execution spec — extras beyond JobInfo, kept for debuggability
            "cmd": list(cmd),
            "cwd": cwd,
            "env": dict(env or {}),
        }
        self._write_record(job_id, record)
        self._queue.put(job_id)
        return JobInfo(**record)

    def get_job(self, job_id: str) -> JobInfo | None:
        record = self._read_record(job_id)
        return JobInfo(**record) if record else None

    def list_jobs(self) -> list[JobInfo]:
        jobs_dir = self.config.jobs_dir
        if not jobs_dir.is_dir():
            return []
        jobs = []
        for entry in jobs_dir.iterdir():
            record = self._read_record(entry.name)
            if record:
                jobs.append(JobInfo(**record))
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)

    def cancel(self, job_id: str) -> JobInfo:
        """Cancel a queued or running job; no-op statuses raise ValueError."""
        with self._lock:
            record = self._read_record(job_id)
            if record is None:
                raise KeyError(job_id)
            status = record["status"]
            if status == "queued":
                self._cancel_requested.add(job_id)
                record.update(status="cancelled", finished_at=_now_iso())
                self._write_record(job_id, record)
                return JobInfo(**record)
            if status != "running":
                raise ValueError(f"job {job_id} is {status}; only queued/running jobs can be cancelled")
            self._cancel_requested.add(job_id)
            proc = self._procs.get(job_id)

        if proc is None:
            # tiny race: status flipped to running before the worker registered
            # the Popen handle — retry briefly so the kill is not skipped
            for _ in range(100):
                time.sleep(0.02)
                with self._lock:
                    proc = self._procs.get(job_id)
                if proc is not None:
                    break
        if proc is not None:
            self._kill_process_group(proc)
        # the worker thread owns the final transition; wait for it briefly
        deadline = time.time() + _CANCEL_GRACE_SECONDS + 5.0
        while time.time() < deadline:
            record = self._read_record(job_id)
            if record and record["status"] == "cancelled":
                return JobInfo(**record)
            time.sleep(0.05)
        record = self._read_record(job_id) or {}
        raise RuntimeError(f"job {job_id} did not reach cancelled state (now {record.get('status')})")

    def read_log(self, job_id: str, offset: int = 0) -> dict:
        """Incremental log read: {'content': str, 'next_offset': byte offset}."""
        if self._read_record(job_id) is None:
            raise KeyError(job_id)
        log_path = self._job_dir(job_id) / _LOG_FILE
        if not log_path.is_file():
            return {"content": "", "next_offset": 0}
        offset = max(0, int(offset))
        with open(log_path, "rb") as log_f:
            log_f.seek(offset)
            data = log_f.read()
        return {"content": data.decode("utf-8", errors="replace"), "next_offset": offset + len(data)}

    # ── internals ────────────────────────────────────────────────

    def _job_dir(self, job_id: str) -> Path:
        if "/" in job_id or "\\" in job_id or ".." in job_id:
            raise ValueError(f"invalid job id {job_id!r}")
        return self.config.jobs_dir / job_id

    def _write_record(self, job_id: str, record: dict) -> None:
        path = self._job_dir(job_id) / _JOB_FILE
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _read_record(self, job_id: str) -> dict | None:
        try:
            path = self._job_dir(job_id) / _JOB_FILE
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run_job(job_id)
            except Exception as exc:  # keep the worker alive; surface in the record
                with self._lock:
                    record = self._read_record(job_id)
                    if record and record["status"] in ("queued", "running"):
                        record.update(status="failed", finished_at=_now_iso(), error=repr(exc))
                        self._write_record(job_id, record)
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            record = self._read_record(job_id)
            if record is None or record["status"] != "queued":
                return  # cancelled (or removed) while waiting in the queue
            record.update(status="running", started_at=_now_iso())
            self._write_record(job_id, record)

        env = dict(os.environ)
        env.update(record.get("env") or {})
        log_path = self._job_dir(job_id) / _LOG_FILE
        try:
            with open(log_path, "ab") as log_f:
                proc = subprocess.Popen(
                    record["cmd"],
                    cwd=record.get("cwd") or None,
                    env=env,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            with self._lock:
                record.update(status="failed", finished_at=_now_iso(), error=f"failed to spawn: {exc}")
                self._write_record(job_id, record)
            return

        with self._lock:
            self._procs[job_id] = proc
        exit_code = proc.wait()
        with self._lock:
            self._procs.pop(job_id, None)
            cancelled = job_id in self._cancel_requested
            self._cancel_requested.discard(job_id)
            record = self._read_record(job_id) or record
            record["exit_code"] = exit_code
            record["finished_at"] = _now_iso()
            if cancelled:
                record["status"] = "cancelled"
            elif exit_code == 0:
                record["status"] = "succeeded"
            else:
                record["status"] = "failed"
                record["error"] = f"process exited with code {exit_code}"
            self._write_record(job_id, record)

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen) -> None:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=_CANCEL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
