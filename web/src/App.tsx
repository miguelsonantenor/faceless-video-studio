import { useLive, type Overview } from "./api";
import { Dashboard } from "./pages/Dashboard";
import { Assets, Templates } from "./pages/Library";
import { Project } from "./pages/Project";
import { ReviewQueue } from "./pages/ReviewQueue";
import { Settings } from "./pages/Settings";
import { Link, usePath } from "./ui";

const NAV = [
  ["/", "Dashboard"], ["/review", "Review queue"], ["/templates", "Templates"],
  ["/assets", "Assets"], ["/settings", "Settings"],
] as const;

export function App() {
  const path = usePath();
  const { data: overview, error } = useLive<Overview>("/api/overview");
  const waiting = overview?.counts.in_review ?? 0;
  const section = "/" + (path.split("/")[1] ?? "");
  const project = path.match(/^\/projects\/([^/]+)$/);

  let page;
  if (project) page = <Project ref_={decodeURIComponent(project[1])} />;
  else if (section === "/review") page = <ReviewQueue />;
  else if (section === "/templates") page = <Templates />;
  else if (section === "/assets") page = <Assets />;
  else if (section === "/settings") page = <Settings overview={overview} />;
  else page = <Dashboard overview={overview} />;

  return (
    <>
      <header className="top">
        <Link to="/" className="brand">
          <span className="logo" aria-hidden="true">F</span>
          <span>Faceless Video Studio</span>
        </Link>
        <nav className="nav" aria-label="Main">
          {NAV.map(([to, label]) => (
            <Link key={to} to={to} current={to === "/" ? section === "/" || !!project
              : section === to}>
              {label}
              {to === "/review" && waiting > 0 && (
                <span className="badge" aria-label={`${waiting} waiting`}>{waiting}</span>
              )}
            </Link>
          ))}
        </nav>
        <div className="status" role="status">
          <span className={`dot ${error ? "off" : ""}`} aria-hidden="true" />
          {error ? "Core not reachable"
            : `Core running · ${overview?.claude_connected ? "Claude connected"
              : "Claude not connected yet"}`}
        </div>
      </header>
      {error?.code === "UNAUTHORIZED" ? (
        <main className="narrow">
          <h1>Open FVS from the terminal</h1>
          <p>This browser has no FVS session. Run <code>fvs open</code>; it opens the web app
            signed in.</p>
        </main>
      ) : page}
    </>
  );
}
