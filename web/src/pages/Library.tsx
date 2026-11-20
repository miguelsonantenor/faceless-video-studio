// Read-only lists of the templates and imported assets Claude can use.
import { useState } from "react";
import { useLive } from "../api";
import { ErrorNote } from "../ui";

interface Templates {
  items: { name: string; version: number; description: string | null }[];
  caption_styles: Record<string, { font?: string; size?: number; position?: string }>;
}

export function Templates() {
  const { data, error } = useLive<Templates>("/api/templates");
  return (
    <main className="narrow">
      <div className="stack-s">
        <h1>Templates</h1>
        <p className="muted" style={{ margin: 0 }}>The looks Claude can pick for a video. Ask
          Claude to use one, or to change one.</p>
      </div>
      <ErrorNote error={error} />
      <section className="card">
        <h2>Templates</h2>
        <ul className="plain" style={{ marginTop: 10 }}>
          {data?.items.map((t) => (
            <li key={t.name}>
              <b style={{ minWidth: 180 }}>{t.name.replace(/_/g, " ")}</b>
              <span className="muted" style={{ flex: 1 }}>{t.description ?? ""}</span>
              <span className="muted">v{t.version}</span>
            </li>
          ))}
        </ul>
      </section>
      <section className="card">
        <h2>Caption styles</h2>
        <ul className="plain" style={{ marginTop: 10 }}>
          {data && Object.entries(data.caption_styles).map(([name, s]) => (
            <li key={name}>
              <b style={{ minWidth: 180 }}>{name.replace(/_/g, " ")}</b>
              <span className="muted">{[s.font, s.size && `${s.size} px`, s.position]
                .filter(Boolean).join(" · ")}</span>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}

interface Assets {
  items: { id: string; kind: string; name: string; duration_s: number | null;
    tags: string[]; license: string | null; source_url: string | null }[];
  summary: string;
}

const KINDS = ["", "music", "sfx", "font", "logo", "image"];

export function Assets() {
  const [kind, setKind] = useState("");
  const { data, error } = useLive<Assets>(`/api/assets${kind ? `?kind=${kind}` : ""}`);
  return (
    <main className="narrow">
      <div className="head">
        <div className="stack-s">
          <h1>Assets</h1>
          <p className="muted" style={{ margin: 0 }}>Music and other files imported for Claude
            to use. Ask Claude to import more.</p>
        </div>
        <label className="small">
          <span className="sr-only">Show kind</span>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map((k) => <option key={k} value={k}>{k || "All kinds"}</option>)}
          </select>
        </label>
      </div>
      <ErrorNote error={error} />
      <section className="card">
        {data && data.items.length === 0 ? <p className="muted">No assets yet.</p> : (
          <ul className="plain">
            {data?.items.map((a) => (
              <li key={a.id}>
                <b style={{ minWidth: 200 }}>{a.name}</b>
                <span className="chip">{a.kind}</span>
                <span className="muted" style={{ flex: 1 }}>
                  {[a.duration_s != null && `${a.duration_s.toFixed(0)} s`, a.tags.join(", ")]
                    .filter(Boolean).join(" · ")}</span>
                <span className="muted small">{a.license ?? "No licence recorded"}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
