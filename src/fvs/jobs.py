"""The job queue: a ``jobs`` table plus an in-process worker pool (ADR-4, ADR-7).

- Jobs outlive the chat that started them (BAT-4): they belong to the core, not to Claude.
- Same inputs never start a duplicate job (MCP-6): ``submit`` takes an idempotency key.
- Restart safety (NFR-R1): on start, jobs left ``running`` are queued again once
  (``attempt`` + 1) or failed cleanly, and leftover temp files are deleted.
- Failure is a flag on the project (DS 7): a failed job sets ``projects.failed`` and
  ``last_error``; a later successful job for the project clears them.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fvs.activity import ActivityLog
from fvs.atomic import remove_temp_files
from fvs.errors import ErrorCode, FvsError
from fvs.events import EventBus
from fvs.logs import get_logger, scrub
from fvs.store import Database
from fvs.timeutil import now_iso

MAX_ATTEMPTS = 2  # the first run plus one re-queue after a crash
ACTIVE_STATES = ("queued", "running")
log = get_logger("fvs.jobs")


class JobCancelled(Exception):
    """Raised inside a handler (via ``ctx.check_cancelled()``) to stop cleanly."""


class JobContext:
    def __init__(self, queue: JobQueue, row: dict[str, Any], cancel_event: threading.Event):
        self._queue = queue
        self.job_id: str = row["id"]
        self.kind: str = row["kind"]
        self.project_id: str | None = row["project_id"]
        self.attempt: int = row["attempt"]
        self.payload: dict[str, Any] = json.loads(row["payload"] or "{}")
        self._cancel = cancel_event

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    def wait_cancelled(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; returns True early if cancel was asked."""
        return self._cancel.wait(timeout)

    def progress(self, fraction: float, note: str | None = None) -> None:
        self._queue._set_progress(self.job_id, max(0.0, min(1.0, fraction)), note)


Handler = Callable[[JobContext], dict[str, Any] | None]


def public_job(row: dict[str, Any] | Any) -> dict[str, Any]:
    r = dict(row)
    out = {
        "id": r["id"],
        "kind": r["kind"],
        "project_id": r["project_id"],
        "parent_id": r["parent_id"],
        "state": r["state"],
        "progress": r["progress"],
        "progress_note": r["progress_note"],
        "attempt": r["attempt"],
        "payload": json.loads(r["payload"] or "{}"),
        "result": json.loads(r["result"]) if r["result"] else None,
        "error": None,
        "created_at": r["created_at"],
        "started_at": r["started_at"],
        "finished_at": r["finished_at"],
    }
    if r["error_code"]:
        out["error"] = {"code": r["error_code"], "message": r["error_message"],
                        "fix": r["fix_hint"]}
    return out


class JobQueue:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        activity: ActivityLog,
        *,
        workers: int,
        workspace: Path,
    ) -> None:
        self.db = db
        self.bus = bus
        self.activity = activity
        self.workspace = workspace
        self.n_workers = max(1, workers)
        self._handlers: dict[str, Handler] = {}
        self._wake = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()

    # ---- registration and lifecycle -------------------------------------------------

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def recover(self) -> dict[str, int]:
        """Run once at start-up, before workers start."""
        requeued = failed = cancelled = 0
        at = now_iso()
        with self.db.transaction() as conn:
            for row in conn.execute("SELECT * FROM jobs WHERE state = 'running'").fetchall():
                if row["cancel_requested"]:
                    self._finish(conn, row["id"], "cancelled", at)
                    cancelled += 1
                elif row["attempt"] < MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE jobs SET state = 'queued', attempt = attempt + 1, progress = 0, "
                        "progress_note = 'Queued again after the app stopped', "
                        "started_at = NULL, updated_at = ? WHERE id = ?",
                        (at, row["id"]),
                    )
                    requeued += 1
                else:
                    self._finish(
                        conn, row["id"], "failed", at,
                        error=FvsError(
                            ErrorCode.JOB_INTERRUPTED,
                            "The job was interrupted twice when the app stopped.",
                            "Start it again; if it keeps stopping, run `fvs doctor`.",
                        ),
                    )
                    failed += 1
            for row in conn.execute(
                "SELECT id FROM jobs WHERE state = 'queued' AND cancel_requested = 1"
            ).fetchall():
                self._finish(conn, row["id"], "cancelled", at)
                cancelled += 1
        removed = len(remove_temp_files(self.workspace))
        counts = {"requeued": requeued, "failed": failed, "cancelled": cancelled,
                  "temp_files_removed": removed}
        if requeued or failed or cancelled:
            log.info("jobs_recovered", **counts)
            self.activity.add(
                f"Restarted: {requeued} job(s) queued again, {failed} failed, "
                f"{cancelled} cancelled."
            )
        return counts

    def start(self) -> None:
        self._stop.clear()
        for i in range(self.n_workers):
            t = threading.Thread(target=self._worker, name=f"fvs-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 10.0) -> None:
        """Ask running jobs to stop and wait for the workers.

        Jobs still running when the process exits stay ``running`` in the database and
        are re-queued by ``recover`` on the next start.
        """
        self._stop.set()
        with self._cancel_lock:
            for ev in self._cancel_events.values():
                ev.set()
        with self._wake:
            self._wake.notify_all()
        for t in self._threads:
            t.join(timeout)
        self._threads.clear()

    # ---- public API ----------------------------------------------------------------

    def submit(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        project_id: str | None = None,
        parent_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        at = now_iso()
        with self.db.transaction() as conn:
            if idempotency_key is not None:
                existing = conn.execute(
                    "SELECT * FROM jobs WHERE kind = ? AND idempotency_key = ? "
                    "AND state IN ('queued', 'running', 'done') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (kind, idempotency_key),
                ).fetchone()
                if existing is not None:
                    return public_job(existing)
            job_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO jobs (id, kind, project_id, parent_id, payload, idempotency_key, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, kind, project_id, parent_id, json.dumps(payload or {}),
                 idempotency_key, at, at),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        job = public_job(row)
        self.bus.publish({"type": "job", "job": job})
        with self._wake:
            self._wake.notify()
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise FvsError(
                ErrorCode.NOT_FOUND,
                f"There is no job {job_id!r}.",
                'List current jobs with fvs_job_get and jobs="active".',
            )
        return public_job(row)

    def active(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
        return [public_job(r) for r in rows]

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["state"] not in ACTIVE_STATES:
            return job
        at = now_iso()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row["state"] == "queued":
                self._finish(conn, job_id, "cancelled", at)
            elif row["state"] == "running":
                conn.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (at, job_id),
                )
        with self._cancel_lock:
            ev = self._cancel_events.get(job_id)
        if ev is not None:
            ev.set()
        job = self.get(job_id)
        self.bus.publish({"type": "job", "job": job})
        return job

    # ---- worker internals ----------------------------------------------------------

    def _claim(self) -> dict[str, Any] | None:
        kinds = list(self._handlers)
        if not kinds:
            return None
        marks = ",".join("?" * len(kinds))
        at = now_iso()
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT id FROM jobs WHERE state = 'queued' AND kind IN ({marks}) "
                "ORDER BY created_at LIMIT 1",
                kinds,
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET state = 'running', started_at = ?, updated_at = ? WHERE id = ?",
                (at, at, row["id"]),
            )
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return dict(claimed)

    def _worker(self) -> None:
        while not self._stop.is_set():
            row = self._claim()
            if row is None:
                with self._wake:
                    self._wake.wait(timeout=1.0)
                continue
            self._run(row)

    def _run(self, row: dict[str, Any]) -> None:
        ev = threading.Event()
        with self._cancel_lock:
            self._cancel_events[row["id"]] = ev
        self.bus.publish({"type": "job", "job": public_job(row)})
        ctx = JobContext(self, row, ev)
        handler = self._handlers[row["kind"]]
        state, result, error = "done", None, None
        try:
            result = handler(ctx) or {}
        except JobCancelled:
            if self._stop.is_set() and not self._cancel_requested(row["id"]):
                return  # shutting down, not a user cancel: recover() re-queues it
            state = "cancelled"
        except FvsError as exc:
            state, error = "failed", exc
        except Exception as exc:  # noqa: BLE001 - a job must never kill its worker
            log.exception("job_crashed", job_id=row["id"], kind=row["kind"])
            state = "failed"
            error = FvsError(
                ErrorCode.INTERNAL,
                f"The {row['kind']} job stopped with an unexpected error: {scrub(str(exc))}",
                "Try again; if it happens again, open the job log and report it.",
            )
        finally:
            with self._cancel_lock:
                self._cancel_events.pop(row["id"], None)

        at = now_iso()
        with self.db.transaction() as conn:
            self._finish(conn, row["id"], state, at, result=result, error=error)
            if row["project_id"] and state in ("done", "failed"):
                conn.execute(
                    "UPDATE projects SET failed = ?, last_error = ?, updated_at = ? WHERE id = ?",
                    (1 if state == "failed" else 0,
                     json.dumps(error.to_dict()) if error else None, at, row["project_id"]),
                )
        job = self.get(row["id"])
        self.bus.publish({"type": "job", "job": job})
        summary = (result or {}).get("summary") if state == "done" else None
        if state == "done":
            line = summary or f"Finished the {row['kind']} job."
        elif state == "failed":
            line = f"The {row['kind']} job failed: {error.message} {error.fix}"  # type: ignore[union-attr]
        else:
            line = f"Cancelled the {row['kind']} job."
        self.activity.add(line, project_id=row["project_id"])

    def _cancel_requested(self, job_id: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def _set_progress(self, job_id: str, fraction: float, note: str | None) -> None:
        at = now_iso()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE jobs SET progress = ?, progress_note = COALESCE(?, progress_note), "
                "updated_at = ? WHERE id = ? AND state = 'running'",
                (fraction, note, at, job_id),
            )
        self.bus.publish({"type": "job_progress", "job_id": job_id, "progress": fraction,
                          "note": note})

    @staticmethod
    def _finish(
        conn: Any,
        job_id: str,
        state: str,
        at: str,
        *,
        result: dict[str, Any] | None = None,
        error: FvsError | None = None,
    ) -> None:
        conn.execute(
            "UPDATE jobs SET state = ?, finished_at = ?, updated_at = ?, "
            "progress = CASE WHEN ? = 'done' THEN 1 ELSE progress END, "
            "result = ?, error_code = ?, error_message = ?, fix_hint = ? WHERE id = ?",
            (state, at, at, state,
             json.dumps(result) if result is not None else None,
             str(error.code) if error else None,
             error.message if error else None,
             error.fix if error else None,
             job_id),
        )
