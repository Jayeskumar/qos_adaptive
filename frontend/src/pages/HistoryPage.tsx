import { useEffect, useMemo, useState } from "react";
import type { AppSnap, Capabilities, DbStats, Decision, Profile, RunRow, SeriesRow } from "../lib/types";
import { getJSON } from "../lib/api";
import { useTheme } from "../lib/theme";
import { APP_NAME, DEVICE_ICON } from "../lib/qoe";
import { ago, compact, fmt, isNum } from "../lib/format";
import { schedLabel } from "../lib/labels";
import { Card, Empty, Seg, Stat } from "../components/ui";
import { Icon } from "../components/Icon";
import { TimeChart, type SeriesSpec } from "../components/Chart";

function appsOf(r: SeriesRow): Partial<AppSnap>[] {
  if (Array.isArray(r.apps)) return r.apps;
  return Object.entries(r.apps ?? {}).map(([id, a]) => ({ app_id: id, ...a }));
}

function RunDetail({ run, scenario }: { run: RunRow; scenario: string }) {
  const { pal } = useTheme();
  const [samples, setSamples] = useState<SeriesRow[] | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  useEffect(() => {
    setSamples(null);
    getJSON<SeriesRow[]>(`/api/runs/${run.id}/samples`).then(setSamples).catch(() => setSamples([]));
    getJSON<Decision[]>(`/api/runs/${run.id}/decisions`).then(setDecisions).catch(() => setDecisions([]));
  }, [run.id]);
  const rows = useMemo(
    () =>
      (samples ?? []).map((r) => {
        const apps = appsOf(r);
        const one = (t: string) => apps.find((a) => a.app_type === t);
        const cap = r.cap_down ?? r.capacity?.down;
        return {
          t: r.t,
          game: one("gaming")?.latency_p95_ms ?? null,
          call: one("video_call")?.latency_p95_ms ?? null,
          cap: isNum(cap) ? cap / 1e6 : null,
          br: one("streaming")?.bitrate_mbps ?? null,
        };
      }),
    [samples],
  );
  const side = /\((a|b)\)$/.exec(run.label ?? "")?.[1];
  const sub = [
    run.kind === "live" ? "Live session" : "Benchmark",
    run.mode === "kernel" ? "Linux testbed" : "Simulator",
    scenario,
    `seed ${run.seed}`,
    side ? `A/B side ${side.toUpperCase()}` : null,
    isNum(run.duration_s) ? `${Math.round(run.duration_s)} s` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const lat: SeriesSpec[] = [
    { key: "game", name: "Gaming ping p95", colour: pal.app.gaming },
    { key: "call", name: "Call delay p95", colour: pal.app.video_call },
  ];
  return (
    <Card title={`Run #${run.id} · ${schedLabel(run.scheduler, run.label)}`} sub={sub} icon="clock">
      {samples === null ? (
        <Empty icon="clock" title="Loading telemetry..." />
      ) : !rows.length ? (
        <Empty icon="info" title="No samples stored for this run" />
      ) : (
        <div className="stack" style={{ gap: 12 }}>
          {rows.some((r) => isNum(r.game) || isNum(r.call)) && <TimeChart data={rows} series={lat} unit="ms" height={210} />}
          <TimeChart data={rows} series={[{ key: "cap", name: "ISP capacity", colour: pal.ink2 }, { key: "br", name: "Netflix bitrate", colour: pal.app.streaming, kind: "step" }]} unit="Mb/s" digits={1} height={140} />
          <div>
            <div className="card-sub" style={{ marginBottom: 6 }}>Stored decisions ({decisions.length})</div>
            <div className={`feed${decisions.length > 3 ? " fade" : ""}`} style={{ maxHeight: 220 }}>
              {decisions.slice(0, 60).map((d, i) => (
                <div className="feed-item" key={i}>
                  <div className={`feed-icon kind-${d.kind}`}><Icon name={d.kind === "classify" ? "brain" : d.kind === "autorate" ? "gauge" : d.kind === "protect" ? "shield" : d.kind === "policy" ? "cog" : "sliders"} size={14} /></div>
                  <div><div className="feed-msg">{d.message}</div><div className="feed-meta"><span className="num">t = {fmt(d.t, 1)} s</span></div></div>
                </div>
              ))}
              {!decisions.length && <div className="small muted">No controller decisions (baseline routers make none).</div>}
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

export function HistoryPage({ caps }: { caps: Capabilities }) {
  const { pal } = useTheme();
  const [runs, setRuns] = useState<RunRow[] | null>(null);
  const [db, setDb] = useState<DbStats | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [kind, setKind] = useState("all");
  const [sel, setSel] = useState<RunRow | null>(null);
  useEffect(() => {
    getJSON<RunRow[]>(`/api/runs?limit=120${kind !== "all" ? `&kind=${kind}` : ""}`).then((r) => {
      setRuns(r);
      setSel((s) => s ?? r[0] ?? null);
    }).catch(() => setRuns([]));
  }, [kind]);
  useEffect(() => {
    getJSON<DbStats>("/api/db").then(setDb).catch(() => setDb(null));
    getJSON<Profile[]>("/api/profiles").then(setProfiles).catch(() => setProfiles([]));
  }, []);
  const label = (s: string) => schedLabel(s, caps.scheduler_labels[s]);
  const scenTitle = (s: string) => (caps.scenarios.find((x) => x.name === s)?.title ?? s).replace(/ \(interactive\)$/, "");
  const backendName = db?.backend === "postgresql" ? "PostgreSQL" : db?.backend === "sqlite" ? "SQLite" : db?.backend ?? "—";
  return (
    <div className="stack">
      <div>
        <div className="section-title">History and learned knowledge</div>
        <p className="section-sub">Every live session and benchmark run is persisted - per-second telemetry, every controller decision, classified flows, and per-device profiles that FlowPilot learns over time.</p>
      </div>
      <div className="stat-tiles">
        <Stat label="Database" value={backendName} sub="via SQLAlchemy" />
        <Stat label="Runs" value={compact(db?.runs)} sub="live sessions + benchmarks" />
        <Stat label="Telemetry samples" value={compact(db?.samples)} sub="one per second per router" />
        <Stat label="Decisions logged" value={compact(db?.decisions)} sub="explainable audit trail" />
        <Stat label="Flows classified" value={compact(db?.flows)} sub="with label, confidence, class" />
      </div>
      <div className="grid-2">
        <Card title="Runs" icon="database" right={<Seg value={kind} onChange={(v) => { setKind(v); setSel(null); }} options={[{ value: "all", label: "All" }, { value: "live", label: "Live" }, { value: "benchmark", label: "Benchmarks" }]} />}>
          {!runs ? (
            <Empty icon="clock" title="Loading..." />
          ) : !runs.length ? (
            <Empty icon="database" title="No runs stored yet" />
          ) : (
            <div className="table-wrap" style={{ maxHeight: 560 }}>
              <table className="t">
                <thead><tr><th>#</th><th>When</th><th>Type</th><th>Scenario</th><th>Router</th><th className="r">Length</th></tr></thead>
                <tbody>
                  {runs.map((r) => (
                    <tr key={r.id} className={`clickable ${sel?.id === r.id ? "sel" : ""}`} onClick={() => setSel(r)}>
                      <td className="num">{r.id}</td>
                      <td>{ago(r.created_at)}</td>
                      <td>
                        {r.kind === "live" ? "Live" : "Benchmark"}
                        <span className="muted"> · {r.mode === "kernel" ? "kernel" : "sim"}</span>
                      </td>
                      <td>{scenTitle(r.scenario)}</td>
                      <td>
                        <span className="row" style={{ gap: 7, alignItems: "center" }}>
                          <span className="side-swatch" style={{ width: 9, height: 9, background: r.scheduler.startsWith("flowpilot") ? pal.flowpilot : r.scheduler === "fifo" ? pal.baseline : pal.neutral }} />
                          {label(r.scheduler)}
                        </span>
                      </td>
                      <td className="r num">{r.status === "running" ? <span className="pill">running</span> : isNum(r.duration_s) ? `${Math.round(r.duration_s)} s` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        {sel ? <RunDetail run={sel} scenario={scenTitle(sel.scenario)} /> : <Card><Empty icon="clock" title="Select a run" /></Card>}
      </div>
      <Card title="What FlowPilot has learned about each device" sub="Accumulated from live sessions: which applications a device runs and how much bandwidth they need" icon="brain">
        {!profiles.length ? (
          <Empty icon="brain" title="No device profiles yet">Run a live session with FlowPilot for a minute - profiles are learned from the classified flows.</Empty>
        ) : (
          <div className="grid-3">
            {profiles.map((p) => {
              const apps = Object.entries(p.profile).sort((a, b) => b[1].avg_kbps_down - a[1].avg_kbps_down);
              const max = Math.max(1, ...apps.map(([, v]) => Math.max(v.peak_kbps_down, v.peak_kbps_up)));
              return (
                <div className="profile-card" key={p.device}>
                  <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 8 }}>
                    <Icon name={DEVICE_ICON[p.device] ?? "monitor"} size={16} />
                    <b>{p.device}</b>
                    <span className="small muted">{p.kind} · {p.ip}</span>
                  </div>
                  <div className="stack" style={{ gap: 8 }}>
                    {apps.map(([app, v]) => (
                      <div key={app}>
                        <div className="row small" style={{ justifyContent: "space-between" }}>
                          <span>{APP_NAME[app] ?? app}</span>
                          <span className="muted num">avg {(v.avg_kbps_down / 1000).toFixed(1)} / peak {(v.peak_kbps_down / 1000).toFixed(1)} Mb/s ↓</span>
                        </div>
                        <div className="bar-mini" title={`seen for ${v.seen_s} samples`}>
                          <i style={{ width: `${(Math.max(v.avg_kbps_down, v.avg_kbps_up) / max) * 100}%`, background: pal.app[app] ?? pal.flowpilot }} />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}
