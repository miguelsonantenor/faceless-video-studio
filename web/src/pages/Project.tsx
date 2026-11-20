// One project: the render with the safe zone, the storyboard, what Claude checked, the 8
// quality checks, script versions, voice and edit, and the decision (REV-2, REV-3, PRJ-5).
import { useCallback, useState } from "react";
import { api, ApiError, useLive, type ProjectDetail, type SettingsPayload } from "../api";
import { Checks, clock, day, ErrorNote, Link, n, Player, secs, StateChip, Toast } from "../ui";
import { decide, type Decision } from "./decide";

export function Project({ ref_ }: { ref_: string }) {
  const { data, error, reload } = useLive<ProjectDetail>(`/api/projects/${ref_}`);
  const { data: settings } = useLive<SettingsPayload>("/api/settings");
  const [showSafe, setShowSafe] = useState(true);
  const [toast, setToast] = useState<string | null>(null);
  const clearToast = useCallback(() => setToast(null), []);

  if (error) return <main><ErrorNote error={error} /></main>;
  if (!data) return <main><p className="muted">Loading…</p></main>;

  const p = data.project;
  const render = data.renders?.find((r) => r.current) ?? null;
  const s = settings?.settings;
  const safe = s && showSafe ? { top: +s["safe_zone.top"], bottom: +s["safe_zone.bottom"],
    left: +s["safe_zone.left"], right: +s["safe_zone.right"] } : null;
  const clipName = (num: number) =>
    data.clips.find((c) => c.number === num)?.file_name.replace(/\.[^.]+$/, "") ?? `clip ${num}`;

  return (
    <main>
      <nav className="small muted" aria-label="Breadcrumb">
        <Link to="/">Dashboard</Link> / #{p.number} · {data.clips.length === 1 ? "clip " : "clips "}
        {data.clips.map((c) => c.file_name).join(", ")}
      </nav>
      <div className="head" style={{ alignItems: "center" }}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
          <h1>#{p.number} {p.title}</h1>
          <StateChip state={p.state} label={data.state_label} failed={p.failed} />
        </div>
      </div>
      {p.failed && p.last_error && (
        <div className="alert red">
          <b>Last step failed</b>
          <p>{p.last_error.message} {p.last_error.fix}</p>
        </div>
      )}
      {p.stale.length > 0 && (
        <div className="alert">
          <b>Out of date: {p.stale.join(", ")}</b>
          <p>The script or edit changed after these were made. Claude makes them again on its
            next pass.</p>
        </div>
      )}

      <div className="row">
        <div className="stack" style={{ flex: "0 1 360px", gap: 14, minWidth: 280 }}>
          <h2>Render{data.spec ? ` · spec v${data.spec.version}` : ""}</h2>
          <Player renderId={render?.id ?? null} safe={safe}
            empty={p.state === "rendering" ? "Rendering…" : "No render yet."} />
          <label style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <input type="checkbox" checked={showSafe} style={{ minHeight: 0, width: 18, height: 18 }}
              onChange={(e) => setShowSafe(e.target.checked)} />
            Show TikTok safe zone
          </label>
          {render && <span className="small muted">1080×1920 · {secs(render.duration_s)} · the
            file that will be exported</span>}
          <Actions detail={data} onDone={(msg) => { setToast(msg); reload(); }} />
          <div className="buttons small">
            {data.spec && <a href={`/api/projects/${p.number}/spec`} target="_blank"
              rel="noreferrer">Edit Spec v{data.spec.version} (JSON)</a>}
            {data.jobs.slice(0, 3).map((j) => (
              <a key={j.id} href={`/api/jobs/${j.id}/log`} target="_blank" rel="noreferrer">
                {j.kind} log ({j.state})</a>
            ))}
          </div>
        </div>

        <div className="stack" style={{ flex: "1 1 520px", minWidth: 0 }}>
          <Storyboard detail={data} clipName={clipName} />
          {render && <Checked render={render} />}
          {render?.qa && (
            <section className="card">
              <div className="section-head" style={{ marginBottom: 8 }}>
                <h2>Quality checks</h2>
                <span className={`chip ${render.qa.status === "pass" ? "green" : "amber"}`}>
                  {render.qa.checks.filter((c) => c.status === "pass").length} of{" "}
                  {render.qa.checks.length} passed
                </span>
              </div>
              <Checks checks={render.qa.checks} />
            </section>
          )}
          <Script detail={data} onDone={(msg) => { setToast(msg); reload(); }} />
          <VoiceAndEdit detail={data} />
        </div>
      </div>
      <Toast text={toast} onDone={clearToast} />
    </main>
  );
}

function Actions({ detail, onDone }: { detail: ProjectDetail; onDone: (m: string) => void }) {
  const p = detail.project;
  const qa = detail.renders?.find((r) => r.current)?.qa;
  const failedQa = qa?.status === "fail";
  const [note, setNote] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  const act = async (decision: Decision, overrideQa = false) => {
    setBusy(true);
    setError(null);
    try {
      onDone(await decide(p.number, decision, { note, overrideQa }));
      setNote("");
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  };

  if (p.state === "in_review") {
    return (
      <div className="stack-s">
        <label className="field" htmlFor="note">
          {failedQa ? "Note (needed to reject, or to approve despite the failed check)"
            : "Note for Claude if you reject"}
        </label>
        <textarea id="note" rows={3} value={note} onChange={(e) => setNote(e.target.value)}
          placeholder="e.g. the hook is too slow, start on the lightning" />
        <div className="buttons" style={{ marginTop: 6 }}>
          <button disabled={busy || !note.trim()} onClick={() => act("reject")}>
            Reject with note</button>
          {failedQa ? (
            <button disabled={busy || !note.trim()} onClick={() => act("approve", true)}>
              Approve with note</button>
          ) : (
            <button className="primary" disabled={busy} onClick={() => act("approve")}>
              Approve and export</button>
          )}
        </div>
        <ErrorNote error={error} />
      </div>
    );
  }
  if (p.state === "exported" || p.state === "skipped" || p.state === "new") {
    const decision: Decision = p.state === "new" ? "skip" : "reopen";
    return (
      <div className="stack-s">
        <div className="buttons">
          <button disabled={busy} onClick={() => act(decision)}>
            {decision === "skip" ? "Skip this clip" : "Reopen"}</button>
        </div>
        <ErrorNote error={error} />
      </div>
    );
  }
  return <p className="small muted">Claude is working on this project; you can decide once it
    is in review.</p>;
}

function Storyboard({ detail, clipName }: { detail: ProjectDetail;
  clipName: (n: number) => string }) {
  const tl = detail.timeline;
  return (
    <section className="card">
      <h2>Storyboard: which shot plays under each sentence</h2>
      <p>The same storyboard Claude checked before rendering.</p>
      {!tl ? <p className="small muted">Appears once Claude saves an edit.</p>
        : tl.error ? <p className="error">{tl.error.message}</p> : (
          <ul className="board">
            {tl.segments.map((seg) => (
              <li key={seg.index}>
                <img src={`/api/projects/${detail.project.number}/thumbs/${seg.index}.jpg?h=${
                  detail.spec?.spec_hash ?? ""}`} alt="" loading="lazy" />
                <span className="when num">
                  {secs(seg.out_start)} – {secs(seg.out_end)}<br />
                  {clipName(seg.clip)} {secs(seg.src_in)}
                </span>
                <span style={{ minWidth: 0 }}>
                  <span className="tiny muted">{sentenceLabel(seg.sentences)}</span><br />
                  {seg.text}
                </span>
              </li>
            ))}
          </ul>
        )}
    </section>
  );
}

/** Sentence numbers are 1-based, as Claude and the script use them. */
function sentenceLabel(list: number[]) {
  if (list.length === 0) return "No speech";
  if (list.length === 1) return `Sentence ${list[0]}`;
  return `Sentences ${list[0]} to ${list[list.length - 1]}`;
}

function Checked({ render }: { render: NonNullable<ProjectDetail["renders"]>[number] }) {
  const pick = (id: string) => render.qa?.checks.find((c) => c.id === id);
  const facts = [["Voice against the script", pick("voice")], ["Loudness", pick("loudness")],
    ["Ending", pick("tail")]] as const;
  return (
    <section className="card">
      <h2>What Claude checked</h2>
      <p>Frames from the render, and what was heard through the transcript and sound levels.</p>
      {render.contact_sheet && (
        <img className="sheet" src={`/api/renders/${render.id}/sheet.jpg`}
          alt="Frames from the render, in order" />
      )}
      <dl className="facts" style={{ marginTop: 14 }}>
        {facts.map(([label, c]) => c && (
          <div key={label}><dt>{label}</dt><dd>{c.measured}</dd></div>
        ))}
      </dl>
    </section>
  );
}

function Script({ detail, onDone }: { detail: ProjectDetail; onDone: (m: string) => void }) {
  const current = detail.script;
  const [shown, setShown] = useState<number | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  if (!current) {
    return <section className="card"><h2>Script</h2>
      <p>Claude has not written a script yet.</p></section>;
  }
  const other = shown !== null && shown !== current.version
    ? detail.scripts.find((s) => s.version === shown) : undefined;
  const meta = detail.scripts.find((s) => s.version === current.version);

  const restore = async (version: number) => {
    setError(null);
    try {
      const r = await api<{ summary: string }>(`/api/projects/${detail.project.number}/restore`,
        { method: "POST", body: { script_version: version } });
      setShown(null);
      onDone(r.summary);
    } catch (e) {
      setError(e as ApiError);
    }
  };

  return (
    <section className="card">
      <div className="section-head" style={{ marginBottom: 12 }}>
        <h2>Script · version {other ? other.version : current.version} of{" "}
          {detail.scripts.length}</h2>
        {detail.scripts.length > 1 && (
          <label className="small">
            <span className="sr-only">Show script version</span>
            <select value={shown ?? current.version}
              onChange={(e) => setShown(Number(e.target.value))}>
              {detail.scripts.map((s) => (
                <option key={s.version} value={s.version}>
                  Version {s.version}{s.version === current.version ? " (current)" : ""}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {other ? (
        <div className="stack-s">
          <dl className="pairs">
            <dt>Hook</dt><dd><b>{other.hook ?? "—"}</b></dd>
            <dt>Voiceover</dt><dd>{other.voiceover_text}</dd>
            <dt>Written by</dt><dd>{other.author}, {day(other.created_at)}{" "}
              {clock(other.created_at)} · about {other.est_seconds.toFixed(0)} s</dd>
          </dl>
          <div className="buttons" style={{ marginTop: 10 }}>
            <button onClick={() => restore(other.version)}>Restore version {other.version}</button>
          </div>
          <span className="small muted">Restoring makes it current again; the video needs a new
            render afterwards.</span>
          <ErrorNote error={error} />
        </div>
      ) : (
        <dl className="pairs">
          <dt>Hook</dt><dd><b>{current.hook ?? "—"}</b></dd>
          <dt>Sentences</dt>
          <dd><ol style={{ margin: 0, paddingLeft: 20 }}>
            {current.sentences.map((s) => <li key={s.i}>{s.caption}</li>)}
          </ol></dd>
          <dt>Post caption</dt><dd>{current.post_caption ?? "—"}</dd>
          <dt>Hashtags</dt><dd style={{ color: "var(--accent)" }}>
            {current.hashtags.map((h) => `#${h.replace(/^#/, "")}`).join(" ") || "—"}</dd>
          {meta && <><dt>Written by</dt><dd>{meta.author}, {clock(meta.created_at)} · about{" "}
            {current.est_seconds.toFixed(0)} s</dd></>}
        </dl>
      )}
    </section>
  );
}

interface EditSpec {
  template?: string;
  reframe?: { mode: string };
  captions?: { mode: string; position: string };
  audio?: { music?: { asset: string; start_at: number; gain_db: number } | null };
}

function VoiceAndEdit({ detail }: { detail: ProjectDetail }) {
  const vo = detail.voiceover;
  const spec = detail.spec?.spec as EditSpec | undefined;
  const words = (s: string | undefined) => (s ? s.replace(/_/g, " ") : "—");
  const music = spec?.audio?.music;
  return (
    <section className="card">
      <h2>Voice and edit</h2>
      <dl className="facts" style={{ marginTop: 12 }}>
        <div><dt>Voice</dt><dd>{vo ? `${vo.voice_alias} · ${vo.duration_s.toFixed(1)} s`
          : "Not made yet"}</dd></div>
        <div><dt>Characters</dt><dd>{vo ? `${n(vo.chars_billed)} billed · ${n(vo.chars_reused)}`
          + " reused" : "—"}</dd></div>
        <div><dt>Template</dt><dd>{words(spec?.template ?? detail.project.template
          ?? undefined)}</dd></div>
        <div><dt>Reframe</dt><dd>{words(spec?.reframe?.mode)}</dd></div>
        <div><dt>Captions</dt><dd>{spec?.captions ? `${words(spec.captions.mode)}, ${
          words(spec.captions.position)}` : "—"}</dd></div>
        <div><dt>Music</dt><dd>{music ? `${music.asset} from ${secs(music.start_at)}, `
          + `${music.gain_db} dB` : "None"}</dd></div>
        <div><dt>Edit</dt><dd>{detail.spec ? `Spec v${detail.spec.version} of ${
          detail.specs.length}` : "Not planned yet"}</dd></div>
      </dl>
      <p className="small muted" style={{ marginTop: 14 }}>Want something changed? Ask Claude in
        chat, for example "put the lava shot under sentence 3 on #{detail.project.number}".</p>
    </section>
  );
}
