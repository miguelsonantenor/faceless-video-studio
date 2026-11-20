// Every render waiting for the creator, side by side (REV-3). Approving writes the export
// package from this exact file; rejecting sends the note to Claude.
import { useCallback, useState } from "react";
import { api, ApiError, useLive, type ProjectRow } from "../api";
import { ErrorNote, Link, Player, Toast } from "../ui";
import { decide } from "./decide";

const QA_CHECKS = 8;

export function ReviewQueue() {
  const { data, error, reload } = useLive<{ items: ProjectRow[] }>(
    "/api/projects?state=in_review");
  const [toast, setToast] = useState<string | null>(null);
  const [batchError, setBatchError] = useState<ApiError | null>(null);
  const clearToast = useCallback(() => setToast(null), []);
  const rows = data?.items ?? [];
  const passed = rows.filter((r) => r.qa?.status === "pass").length;

  const done = (msg: string) => { setToast(msg); reload(); };
  const approveAll = async () => {
    setBatchError(null);
    try {
      done((await api<{ summary: string }>("/api/review/approve-passed",
        { method: "POST" })).summary);
    } catch (e) {
      setBatchError(e as ApiError);
    }
  };

  return (
    <main>
      <div className="stack-s">
        <h1>Review queue · {rows.length} waiting</h1>
        <p className="muted" style={{ margin: 0 }}>Claude has already checked each of these.
          Approving writes the export package from this exact file. Rejecting sends your note
          to Claude.</p>
      </div>
      {passed > 0 && (
        <div>
          <button className="primary" onClick={approveAll}>
            Approve the {passed === 1 ? "1 that" : `${passed} that`} passed every check</button>
          <ErrorNote error={batchError} />
        </div>
      )}
      <ErrorNote error={error} />
      {data && rows.length === 0 && (
        <p className="muted">Nothing to review. Finished videos appear here after Claude has
          checked them.</p>
      )}
      <div className="cards">
        {rows.map((r) => <Card key={r.id} row={r} onDone={done} />)}
      </div>
      <Toast text={toast} onDone={clearToast} />
    </main>
  );
}

function Card({ row, onDone }: { row: ProjectRow; onDone: (m: string) => void }) {
  const [mode, setMode] = useState<"reject" | "override" | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const failed = row.qa?.status === "fail";

  const act = async (decision: "approve" | "reject", overrideQa = false) => {
    setBusy(true);
    setError(null);
    try {
      onDone(await decide(row.number, decision, { note, overrideQa }));
    } catch (e) {
      setError(e as ApiError);
      setBusy(false);
    }
  };

  return (
    <article className="review" aria-label={`#${row.number} ${row.title}`}>
      <Player renderId={row.render_id} />
      <div>
        <Link to={`/projects/${row.number}`} className="title">
          <b>#{row.number} {row.title}</b></Link>
        <div className="small muted">
          {[row.template?.replace(/_/g, " "), row.length,
            `${row.clips.length} clip${row.clips.length === 1 ? "" : "s"}`].filter(Boolean)
            .join(" · ")}</div>
      </div>
      {row.qa && (
        <div className={`qa-box ${row.qa.status === "pass" ? "" : "amber"}`}>
          {row.qa.status === "pass" ? `Passed all ${QA_CHECKS} checks`
            : failed ? `${QA_CHECKS - row.qa.failed.length} passed, ${row.qa.failed.length} failed: `
              + row.qa.failed.map((c) => `${c.name.toLowerCase()} ${c.measured}`).join("; ")
              + ". Approving needs a note."
              : row.qa.summary}
        </div>
      )}
      {mode && (
        <div className="stack-s">
          <label className="field" htmlFor={`note-${row.id}`}>
            {mode === "reject" ? "What should Claude change?" : "Why is the failed check fine?"}
          </label>
          <textarea id={`note-${row.id}`} rows={3} value={note} autoFocus
            onChange={(e) => setNote(e.target.value)} />
        </div>
      )}
      <div className="buttons" style={{ display: "grid", gridTemplateColumns: "1fr 1fr" }}>
        {mode ? (
          <>
            <button disabled={busy} onClick={() => { setMode(null); setNote(""); }}>
              Cancel</button>
            <button className="primary" disabled={busy || !note.trim()}
              onClick={() => act(mode === "reject" ? "reject" : "approve", mode === "override")}>
              {mode === "reject" ? "Send to Claude" : "Approve"}</button>
          </>
        ) : (
          <>
            <button disabled={busy} onClick={() => setMode("reject")}>Reject…</button>
            {failed ? (
              <button disabled={busy} onClick={() => setMode("override")}>
                Approve with note…</button>
            ) : (
              <button className="primary" disabled={busy} onClick={() => act("approve")}>
                Approve</button>
            )}
          </>
        )}
      </div>
      <ErrorNote error={error} />
    </article>
  );
}
