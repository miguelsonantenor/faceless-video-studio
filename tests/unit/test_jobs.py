"""Job queue: results, failures, cancel, idempotency, restart safety (NFR-R1, MCP-6, BAT-4)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from fvs.atomic import TMP_SUFFIX
from fvs.core import Core
from fvs.errors import ErrorCode, FvsError
from fvs.jobs import JobContext, JobQueue
from fvs.timeutil import now_iso
from tests.conftest import wait_for


def _state(core: Core, job_id: str) -> str:
    return core.jobs.get(job_id)["state"]


def _add_project(core: Core, pid: str = "p1") -> None:
    with core.db.connect() as conn:
        conn.execute("INSERT INTO projects (id, number, title, slug, created_at, updated_at) "
                     "VALUES (?, 1, 'T', 't', ?, ?)", (pid, now_iso(), now_iso()))


def _project_flag(core: Core, pid: str = "p1") -> tuple[int, str | None]:
    with core.db.connect() as conn:
        row = conn.execute("SELECT failed, last_error FROM projects WHERE id = ?",
                           (pid,)).fetchone()
    return row["failed"], row["last_error"]


@pytest.fixture
def running(core: Core) -> Core:
    def echo(ctx: JobContext) -> dict:
        ctx.progress(0.5, "halfway")
        return {"summary": f"Echoed {ctx.payload.get('x')}.", "x": ctx.payload.get("x")}

    def fail(ctx: JobContext) -> dict:
        raise FvsError(ErrorCode.RENDER_FAILED, "FFmpeg ran out of disk space.",
                       "Free some space and render again.")

    def crash(ctx: JobContext) -> dict:
        raise ValueError("unexpected")

    def slow(ctx: JobContext) -> dict:
        while not ctx.wait_cancelled(0.02):
            pass
        ctx.check_cancelled()
        return {}

    for kind, fn in {"echo": echo, "fail": fail, "crash": crash, "slow": slow}.items():
        core.jobs.register(kind, fn)
    core.jobs.start()
    return core


def test_job_runs_to_done_with_result_progress_and_activity(running: Core) -> None:
    events: list[dict] = []
    running.bus.add_listener(events.append)
    job = running.jobs.submit("echo", {"x": 7})
    wait_for(lambda: _state(running, job["id"]) == "done")
    done = running.jobs.get(job["id"])
    assert done["result"] == {"summary": "Echoed 7.", "x": 7}
    assert done["progress"] == 1.0
    assert any(e["type"] == "job_progress" and e["note"] == "halfway" for e in events)
    assert running.activity.recent(1)[0]["summary"] == "Echoed 7."


def test_failure_sets_and_success_clears_the_project_flag(running: Core) -> None:
    _add_project(running)
    job = running.jobs.submit("fail", project_id="p1")
    wait_for(lambda: _state(running, job["id"]) == "failed")
    err = running.jobs.get(job["id"])["error"]
    assert err["code"] == "RENDER_FAILED" and "Free some space" in err["fix"]
    failed, last_error = _project_flag(running)
    assert failed == 1 and json.loads(last_error)["code"] == "RENDER_FAILED"

    job2 = running.jobs.submit("echo", project_id="p1")
    wait_for(lambda: _state(running, job2["id"]) == "done")
    assert _project_flag(running) == (0, None)


def test_unexpected_error_fails_cleanly_and_worker_survives(running: Core) -> None:
    job = running.jobs.submit("crash")
    wait_for(lambda: _state(running, job["id"]) == "failed")
    assert running.jobs.get(job["id"])["error"]["code"] == "INTERNAL"
    job2 = running.jobs.submit("echo")
    wait_for(lambda: _state(running, job2["id"]) == "done")


def test_same_inputs_do_not_start_a_duplicate(running: Core) -> None:
    a = running.jobs.submit("echo", {"x": 1}, idempotency_key="render:abc")
    b = running.jobs.submit("echo", {"x": 1}, idempotency_key="render:abc")
    assert a["id"] == b["id"]
    wait_for(lambda: _state(running, a["id"]) == "done")
    assert running.jobs.submit("echo", idempotency_key="render:abc")["id"] == a["id"]


def test_a_failed_job_can_be_retried_with_the_same_key(running: Core) -> None:
    a = running.jobs.submit("fail", idempotency_key="k")
    wait_for(lambda: _state(running, a["id"]) == "failed")
    assert running.jobs.submit("fail", idempotency_key="k")["id"] != a["id"]


def test_cancel_running_job(running: Core) -> None:
    job = running.jobs.submit("slow")
    wait_for(lambda: _state(running, job["id"]) == "running")
    running.jobs.cancel(job["id"])
    wait_for(lambda: _state(running, job["id"]) == "cancelled")


def test_cancel_queued_job(core: Core) -> None:  # no workers started
    job = core.jobs.submit("echo")
    assert core.jobs.cancel(job["id"])["state"] == "cancelled"


def test_unknown_job_is_not_found(core: Core) -> None:
    with pytest.raises(FvsError) as info:
        core.jobs.get("nope")
    assert info.value.code == ErrorCode.NOT_FOUND


def _insert_running(core: Core, job_id: str, attempt: int) -> None:
    at = now_iso()
    with core.db.connect() as conn:
        conn.execute("INSERT INTO jobs (id, kind, state, attempt, created_at, started_at, "
                     "updated_at) VALUES (?, 'render', 'running', ?, ?, ?, ?)",
                     (job_id, attempt, at, at, at))


def test_restart_requeues_once_then_fails_and_removes_temp_files(core: Core) -> None:
    _insert_running(core, "j1", attempt=1)
    _insert_running(core, "j2", attempt=2)
    tmp = core.config.folders.expanded().workspace / "projects" / f"r.mp4{TMP_SUFFIX}"
    tmp.write_bytes(b"half a render")

    counts = core.jobs.recover()

    assert counts == {"requeued": 1, "failed": 1, "cancelled": 0, "temp_files_removed": 1}
    j1, j2 = core.jobs.get("j1"), core.jobs.get("j2")
    assert (j1["state"], j1["attempt"]) == ("queued", 2)
    assert j2["state"] == "failed" and j2["error"]["code"] == "JOB_INTERRUPTED"
    assert not tmp.exists()


def test_stopping_mid_job_leaves_it_for_recovery(home: Path) -> None:
    core = Core.open(home)
    started = threading.Event()

    def slow(ctx: JobContext) -> dict:
        started.set()
        while not ctx.wait_cancelled(0.02):
            pass
        ctx.check_cancelled()
        return {}

    core.jobs.register("slow", slow)
    core.jobs.start()
    job = core.jobs.submit("slow")
    assert started.wait(5)
    core.jobs.stop(timeout=5)
    assert _state(core, job["id"]) == "running"  # as if the app was killed

    again = JobQueue(core.db, core.bus, core.activity, workers=1,
                     workspace=core.config.folders.expanded().workspace)
    again.recover()
    assert (again.get(job["id"])["state"], again.get(job["id"])["attempt"]) == ("queued", 2)
