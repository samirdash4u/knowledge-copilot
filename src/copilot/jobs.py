"""Background jobs for the web UI: ingest, eval and model download.

Jobs run one at a time on a single worker thread inside the API process, so a long eval never
competes with an ingest for the CPU or GPU, and the same code runs in Docker and natively on
Windows. Each job reports progress through :class:`JobContext`, which the web UI polls.
Jobs live in memory: a restart forgets the history, but not what a job produced (the index, the
eval report).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
LOG_LINES = 200


class JobCancelled(Exception):
    """Raised inside a job when someone pressed Cancel."""


@dataclass
class Job:
    id: str
    kind: str
    label: str
    started_by: str
    params: dict = field(default_factory=dict)
    status: str = "queued"  # queued, running, succeeded, failed, cancelled
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    done: int = 0
    total: int = 0
    message: str = "Waiting for other jobs to finish"
    log: list[str] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    cancel_requested: bool = False

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    def to_dict(self, details: bool = True) -> dict:
        out = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "started_by": self.started_by,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "done": self.done,
            "total": self.total,
            "message": self.message,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
        }
        if details:
            out["log"] = list(self.log)
            out["result"] = self.result
        return out


class JobContext:
    """Handed to a running job: report progress, write log lines, notice a cancel."""

    def __init__(self, job: Job, lock: threading.Lock) -> None:
        self._job = job
        self._lock = lock

    def check_cancelled(self) -> None:
        if self._job.cancel_requested:
            raise JobCancelled()

    def progress(self, done: int, total: int, message: str | None = None) -> None:
        with self._lock:
            self._job.done, self._job.total = done, total
            if message is not None:
                self._job.message = message
        self.check_cancelled()

    def log(self, line: str) -> None:
        with self._lock:
            self._job.log.append(line)
            del self._job.log[:-LOG_LINES]
            self._job.message = line


JobFn = Callable[[JobContext], dict | None]


class JobManager:
    def __init__(self, keep: int = 50) -> None:
        self.keep = keep
        self._jobs: dict[str, Job] = {}
        self._fns: dict[str, JobFn] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def submit(
        self,
        kind: str,
        label: str,
        fn: JobFn,
        started_by: str,
        params: dict | None = None,
        reuse_queued: bool = False,
    ) -> tuple[Job, bool]:
        """Queue a job. Returns (job, created).

        With ``reuse_queued``, a job of the same kind that has not started yet is returned
        instead of queueing a second one: it will see the same files when it starts.
        """
        with self._lock:
            if reuse_queued:
                for job in self._jobs.values():
                    if job.kind == kind and job.status == "queued" and not job.cancel_requested:
                        return job, False
            job = Job(uuid.uuid4().hex[:12], kind, label, started_by, params or {})
            self._jobs[job.id] = job
            self._fns[job.id] = fn
            self._prune()
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="copilot-jobs", daemon=True)
                self._worker.start()
        self._queue.put(job.id)
        return job, True

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def active(self, kind: str | None = None) -> list[Job]:
        return [j for j in self.list() if j.active and (kind is None or j.kind == kind)]

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or not job.active:
                return job
            job.cancel_requested = True
            if job.status == "queued":
                job.status, job.finished_at = "cancelled", time.time()
                job.message = "Cancelled before it started"
            else:
                job.message = "Cancelling…"
        return job

    def wait(self, job_id: str, timeout: float = 60) -> Job:
        """Block until the job finishes (for tests and scripts)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self._jobs[job_id]
            if not job.active:
                return job
            time.sleep(0.05)
        raise TimeoutError(f"job {job_id} still {self._jobs[job_id].status}")

    def _prune(self) -> None:
        finished = [j for j in self._jobs.values() if not j.active]
        finished.sort(key=lambda j: j.created_at)
        for job in finished[: max(0, len(self._jobs) - self.keep)]:
            self._jobs.pop(job.id, None)
            self._fns.pop(job.id, None)

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self._jobs.get(job_id)
            fn = self._fns.pop(job_id, None)
            if job is None or fn is None or job.status != "queued":
                continue
            with self._lock:
                job.status, job.started_at, job.message = "running", time.time(), "Starting"
            ctx = JobContext(job, self._lock)
            try:
                result = fn(ctx)
                status, error = "succeeded", None
            except JobCancelled:
                result, status, error = None, "cancelled", None
            except Exception as exc:  # noqa: BLE001 - any failure ends the job, not the worker
                log.error("job %s (%s) failed:\n%s", job.id, job.kind, traceback.format_exc())
                result, status, error = None, "failed", str(exc) or type(exc).__name__
            with self._lock:
                job.result, job.status, job.error = result, status, error
                job.finished_at = time.time()
                job.message = {
                    "succeeded": "Finished",
                    "cancelled": "Cancelled",
                    "failed": f"Failed: {error}",
                }[status]
