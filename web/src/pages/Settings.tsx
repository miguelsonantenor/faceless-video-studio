// Settings (REV-4): the ElevenLabs key and budget, voices, connecting Claude, folders,
// export, video defaults and the system check. The key is never shown in full.
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, ONCE, useLive, type Overview, type SettingsPayload } from "../api";
import { day, ErrorNote, Mark, n, OpenFolder, Toast } from "../ui";

type Values = Record<string, string>;

interface Profile {
  alias: string; voice_id: string; model_id: string; speed: number; language: string;
  is_default: boolean; stability?: number; similarity?: number; style?: number;
  words_per_minute?: number;
}

export function Settings({ overview }: { overview: Overview | null }) {
  const { data, error, reload } = useLive<SettingsPayload>("/api/settings", ["settings_changed"]);
  const [toast, setToast] = useState<string | null>(null);
  const clearToast = useCallback(() => setToast(null), []);
  const saved = (msg: string) => { setToast(msg); reload(); };

  return (
    <main className="narrow">
      <h1>Settings</h1>
      <ErrorNote error={error} />
      {data && (
        <>
          <ElevenLabs data={data} overview={overview} onSaved={saved} />
          <ConnectClaude data={data} onSaved={saved} />
          <Folders data={data} onSaved={saved} />
          <InspirationSets onSaved={saved} />
          <VideoDefaults data={data} onSaved={saved} />
          <Performance data={data} onSaved={saved} />
          <SystemCheck />
        </>
      )}
      <Toast text={toast} onDone={clearToast} />
    </main>
  );
}

/** Run an action, keeping its error and busy state for the form that started it. */
function useAction() {
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return { error, busy, run };
}

function copy(text: string, onSaved: (m: string) => void) {
  navigator.clipboard.writeText(text).then(() => onSaved("Copied."),
    () => onSaved("Could not copy; select the text and copy it by hand."));
}

// ---- ElevenLabs ---------------------------------------------------------------------------

function ElevenLabs({ data, overview, onSaved }: { data: SettingsPayload;
  overview: Overview | null; onSaved: (m: string) => void }) {
  const [key, setKey] = useState("");
  const [budget, setBudget] = useState(String(data.settings["voice.daily_budget_chars"]));
  const [reset, setReset] = useState(String(data.settings["voice.budget_reset_time"]));
  const keyAction = useAction();
  const budgetAction = useAction();
  const quota = overview?.quota;

  const saveKey = (e: FormEvent) => {
    e.preventDefault();
    keyAction.run(async () => {
      const r = await api<{ key_last4: string }>("/api/key", { method: "PUT", body: { key } });
      setKey("");
      onSaved(`Key saved and tested (ends in ${r.key_last4}).`);
    });
  };
  const saveBudget = (e: FormEvent) => {
    e.preventDefault();
    budgetAction.run(async () => {
      await api("/api/settings", { method: "PATCH", body: {
        "voice.daily_budget_chars": Number(budget), "voice.budget_reset_time": reset } });
      onSaved("Voice budget saved.");
    });
  };

  return (
    <section className="card pad-l" aria-labelledby="el-h">
      <h2 id="el-h">ElevenLabs</h2>
      <div className="stack-s">
        <b>API key</b>
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <span className="well">{data.elevenlabs.configured
            ? `••••••••••••${data.elevenlabs.key_last4}` : "No key saved"}</span>
          {data.elevenlabs.configured && quota?.available && (
            <span className="ok"><Mark status="ok" /> Key works</span>)}
          {data.elevenlabs.configured && quota && !quota.available && (
            <span className="error">{quota.reason}</span>)}
        </div>
      </div>
      <form className="stack-s" onSubmit={saveKey}>
        <label htmlFor="key" className="small muted">Replace key: paste a new one from your
          ElevenLabs account page</label>
        <div style={{ display: "flex", gap: 10 }}>
          <input id="key" className="mono" type="password" autoComplete="off" value={key}
            onChange={(e) => setKey(e.target.value)} placeholder="Paste a new key"
            style={{ flex: 1, minWidth: 0 }} />
          <button className="primary" disabled={keyAction.busy || !key.trim()}>
            {keyAction.busy ? "Testing…" : "Save and test"}</button>
        </div>
        <span className="tiny muted">Saved only on this computer, in ~/.fvs/config.toml. FVS
          never shows the full key again and never gives it to Claude.</span>
        <ErrorNote error={keyAction.error} />
      </form>
      <form className="grid-fields" onSubmit={saveBudget}>
        <label className="field">Daily budget (characters)
          <input type="number" min={0} value={budget} onChange={(e) => setBudget(e.target.value)} />
        </label>
        <label className="field">Budget resets at
          <input type="time" value={reset} onChange={(e) => setReset(e.target.value)} />
        </label>
        <div className="stack-s">
          <b>ElevenLabs plan</b>
          <span className="muted" style={{ paddingTop: 10 }}>
            {quota?.available ? `${n(quota.left ?? 0)} characters left`
              + (quota.resets_at ? ` · resets ${day(quota.resets_at)}` : "")
              : "Not available until a key works"}</span>
        </div>
        <div><button disabled={budgetAction.busy}>Save budget</button>
          <ErrorNote error={budgetAction.error} /></div>
      </form>
      <Voices onSaved={onSaved} />
    </section>
  );
}

interface Model { model_id: string; name: string; description: string; languages: number }

/** The speech models the ElevenLabs account offers (v2, v3 and any newer one). */
function ModelSelect({ value, onChange, models, id }: { value: string;
  onChange: (v: string) => void; models: Model[] | null; id?: string }) {
  const known = models?.some((m) => m.model_id === value);
  return (
    <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
      {!known && <option value={value}>{value}</option>}
      {models?.map((m) => (
        <option key={m.model_id} value={m.model_id}>{m.name}</option>
      ))}
    </select>
  );
}

function Voices({ onSaved }: { onSaved: (m: string) => void }) {
  const { data, reload } = useLive<{ items: Profile[] }>("/api/voices");
  const { data: models, error: modelsError } = useLive<{ items: Model[] }>("/api/voice-models", ONCE);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ alias: "", voice_id: "", model_id: "eleven_multilingual_v2",
    speed: "1.0", language: "en" });
  const action = useAction();
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm({ ...form, [k]: e.target.value });

  // The whole profile goes back, so nothing (speed, words a minute) falls back to a default.
  const save = (p: Profile, changes: Partial<Profile>, makeDefault: boolean, msg: string) =>
    action.run(async () => {
      const { alias, is_default: _d, ...rest } = { ...p, ...changes };
      await api(`/api/voices/${encodeURIComponent(alias)}?make_default=${makeDefault}`,
        { method: "PUT", body: rest });
      reload();
      onSaved(msg);
    });

  const add = (e: FormEvent) => {
    e.preventDefault();
    action.run(async () => {
      await api(`/api/voices/${encodeURIComponent(form.alias)}?make_default=false`,
        { method: "PUT", body: { voice_id: form.voice_id, model_id: form.model_id,
          speed: Number(form.speed), language: form.language } });
      reload();
      onSaved(`Voice profile ${form.alias} saved.`);
      setAdding(false);
    });
  };

  return (
    <div className="divide">
      <div className="section-head">
        <b>Voice profiles</b>
        <button onClick={() => setAdding(!adding)}>{adding ? "Cancel" : "Add voice profile"}</button>
      </div>
      {modelsError && <p className="small muted">The model list needs a working key; you can
        still type a model ID by adding a profile.</p>}
      {adding && (
        <form className="grid-fields" onSubmit={add}>
          <label className="field">Name (lowercase, no spaces)
            <input required pattern="[a-z0-9_\-]{1,40}" value={form.alias} onChange={set("alias")} />
          </label>
          <label className="field">ElevenLabs voice ID
            <input required className="mono" value={form.voice_id} onChange={set("voice_id")} />
          </label>
          <label className="field">Model
            <ModelSelect value={form.model_id} models={models?.items ?? null}
              onChange={(v) => setForm({ ...form, model_id: v })} />
          </label>
          <label className="field">Speed (0.7 to 1.2)
            <input type="number" min={0.7} max={1.2} step={0.05} value={form.speed}
              onChange={set("speed")} />
          </label>
          <label className="field">Language
            <input required value={form.language} onChange={set("language")} />
          </label>
          <div><button className="primary" disabled={action.busy}>Save profile</button></div>
        </form>
      )}
      <ErrorNote error={action.error} />
      <ul className="plain">
        {data?.items.map((p) => (
          <li key={p.alias} style={{ alignItems: "center", flexWrap: "wrap", gap: 10 }}>
            <b style={{ minWidth: 120 }}>{p.alias}</b>
            <label className="small muted" style={{ display: "flex", alignItems: "center",
              gap: 8 }}>Model
              <ModelSelect value={p.model_id} models={models?.items ?? null}
                onChange={(v) => save(p, { model_id: v }, false,
                  `${p.alias} now uses ${models?.items.find((m) => m.model_id === v)?.name ?? v}.`)} />
            </label>
            <span className="muted" style={{ flex: 1 }}>{p.language} · speed {p.speed.toFixed(2)}</span>
            {p.is_default ? <span className="muted">Default</span> : (
              <button onClick={() => save(p, {}, true, `${p.alias} is now the default.`)}>
                Make default</button>
            )}
          </li>
        ))}
      </ul>
      <span className="tiny muted">Eleven v3 is the most expressive; it does not stitch
        sentences together, so each sentence is made on its own.</span>
    </div>
  );
}

// ---- Connect Claude ---------------------------------------------------------------------

interface Desktop {
  path: string; installed: boolean; config: unknown; mcp_address: string; claude_code: string;
}

function ConnectClaude({ data, onSaved }: { data: SettingsPayload;
  onSaved: (m: string) => void }) {
  const { data: desktop, reload } = useLive<Desktop>("/api/claude-desktop", ONCE);
  const action = useAction();
  const config = desktop ? JSON.stringify(desktop.config, null, 2) : "";

  const install = () => action.run(async () => {
    await api("/api/claude-desktop", { method: "POST" });
    reload();
    onSaved("Added to Claude Desktop. Restart Claude Desktop to use FVS.");
  });
  const copyToken = () => action.run(async () => {
    copy((await api<{ token: string }>("/api/token")).token, onSaved);
  });

  return (
    <section className="card pad-l" aria-labelledby="cc-h">
      <div className="section-head">
        <h2 id="cc-h">Connect Claude</h2>
        {desktop?.installed && <span className="chip green">In Claude Desktop's config</span>}
      </div>
      <div className="stack-s">
        <b>Claude Desktop</b>
        <span className="small muted">One click adds FVS to Claude Desktop's config file
          ({desktop?.path ?? "…"}). Restart Claude Desktop afterwards. Or copy the config and
          paste it in yourself.</span>
        <pre className="well">{config}</pre>
        <div className="buttons" style={{ marginTop: 6 }}>
          <button className="primary" disabled={action.busy} onClick={install}>
            {desktop?.installed ? "Add again" : "Add to Claude Desktop"}</button>
          <button onClick={() => copy(config, onSaved)}>Copy config</button>
        </div>
      </div>
      <div className="divide">
        <b>Local MCP address, for Claude Code and other MCP apps on this computer</b>
        <div style={{ display: "grid", gridTemplateColumns: "80px 1fr auto", gap: 10,
          alignItems: "center" }}>
          <span className="small muted">Address</span>
          <span className="well">{data.mcp.address}</span>
          <button onClick={() => copy(data.mcp.address, onSaved)}>Copy</button>
          <span className="small muted">Token</span>
          <span className="well">••••••••••••••••{data.mcp.token_last4}</span>
          <button onClick={copyToken}>Copy</button>
        </div>
        {desktop && <pre className="well">{desktop.claude_code}</pre>}
        <span className="tiny muted">Only programs on this computer that have the token can
          connect. Web pages are blocked.</span>
        <ErrorNote error={action.error} />
      </div>
    </section>
  );
}

// ---- folders and export -----------------------------------------------------------------

function Folders({ data, onSaved }: { data: SettingsPayload; onSaved: (m: string) => void }) {
  const [output, setOutput] = useState(data.folders.output);
  const [dest, setDest] = useState(String(data.settings["export.destination"]));
  const action = useAction();

  const save = (e: FormEvent) => {
    e.preventDefault();
    action.run(async () => {
      if (output !== data.folders.output) {
        await api("/api/folders", { method: "PUT", body: { output } });
      }
      await api("/api/settings", { method: "PATCH", body: { "export.destination": dest } });
      onSaved(output !== data.folders.output
        ? "Saved. The new output folder is used after FVS restarts." : "Saved.");
    });
  };

  return (
    <form className="card pad-l" aria-labelledby="f-h" onSubmit={save}>
      <h2 id="f-h">Folders and export</h2>
      <div className="folder-row">
        <label className="field">
          <span>Input folder <span className="muted" style={{ fontWeight: 400 }}>(your clips;
            read only, FVS never changes it)</span></span>
          <input className="mono" value={data.folders.input} readOnly />
        </label>
        <OpenFolder folder="input">Open</OpenFolder>
      </div>
      <div className="folder-row">
        <label className="field">Output folder
          <input className="mono" value={output} onChange={(e) => setOutput(e.target.value)} />
        </label>
        <OpenFolder folder="output">Open</OpenFolder>
      </div>
      <div className="folder-row">
        <label className="field">
          <span>Inspiration folder <span className="muted" style={{ fontWeight: 400 }}>(example
            videos for Claude to study; read only)</span></span>
          <input className="mono" value={data.folders.inspiration} readOnly />
        </label>
        <OpenFolder folder="inspiration">Open</OpenFolder>
      </div>
      <fieldset>
        <legend>When a video is approved</legend>
        <label style={{ display: "flex", gap: 8, alignItems: "center", minHeight: 32 }}>
          <input type="radio" name="dest" style={{ minHeight: 0 }} checked={dest === "folder"}
            onChange={() => setDest("folder")} />
          Write the package to the output folder
        </label>
        <label className="muted" style={{ display: "flex", gap: 8, alignItems: "center",
          minHeight: 32 }}>
          <input type="radio" name="dest" style={{ minHeight: 0 }} disabled />
          Also upload a TikTok draft (coming later)
        </label>
      </fieldset>
      <div><button disabled={action.busy}>Save</button></div>
      <ErrorNote error={action.error} />
    </form>
  );
}

// ---- inspiration sets ---------------------------------------------------------------------

interface InspirationVideo { number: number; file_name: string; content_hash: string;
  note: string }
interface InspirationSet { name: string; folder: string; videos: InspirationVideo[] }

function InspirationSets({ onSaved }: { onSaved: (m: string) => void }) {
  const { data, error, reload } = useLive<{ folder: string; default: string;
    sets: InspirationSet[] }>("/api/inspiration", ONCE);
  const action = useAction();
  const sets = data?.sets.filter((s) => s.videos.length) ?? [];

  const choose = (name: string) => action.run(async () => {
    await api("/api/settings", { method: "PATCH", body: { "defaults.inspiration_set": name } });
    reload();
    onSaved(name ? `Claude now follows the ${name} examples.` : "No inspiration set chosen.");
  });
  const note = (set: string, v: InspirationVideo, text: string) => {
    if (text === v.note) return;
    action.run(async () => {
      await api(`/api/inspiration/${encodeURIComponent(set)}/videos/${v.number}/note`,
        { method: "PUT", body: { note: text } });
      reload();
      onSaved("Note saved.");
    });
  };

  return (
    <section className="card pad-l" aria-labelledby="in-h">
      <div className="section-head">
        <h2 id="in-h">Inspiration</h2>
        <div className="buttons">
          <button onClick={reload}>Look again</button>
          <OpenFolder folder="inspiration">Open inspiration folder</OpenFolder>
        </div>
      </div>
      <p className="small muted">Put videos whose style you want in the inspiration folder,
        one subfolder per style (for example <span className="mono">perfume</span>). Claude
        measures their length, pace, hooks and sound, looks at their frames, and makes your
        videos the same way. Their footage is never used.</p>
      <ErrorNote error={error ?? action.error} />
      {data && (
        <label className="field" style={{ maxWidth: 360 }}>Claude follows
          <select value={data.default} onChange={(e) => choose(e.target.value)}>
            <option value="">No set</option>
            {sets.map((s) => <option key={s.name} value={s.name}>
              {s.name} ({s.videos.length} video{s.videos.length === 1 ? "" : "s"})</option>)}
          </select>
        </label>
      )}
      {data && !sets.length && <p className="muted small">No example videos yet in
        {" "}<span className="mono">{data.folder}</span>.</p>}
      {sets.map((s) => (
        <div key={s.name} className="divide stack-s">
          <b>{s.name}</b>
          <div className="insp-videos">
            {s.videos.map((v) => (
              <div key={v.content_hash} className="insp-video">
                <img alt="" loading="lazy" src={`/api/inspiration/${encodeURIComponent(s.name)}`
                  + `/thumbs/${v.content_hash}.jpg`} />
                <label className="field small">{v.number}. {v.file_name}
                  <input defaultValue={v.note} placeholder="What to take from it, e.g. the hook"
                    onBlur={(e) => note(s.name, v, e.target.value.trim())} />
                </label>
              </div>
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}

// ---- performance --------------------------------------------------------------------------

interface Hardware { gpu_encoder: string | null; whisper: string; device: "gpu" | "cpu" }

function Performance({ data, onSaved }: { data: SettingsPayload;
  onSaved: (m: string) => void }) {
  const { data: hw, reload } = useLive<Hardware>("/api/hardware", ["settings_changed"]);
  const action = useAction();
  const device = String(data.settings["performance.device"]);
  const pick = (value: string) => action.run(async () => {
    await api("/api/settings", { method: "PATCH", body: { "performance.device": value } });
    reload();
    onSaved(value === "gpu" ? "Renders and speech recognition use the GPU."
      : "Renders and speech recognition use the CPU only.");
  });
  const option = (value: string, label: string, detail: string) => (
    <label style={{ display: "flex", gap: 8, alignItems: "flex-start", minHeight: 32 }}>
      <input type="radio" name="device" style={{ minHeight: 0, marginTop: 4 }}
        checked={device === value} disabled={action.busy} onChange={() => pick(value)} />
      <span>{label}<br /><span className="small muted">{detail}</span></span>
    </label>
  );
  const gpuDetail = !hw ? "Checking the GPU…"
    : (hw.gpu_encoder ? `Renders encode on the GPU (${hw.gpu_encoder})`
      : "No working GPU encoder was found, so renders still encode on the CPU")
      + (hw.device === "gpu" ? `; speech recognition runs on the ${hw.whisper}.` : ".");
  return (
    <section className="card pad-l" aria-labelledby="pf-h">
      <h2 id="pf-h">Performance</h2>
      <fieldset>
        <legend>Where heavy work runs</legend>
        {option("gpu", "GPU (recommended)", gpuDetail)}
        {option("cpu", "CPU only", "Slower. Use it if the GPU is busy with something else "
          + "or renders fail on it.")}
      </fieldset>
      <ErrorNote error={action.error} />
    </section>
  );
}

// ---- video defaults -----------------------------------------------------------------------

const DEFAULT_KEYS = ["defaults.template", "video.frame_rate", "video.target_min_seconds",
  "video.target_max_seconds", "video.max_seconds", "render.renders_at_once",
  "safe_zone.top", "safe_zone.bottom", "safe_zone.left", "safe_zone.right"];

function VideoDefaults({ data, onSaved }: { data: SettingsPayload;
  onSaved: (m: string) => void }) {
  const initial = () => Object.fromEntries(DEFAULT_KEYS.map((k) => [k,
    String(data.settings[k])]));
  const [v, setV] = useState<Values>(initial);
  const { data: templates } = useLive<{ items: { name: string; description: string }[] }>(
    "/api/templates", ONCE);
  const action = useAction();
  // Live events reload the settings often; only a real change to these values (say, Claude
  // changed one) replaces what the creator is typing.
  const stored = DEFAULT_KEYS.map((k) => data.settings[k]).join("|");
  useEffect(() => setV(initial()), [stored]);

  const min = Number(v["video.target_min_seconds"]);
  const max = Number(v["video.target_max_seconds"]);
  const hard = Number(v["video.max_seconds"]);
  const problem = min > max ? "The shortest target must not be longer than the longest."
    : max > hard ? "The target must fit inside the maximum length." : null;
  const set = (k: string) => (e: { target: { value: string } }) => setV({ ...v, [k]: e.target.value });

  const save = (e: FormEvent) => {
    e.preventDefault();
    action.run(async () => {
      const body = Object.fromEntries(DEFAULT_KEYS.map((k) => [k,
        k === "defaults.template" ? v[k] : Number(v[k])]));
      await api("/api/settings", { method: "PATCH", body });
      onSaved("Saved. Applies to new scripts and plans; existing versions keep their values.");
    });
  };

  const num = (k: string, label: string, extra: Record<string, number> = {}) => (
    <label className="field">{label}
      <input type="number" value={v[k]} onChange={set(k)} {...extra}
        aria-invalid={problem && k.startsWith("video.") ? true : undefined} />
    </label>
  );

  return (
    <form className="card pad-l" aria-labelledby="vd-h" onSubmit={save}>
      <h2 id="vd-h">Video defaults</h2>
      <div className="grid-fields">
        <label className="field">Template
          <select value={v["defaults.template"]} onChange={set("defaults.template")}>
            {(templates?.items ?? [{ name: v["defaults.template"], description: "" }]).map((t) => (
              <option key={t.name} value={t.name}>{t.name.replace(/_/g, " ")}</option>
            ))}
          </select>
        </label>
        <label className="field">Frame rate
          <select value={v["video.frame_rate"]} onChange={set("video.frame_rate")}>
            <option value="0">Same as my clips (up to 60 fps)</option>
            {[24, 25, 30, 50, 60].map((k) => <option key={k} value={k}>{k} fps</option>)}
          </select>
        </label>
        <label className="field">Renders at the same time
          <select value={v["render.renders_at_once"]} onChange={set("render.renders_at_once")}>
            <option value="0">Automatic</option>
            {[1, 2, 3, 4].map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </label>
      </div>
      <div className="grid-fields">
        {num("video.target_min_seconds", "Target length from (s)", { min: 5, max: 180 })}
        {num("video.target_max_seconds", "Target length to (s)", { min: 5, max: 180 })}
        {num("video.max_seconds", "Maximum length (s)", { min: 5, max: 600 })}
      </div>
      {problem && <p className="error" role="alert">{problem}</p>}
      <fieldset>
        <legend>TikTok safe zone (pixels kept clear in a 1080×1920 frame)</legend>
        <div className="grid-fields" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))" }}>
          {num("safe_zone.top", "Top", { min: 0, max: 900 })}
          {num("safe_zone.bottom", "Bottom", { min: 0, max: 900 })}
          {num("safe_zone.left", "Left", { min: 0, max: 500 })}
          {num("safe_zone.right", "Right", { min: 0, max: 500 })}
        </div>
      </fieldset>
      <div><button className="primary" disabled={action.busy || !!problem}>Save defaults</button></div>
      <ErrorNote error={action.error} />
    </form>
  );
}

// ---- system check -------------------------------------------------------------------------

interface Doctor { status: string; checks: { name: string; status: "ok" | "warn" | "fail";
  detail: string; fix: string }[] }

function SystemCheck() {
  const [online, setOnline] = useState(false);
  const { data, error, reload } = useLive<Doctor>(`/api/doctor?online=${online}`, ONCE);
  return (
    <section className="card pad-l" aria-labelledby="sc-h">
      <div className="section-head">
        <h2 id="sc-h">System check</h2>
        <button onClick={() => (online ? reload() : setOnline(true))}>
          Run again, with the ElevenLabs test</button>
      </div>
      <ErrorNote error={error} />
      {!data ? <p className="muted small">Checking…</p> : (
        <ul className="checks">
          {data.checks.map((c) => (
            <li key={c.name}>
              <Mark status={c.status} />
              <span>{c.name}{c.status !== "ok" && c.fix && <span className="fix">{c.fix}</span>}</span>
              <span>{c.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
