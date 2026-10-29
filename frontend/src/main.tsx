import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter, Link, Route, Routes } from "react-router-dom";
import { ShieldCheck } from "lucide-react";
import "@fontsource-variable/dm-sans";
import { api } from "@/api";
import { useLoad } from "@/hooks";
import { Disagreements } from "@/pages/Disagreements";
import { Performance } from "@/pages/Performance";
import { EvalCaseEditor, EvalSet } from "@/pages/EvalSet";
import { EvalRunPage } from "@/pages/Evaluations";
import { Changes } from "@/pages/Changes";
import { Failures } from "@/pages/Failures";
import { Overview } from "@/pages/Overview";
import { Space } from "@/pages/Space";
import { Trace } from "@/pages/Trace";
import { Versions } from "@/pages/Versions";
import { EvaluatorsPage } from "@/pages/Evaluators";
import { Settings } from "@/pages/Settings";
import "./styles.css";

// HashRouter (#/spaces/...) so deep links work without server-side routing: FastAPI only
// serves index.html at "/".
function App() {
  const me = useLoad(() => api.me(), []);
  const email = me.data?.email;
  return (
    <HashRouter>
      <header className="sticky top-0 z-10 border-t-[3px] border-b border-t-lava border-b-line bg-white/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-6">
          <Link to="/" className="flex items-center gap-2.5 text-ink hover:no-underline">
            <span className="flex size-7 items-center justify-center rounded-md bg-lava text-white">
              <ShieldCheck className="size-4" aria-hidden />
            </span>
            <span className="font-semibold tracking-tight">Genie EvalOps</span>
            <span className="hidden text-sm text-muted sm:inline">· quality checks for Genie Agents</span>
          </Link>
          {email && (
            <span className="flex items-center gap-2 text-sm text-muted" title="Signed in as">
              <span className="flex size-7 items-center justify-center rounded-full bg-navy text-xs font-semibold uppercase text-white">
                {email[0]}
              </span>
              <span className="hidden sm:inline">{email}</span>
            </span>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/spaces/:spaceId" element={<Space />} />
          <Route path="/spaces/:spaceId/failures" element={<Failures />} />
          <Route path="/spaces/:spaceId/disagreements" element={<Disagreements />} />
          <Route path="/spaces/:spaceId/performance" element={<Performance />} />
          <Route path="/spaces/:spaceId/evalset" element={<EvalSet />} />
          <Route path="/spaces/:spaceId/evalset/new" element={<EvalCaseEditor />} />
          <Route path="/spaces/:spaceId/evalset/:caseId" element={<EvalCaseEditor />} />
          <Route path="/spaces/:spaceId/evaluations/:runId" element={<EvalRunPage />} />
          <Route path="/spaces/:spaceId/changes" element={<Changes />} />
          <Route path="/spaces/:spaceId/versions" element={<Versions />} />
          <Route path="/spaces/:spaceId/evaluators" element={<EvaluatorsPage />} />
          <Route path="/spaces/:spaceId/settings" element={<Settings />} />
          <Route path="/traces/:traceId" element={<Trace />} />
        </Routes>
      </main>
    </HashRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
