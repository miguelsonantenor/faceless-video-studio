// Small shared pieces: navigation, chips, check marks, the player, formatting.
import { useEffect, useState, type MouseEvent, type ReactNode } from "react";
import type { ApiError, Check, State } from "./api";

// ---- a minimal router (five screens do not need a library) ----------------------------

export function navigate(to: string) {
  window.history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo(0, 0);
}

export function usePath() {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return path;
}

export function Link(props: { to: string; className?: string; current?: boolean;
  children: ReactNode }) {
  const go = (e: MouseEvent) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    navigate(props.to);
  };
  return (
    <a href={props.to} className={props.className} onClick={go}
      aria-current={props.current ? "page" : undefined}>
      {props.children}
    </a>
  );
}

// ---- state chips ----------------------------------------------------------------------

const CHIP: Partial<Record<State, string>> = {
  rendering: "blue", claude_check: "blue", in_review: "amber", changes_requested: "amber",
  exported: "green",
};

export function StateChip({ state, label, failed }: { state: State; label: string;
  failed?: boolean }) {
  return (
    <>
      <span className={`chip ${CHIP[state] ?? ""}`}>{label}</span>
      {failed && <span className="chip red">failed</span>}
    </>
  );
}

// ---- check marks ----------------------------------------------------------------------

export function Mark({ status }: { status: Check["status"] | "ok" | "warn" | "fail" }) {
  if (status === "pass" || status === "ok") {
    return (
      <svg className="icon" width="16" height="16" viewBox="0 0 16 16" aria-label="passed"
        role="img">
        <path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="#1F9D55" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (status === "warn") {
    return (
      <svg className="icon" width="16" height="16" viewBox="0 0 16 16" aria-label="warning"
        role="img">
        <path d="M8 2.5l6 11H2z" fill="none" stroke="#B57300" strokeWidth="1.8"
          strokeLinejoin="round" />
        <path d="M8 7v3M8 11.8v.2" stroke="#B57300" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    );
  }
  return (
    <svg className="icon" width="16" height="16" viewBox="0 0 16 16" aria-label="failed"
      role="img">
      <path d="M4 4l8 8M12 4l-8 8" stroke="#9B1C14" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

export function Checks({ checks }: { checks: Check[] }) {
  return (
    <ul className="checks">
      {checks.map((c) => (
        <li key={c.id}>
          <Mark status={c.status} />
          <span>
            {c.name}
            {c.status !== "pass" && c.fix && <span className="fix">{c.fix}</span>}
          </span>
          <span className="num">{c.measured}{c.status !== "pass" && c.limit ?
            ` (limit ${c.limit})` : ""}</span>
        </li>
      ))}
    </ul>
  );
}

// ---- the player -----------------------------------------------------------------------

export interface SafeZone { top: number; bottom: number; left: number; right: number }

/** The render at its 9:16 shape, with TikTok's safe zone drawn over it when asked. */
export function Player({ renderId, safe, empty }: { renderId: string | null;
  safe?: SafeZone | null; empty?: string }) {
  const pct = (px: number, of: number) => `${(100 * px) / of}%`;
  return (
    <div className="player">
      {renderId ? (
        <video src={`/api/renders/${renderId}/video`} controls preload="metadata"
          playsInline aria-label="The rendered video" />
      ) : (
        <div className="empty-player">{empty ?? "No render yet."}</div>
      )}
      {safe && (
        <div className="safe" style={{ top: pct(safe.top, 1920), bottom: pct(safe.bottom, 1920),
          left: pct(safe.left, 1080), right: pct(safe.right, 1080) }}>
          <span>safe zone</span>
        </div>
      )}
    </div>
  );
}

// ---- messages -------------------------------------------------------------------------

export function ErrorNote({ error }: { error: ApiError | null }) {
  if (!error) return null;
  return (
    <p className="error" role="alert">
      {error.message}{error.fix ? ` ${error.fix}` : ""}
    </p>
  );
}

export function Toast({ text, onDone }: { text: string | null; onDone: () => void }) {
  useEffect(() => {
    if (!text) return;
    const t = window.setTimeout(onDone, 4000);
    return () => window.clearTimeout(t);
  }, [text, onDone]);
  return text ? <div className="toast" role="status">{text}</div> : null;
}

// ---- formatting -----------------------------------------------------------------------

export function clock(iso: string) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

export function day(iso: string) {
  return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });
}

export function secs(s: number) {
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  return `${m}:${rest.toFixed(1).padStart(4, "0")}`;
}

export function n(value: number) {
  return value.toLocaleString();
}
