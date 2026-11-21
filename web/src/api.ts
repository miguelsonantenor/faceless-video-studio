// Talking to the FVS core. The session cookie (set by `fvs open`) authenticates every
// call; errors arrive as {code, message, fix} and are thrown as ApiError.
import { useCallback, useEffect, useRef, useState } from "react";

export class ApiError extends Error {
  code: string;
  fix: string;
  constructor(code: string, message: string, fix: string) {
    super(message);
    this.code = code;
    this.fix = fix;
  }
}

export async function api<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const resp = await fetch(path, {
    method: init?.method ?? "GET",
    headers: init?.body === undefined ? undefined : { "Content-Type": "application/json" },
    body: init?.body === undefined ? undefined : JSON.stringify(init.body),
    credentials: "same-origin",
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    throw new ApiError(data?.code ?? String(resp.status), data?.message ?? resp.statusText,
      data?.fix ?? "");
  }
  return data as T;
}

// ---- live updates ---------------------------------------------------------------------

type Listener = () => void;
const listeners = new Set<Listener>();
let source: EventSource | null = null;
let timer: number | undefined;

function connect() {
  if (source) return;
  source = new EventSource("/api/events");
  const changed = () => {
    // Bursts of events (job progress) refresh the screen at most a few times a second.
    window.clearTimeout(timer);
    timer = window.setTimeout(() => listeners.forEach((l) => l()), 250);
  };
  for (const type of ["activity", "job", "job_progress", "settings_changed"]) {
    source.addEventListener(type, changed);
  }
}

/** Load `path`, and load it again whenever the core reports a change. */
export function useLive<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const current = useRef(path);
  current.current = path;

  const load = useCallback(async () => {
    if (!path) return;
    try {
      const value = await api<T>(path);
      if (current.current === path) {
        setData(value);
        setError(null);
      }
    } catch (e) {
      if (current.current === path) setError(e as ApiError);
    }
  }, [path]);

  useEffect(() => {
    setData(null);
    load();
    connect();
    listeners.add(load);
    return () => {
      listeners.delete(load);
    };
  }, [load]);

  return { data, error, reload: load };
}

// ---- shapes ---------------------------------------------------------------------------

export type State =
  | "new" | "scripted" | "voiced" | "planned" | "rendering" | "claude_check"
  | "in_review" | "changes_requested" | "exported" | "skipped";

export interface Check {
  id: string;
  name: string;
  status: "pass" | "warn" | "fail";
  measured: string;
  limit: string;
  fix: string | null;
}

export interface QaReport {
  status: "pass" | "warn" | "fail";
  summary: string;
  checks: Check[];
}

export interface Job {
  id: string;
  kind: string;
  project_id: string | null;
  state: string;
  progress: number | null;
  progress_note: string | null;
}

export interface ActivityEntry {
  id: number;
  at: string;
  source: string;
  tool: string | null;
  summary: string;
}

export interface Budget {
  limit: number;
  spent_today: number;
  left: number;
  resets_at: string;
}

export interface Quota {
  available: boolean;
  limit?: number;
  left?: number;
  resets_at?: string | null;
  reason?: string;
  fix?: string;
}

export interface Overview {
  claude_connected: boolean;
  counts: Record<State, number>;
  labels: Record<State, string>;
  budget: Budget;
  quota: Quota;
  jobs: Job[];
  activity: ActivityEntry[];
}

export interface ProjectRow {
  id: string;
  number: number;
  title: string;
  state: State;
  failed: boolean;
  template: string | null;
  render_id: string | null;
  clips: string[];
  where: string;
  length: string | null;
  qa: { status: QaReport["status"]; summary: string; failed: Check[] } | null;
  note: string | null;
  updated_at: string;
}

export interface Sentence {
  i: number;
  caption: string;
  spoken: string;
}

export interface Segment {
  index: number;
  clip: number;
  out_start: number;
  out_end: number;
  src_in: number;
  sentences: number[];
  text: string;
}

export interface Render {
  id: string;
  current: boolean;
  duration_s: number;
  created_at: string;
  contact_sheet: string | null;
  qa: QaReport | null;
}

export interface ProjectDetail {
  project: {
    id: string; number: number; title: string; state: State; failed: boolean;
    last_error: { message: string; fix: string } | null; stale: string[];
    script_version: number | null; spec_version: number | null; render_id: string | null;
    template: string | null;
  };
  state_label: string;
  clips: { number: number; file_name: string; duration_s: number }[];
  script?: {
    version: number; title: string; voiceover: string; hook: string | null;
    post_caption: string | null; hashtags: string[]; est_seconds: number;
    sentences: Sentence[];
  };
  spec?: { version: number; spec_hash: string; spec: Record<string, unknown> };
  timeline?: { duration_s: number; segments: Segment[]; error?: { message: string } };
  renders?: Render[];
  reviews?: { at: string; decision: string; note: string | null; by: string }[];
  scripts: { version: number; voiceover_text: string; hook: string | null; author: string; est_seconds: number;
    created_at: string }[];
  specs: { version: number; spec_hash: string; author: string; created_at: string }[];
  voiceover: { voice_alias: string; duration_s: number; chars_billed: number;
    chars_reused: number } | null;
  jobs: { id: string; kind: string; state: string; created_at: string }[];
}

export interface SettingsPayload {
  settings: Record<string, string | number>;
  elevenlabs: { configured: boolean; key_last4: string | null };
  folders: { input: string; workspace: string; output: string; inspiration: string };
  mcp: { address: string; token_last4: string };
}
