import { useCallback, useEffect, useState } from "react";
import type { Capabilities, SessionInfo } from "./lib/types";
import { getJSON } from "./lib/api";
import { useLive } from "./lib/useLive";
import { useTheme } from "./lib/theme";
import { Icon } from "./components/Icon";
import { Card, Empty } from "./components/ui";
import { LivePage } from "./pages/LivePage";
import { BenchmarksPage } from "./pages/BenchmarksPage";
import { ModelsPage } from "./pages/ModelsPage";
import { HistoryPage } from "./pages/HistoryPage";
import { ArchitecturePage } from "./pages/ArchitecturePage";

const TABS = [
  { id: "live", label: "Live", icon: "activity" },
  { id: "bench", label: "Benchmarks", icon: "flask" },
  { id: "models", label: "AI models", icon: "brain" },
  { id: "history", label: "History", icon: "database" },
  { id: "how", label: "How it works", icon: "info" },
] as const;
type TabId = (typeof TABS)[number]["id"];

function initialTab(): TabId {
  const h = window.location.hash.replace("#", "");
  return (TABS.find((t) => t.id === h)?.id ?? "live") as TabId;
}

export default function App() {
  const { theme, setTheme } = useTheme();
  const [tab, setTab] = useState<TabId>(initialTab);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [capsErr, setCapsErr] = useState<string | null>(null);
  const [session, setSession] = useState<SessionInfo | null>(null);
  const live = useLive();

  useEffect(() => {
    getJSON<Capabilities>("/api/capabilities").then(setCaps).catch((e) => setCapsErr(String(e)));
  }, []);
  const refresh = useCallback(() => {
    getJSON<SessionInfo>("/api/session").then(setSession).catch(() => undefined);
  }, []);
  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 5000);
    return () => window.clearInterval(id);
  }, [refresh]);
  useEffect(() => {
    const onHash = () => setTab(initialTab());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = (t: TabId) => {
    window.location.hash = t;
    setTab(t);
  };

  const liveRunning = live.tick && !live.stopped && live.tick.session?.status === "running";
  const mode = (live.tick?.mode ?? session?.params?.mode) === "kernel" ? "Linux testbed" : "Simulation";

  return (
    <>
      <header className="header">
        <div className="brand">
          <div className="brand-mark"><Icon name="zap" size={17} /></div>
          <div>
            <div className="brand-name">FlowPilot</div>
            <div className="brand-tag">Adaptive QoS engine for mixed home broadband</div>
          </div>
        </div>
        <nav className="tabs">
          {TABS.map((t) => (
            <button key={t.id} className={`tab ${tab === t.id ? "active" : ""}`} onClick={() => go(t.id)}>
              <Icon name={t.icon} size={15} />
              {t.label}
            </button>
          ))}
        </nav>
        <div className="header-right">
          <span className="conn" title={live.connected ? "Live telemetry connected" : "Reconnecting to the API..."}>
            <span className={`dot ${liveRunning ? "live" : live.connected ? "on" : ""}`} />
            {liveRunning ? `${mode} live` : live.connected ? "Connected" : "Offline"}
          </span>
          <button className="btn icon-btn ghost" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} title="Toggle light / dark theme" aria-label="toggle theme">
            <Icon name={theme === "dark" ? "sun" : "moon"} size={16} />
          </button>
        </div>
      </header>
      <main className="container">
        {!caps ? (
          <Card>
            <Empty icon={capsErr ? "alert" : "clock"} title={capsErr ? "Cannot reach the FlowPilot API" : "Connecting to FlowPilot..."}>
              {capsErr ? <>Start it with <code>python -m flowpilot.api</code> ({capsErr})</> : null}
            </Empty>
          </Card>
        ) : tab === "live" ? (
          <LivePage caps={caps} live={live} session={session} refresh={refresh} />
        ) : tab === "bench" ? (
          <BenchmarksPage caps={caps} />
        ) : tab === "models" ? (
          <ModelsPage />
        ) : tab === "history" ? (
          <HistoryPage caps={caps} />
        ) : (
          <ArchitecturePage />
        )}
      </main>
    </>
  );
}
