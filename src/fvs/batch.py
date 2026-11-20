"""Batches: saved steps for many projects as one job (BAT-1, BAT-2, BAT-4).

The steps (voiceover, plan, render) run project by project inside the batch's own worker,
so a batch needs no second worker. One project's failure sets that project's failure flag
and the batch carries on with the others. The whole batch's characters are checked
against today's budget before it starts; going over needs ``allow_over_budget``, which
Claude sets only after the creator agreed in chat, and that is recorded.

Every step is safe to run twice (cached sentences, unchanged specs, renders reused by
hash), so a batch re-queued after a restart picks up where it stopped.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fvs.activity import ActivityLog, Source
from fvs.errors import ErrorCode, FvsError
from fvs.jobs import JobCancelled, JobContext, JobQueue, StepContext
from fvs.plan.planner import Planner
from fvs.projects import Projects
from fvs.render.service import RenderService
from fvs.spec.service import SpecService
from fvs.store import Database
from fvs.timeutil import now_iso
from fvs.voice.budget import Budget
from fvs.voice.service import VoiceService

STEPS = ("voiceover", "plan", "render")
SHARE = {"voiceover": 0.3, "plan": 0.05, "render": 0.65}  # of one project's progress


class BatchService:
    def __init__(self, db: Database, *, projects: Projects, voice: VoiceService,
                 planner: Planner, specs: SpecService, renders: RenderService,
                 jobs: JobQueue, budget: Budget, activity: ActivityLog) -> None:
        self.db = db
        self.projects = projects
        self.voice = voice
        self.planner = planner
        self.specs = specs
        self.renders = renders
        self.jobs = jobs
        self.budget = budget
        self.activity = activity
        jobs.register("batch", self.run_job)

    def run(self, projects: list[int | str], steps: list[str], *,
            template: str | None = None, voice: str | None = None,
            allow_over_budget: bool = False, source: Source = "claude") -> dict[str, Any]:
        unknown = sorted(set(steps) - set(STEPS))
        if unknown or not steps:
            raise FvsError(ErrorCode.SPEC_INVALID, f"Unknown or missing steps: {unknown}.",
                           f"Use some of: {', '.join(STEPS)}.",
                           example='{"projects": [3, 4], "steps": ["voiceover", "plan", '
                                   '"render"]}')
        steps = [s for s in STEPS if s in steps]
        rows = [self.projects.get_row(p) for p in dict.fromkeys(projects)]
        to_bill = 0
        if "voiceover" in steps:
            for r in rows:
                try:
                    to_bill += self.voice.generate(r["id"], voice=voice,
                                                   dry_run=True)["chars_to_bill"]
                except FvsError:
                    pass  # that project fails at its voiceover step, with the reason
            budget = self.budget.status()
            if to_bill > budget["left"] and not allow_over_budget:
                raise FvsError(
                    ErrorCode.BUDGET_EXCEEDED,
                    f"This batch needs about {to_bill:,} characters, but only "
                    f"{budget['left']:,} of today's {budget['limit']:,}-character voice "
                    "budget are left.",
                    "Ask the creator whether to go over today's budget; if they agree, call "
                    "again with allow_over_budget=true, or run fewer projects.")
            if to_bill > budget["left"]:
                self.activity.add(f"A batch goes over today's voice budget by about "
                                  f"{to_bill - budget['left']:,} characters, as the creator "
                                  "agreed.", source=source)
        ids = [r["id"] for r in rows]
        payload = {"projects": ids, "steps": steps, "template": template, "voice": voice,
                   "allow_over_budget": allow_over_budget}
        key = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        job = self.jobs.submit("batch", payload, idempotency_key=f"batch:{key}",
                               reuse_done=False)
        nums = ", ".join(f"#{r['number']}" for r in rows)
        summary = (f"Started a batch for {nums}: {', '.join(steps)}"
                   + (f", about {to_bill:,} characters to bill" if to_bill else "") + ".")
        self.activity.add(summary, source=source,
                          tool="fvs_batch_run" if source == "claude" else None)
        return {"job_id": job["id"], "projects": [r["number"] for r in rows],
                "steps": steps, "chars_to_bill": to_bill,
                "summary": summary + " Wait with fvs_job_get."}

    # ---- the job ----------------------------------------------------------------------

    def _project(self, ctx: JobContext, pid: str, lo: float, hi: float,
                 item: dict[str, Any]) -> None:
        p = ctx.payload
        at = lo
        for step in p["steps"]:
            if ctx.cancelled:
                raise JobCancelled()
            width = (hi - lo) * SHARE[step] / sum(SHARE[s] for s in p["steps"])
            item["step"] = step
            sub = StepContext(ctx, project_id=pid, payload={}, start=at, end=at + width)
            sub.progress(0.0, f"#{item['project']}: {step}")
            if step == "voiceover":
                res = self.voice.generate(pid, voice=p["voice"],
                                          allow_over_budget=p["allow_over_budget"], step=sub)
                item["voiceover"] = res["summary"]
            elif step == "plan":
                proj = self.projects.get_row(pid)
                if p["template"] or not proj["spec_v"]:
                    plan = self.planner.propose(pid, template=p["template"])
                    if plan["problems"]:
                        first = plan["problems"][0]
                        raise FvsError(ErrorCode.SPEC_INVALID,
                                       f"The plan for #{item['project']} has "
                                       f"{len(plan['problems'])} problem(s): "
                                       f"{first.get('problem', first)}",
                                       first.get("fix", "Plan it by hand with fvs_edit_plan "
                                                        "and fvs_edit_save."),
                                       details=plan["problems"])
                    item["plan"] = self.specs.save(pid, spec=plan["spec"])["summary"]
                else:
                    item["plan"] = "kept the saved Edit Spec"
            else:
                res = self.renders.render_in(ctx, pid, start=at, end=at + width)
                item["render"] = {k: res[k] for k in ("render_id", "path") if k in res}
                if "qa" in res:
                    item["qa"] = res["qa"]
            at += width
        item.pop("step", None)

    def run_job(self, ctx: JobContext) -> dict[str, Any]:
        ids = ctx.payload["projects"]
        items: list[dict[str, Any]] = []
        for k, pid in enumerate(ids):
            proj = self.projects.get_row(pid)
            item: dict[str, Any] = {"project": proj["number"]}
            try:
                self._project(ctx, pid, k / len(ids), (k + 1) / len(ids), item)
                item["status"] = "done"
                failed, error = 0, None
            except JobCancelled:
                raise
            except FvsError as exc:
                item.update(status="failed", error=exc.to_dict())
                failed, error = 1, json.dumps(exc.to_dict())
                self.activity.add(f"Batch: #{proj['number']} stopped at the "
                                  f"{item.get('step', 'first')} step: {exc.message}",
                                  project_id=pid)
            with self.db.connect() as conn:
                conn.execute("UPDATE projects SET failed = ?, last_error = ?, updated_at = ? "
                             "WHERE id = ?", (failed, error, now_iso(), pid))
            items.append(item)
        done = sum(1 for i in items if i["status"] == "done")
        return {"items": items,
                "summary": f"Batch finished: {done} of {len(items)} project(s) done"
                           + (f", {len(items) - done} failed" if done < len(items) else "")
                           + "."}
