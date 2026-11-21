// What is happening: counts by state, every project, Claude's activity, running jobs,
// and what needs the creator (REV-1, REV-2, REV-5).
import { useLive, type Overview, type ProjectRow, type State } from "../api";
import { clock, day, ErrorNote, Link, n, OpenFolder, StateChip } from "../ui";

const TONE: Partial<Record<State, string>> = {
  rendering: "blue", claude_check: "blue", in_review: "amber", changes_requested: "amber",
  exported: "green",
};

export function Dashboard({ overview }: { overview: Overview | null }) {
  const { data, error } = useLive<{ items: ProjectRow[] }>("/api/projects");
  const rows = data?.items ?? [];
  const byId = new Map(rows.map((r) => [r.id, r]));
  const attention = rows.filter((r) => r.failed || r.state === "changes_requested");
  const total = overview ? Object.values(overview.counts).reduce((a, b) => a + b, 0) : 0;

  return (
    <main>
      <div className="head">
        <div className="stack-s">
          <span className="muted small">
            {overview?.activity[0] ? `Last change ${clock(overview.activity[0].at)}, `
              + `${day(overview.activity[0].at)}` : "Nothing has happened yet"}
          </span>
          <h1>All projects · {total}</h1>
          <div className="buttons">
            <OpenFolder folder="input">Open input folder</OpenFolder>
            <OpenFolder folder="output">Open output folder</OpenFolder>
          </div>
        </div>
        {overview && <BudgetCard overview={overview} />}
      </div>

      {overview && (
        <section className="counters" aria-label="Projects by state">
          {(Object.keys(overview.labels) as State[]).map((s) => (
            <div key={s} className={`counter ${TONE[s] ?? ""}`}>
              <b className="num">{overview.counts[s]}</b>
              <span>{overview.labels[s]}</span>
            </div>
          ))}
        </section>
      )}

      <div className="row">
        <section className="card flush" style={{ flex: "1 1 560px", minWidth: 0 }}>
          <div className="card-head">
            <h2>Projects</h2>
            <span className="muted small">
              {rows.length ? `Showing ${rows.length} · latest activity first` : ""}
            </span>
          </div>
          <ErrorNote error={error} />
          {data && rows.length === 0 ? (
            <p className="muted" style={{ padding: "16px 20px" }}>
              No projects yet. Put clips in the input folder and ask Claude to make videos
              from them.
            </p>
          ) : (
            <div className="scroll-x">
              <table>
                <thead>
                  <tr><th scope="col">#</th><th scope="col">Video</th><th scope="col">State</th>
                    <th scope="col">Where it stands</th><th scope="col">Length</th></tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td className="num">#{r.number}</td>
                      <td>
                        <Link to={`/projects/${r.number}`} className="title">{r.title}</Link>
                        <div className="tiny muted">
                          {r.clips[0] ?? ""}{r.clips.length > 1 ? ` + ${r.clips.length - 1} more`
                            : ""}
                        </div>
                      </td>
                      <td>
                        <StateChip state={r.state} failed={r.failed}
                          label={overview?.labels[r.state] ?? r.state} />
                      </td>
                      <td>{r.where}</td>
                      <td className="num">{r.length ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <aside className="stack" style={{ flex: "0 1 320px", minWidth: 280 }}>
          <section className="card">
            <h2>What Claude is doing</h2>
            <p>Every tool call Claude makes, in plain words. Updates live.</p>
            {overview?.activity.length ? (
              <ul className="feed">
                {overview.activity.slice(0, 12).map((a) => (
                  <li key={a.id}><time dateTime={a.at}>{clock(a.at)}</time>
                    <span>{a.summary}</span></li>
                ))}
              </ul>
            ) : <p className="muted small">Nothing yet.</p>}
          </section>

          <section className="card">
            <h2>Running now</h2>
            {overview?.jobs.length ? (
              <ul className="plain">
                {overview.jobs.map((j) => {
                  const p = j.project_id ? byId.get(j.project_id) : undefined;
                  const pct = Math.round(100 * (j.progress ?? 0));
                  return (
                    <li key={j.id} style={{ display: "block" }}>
                      <div className="section-head small">
                        <b>{j.kind[0].toUpperCase() + j.kind.slice(1)}{p ? ` · #${p.number}`
                          : ""}</b>
                        <span className="muted num">
                          {j.state === "queued" ? "Queued" : j.progress_note ?? `${pct}%`}
                        </span>
                      </div>
                      <div className="bar thin" role="progressbar" aria-valuenow={pct}
                        aria-valuemin={0} aria-valuemax={100}
                        aria-label={`${j.kind} progress`}>
                        <div style={{ width: `${pct}%` }} />
                      </div>
                    </li>
                  );
                })}
              </ul>
            ) : <p className="muted small" style={{ marginTop: 8 }}>Nothing running.</p>}
          </section>

          {attention.length > 0 && (
            <section className="card stack" style={{ gap: 12 }}>
              <h2>Needs attention</h2>
              {attention.map((r) => (
                <div key={r.id} className={`alert ${r.failed ? "red" : ""}`}>
                  <b>#{r.number} {r.failed ? "failed" : "changes requested"}</b>
                  <p>{r.failed ? r.where : `Your note: "${r.note ?? ""}". Claude will pick it`
                    + " up on its next pass."}</p>
                  <Link to={`/projects/${r.number}`}>Open project</Link>
                </div>
              ))}
            </section>
          )}
        </aside>
      </div>
    </main>
  );
}

function BudgetCard({ overview }: { overview: Overview }) {
  const { budget, quota } = overview;
  const pct = budget.limit ? Math.min(100, (100 * budget.spent_today) / budget.limit) : 100;
  return (
    <section className="budget" aria-label="Voice budget">
      <div className="line">
        <span className="muted">Voice budget today</span>
        <b className="num">{n(budget.spent_today)} of {n(budget.limit)} characters</b>
      </div>
      <div className="bar" role="progressbar" aria-valuenow={Math.round(pct)}
        aria-valuemin={0} aria-valuemax={100} aria-label="Voice budget used">
        <div style={{ width: `${pct}%` }} />
      </div>
      <div className="tiny muted" style={{ marginTop: 8 }}>
        {quota.available ? `ElevenLabs: ${n(quota.left ?? 0)} characters left this period`
          + (quota.resets_at ? ` · resets ${day(quota.resets_at)}` : "")
          : `ElevenLabs: ${quota.reason ?? "not connected"}`}
      </div>
    </section>
  );
}
