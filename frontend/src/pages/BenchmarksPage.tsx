import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { Bar, BarChart, CartesianGrid, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { BenchRow, Capabilities, SeriesRow } from "../lib/types";
import { getJSON } from "../lib/api";
import { useTheme, type Palette } from "../lib/theme";
import { isNum } from "../lib/format";
import { Card, Empty, Legend, Seg, Select, tipLabel } from "../components/ui";
import { Icon } from "../components/Icon";
import { schedLabel } from "../lib/labels";
import { TimeChart, type SeriesSpec } from "../components/Chart";

const ORDER = ["fifo", "static_qos", "fq_codel", "cake", "cake_autorate", "flowpilot", "flowpilot_fq", "flowpilot_rl"];

interface Metric {
  key: string;
  label: string;
  unit: string;
  better: "lower" | "higher";
  digits: number;
  short?: string;
  /** natural top of the scale (MOS 5, fairness 1, 100 %): the axis always ends there */
  ceiling?: number;
}

const METRICS: Metric[] = [
  { key: "game_rtt_p99_ms", label: "Gaming ping p99", unit: "ms", better: "lower", digits: 0, short: "Game p99" },
  { key: "call_owd_p99_ms", label: "Video-call delay p99", unit: "ms", better: "lower", digits: 0, short: "Call p99" },
  { key: "call_mos", label: "Video-call MOS", unit: "", better: "higher", digits: 2, short: "Call MOS", ceiling: 5 },
  { key: "stream_bitrate_mbps", label: "Netflix average bitrate", unit: "Mb/s", better: "higher", digits: 1, short: "Netflix" },
  { key: "stream_rebuffer_pct", label: "Netflix rebuffering", unit: "%", better: "lower", digits: 1, short: "Rebuffer" },
  { key: "web_plt_p50_s", label: "Web page load (median)", unit: "s", better: "lower", digits: 2, short: "Page load" },
  { key: "bulk_mbps", label: "Download throughput", unit: "Mb/s", better: "higher", digits: 1, short: "Download" },
  { key: "upload_mbps", label: "Upload throughput", unit: "Mb/s", better: "higher", digits: 1, short: "Upload" },
  { key: "utilization_down_pct", label: "Downlink utilisation", unit: "%", better: "higher", digits: 1, short: "Util.", ceiling: 100 },
  { key: "jain_bulk_devices", label: "Per-device fairness (Jain)", unit: "", better: "higher", digits: 2, short: "Device fair.", ceiling: 1 },
  { key: "qoe_fairness", label: "QoE fairness (Hossfeld F)", unit: "", better: "higher", digits: 2, short: "QoE fair.", ceiling: 1 },
  { key: "mean_mos", label: "Mean MOS, all apps", unit: "", better: "higher", digits: 2, short: "Mean MOS", ceiling: 5 },
];

const SERIES_METRICS = [
  { value: "game", label: "Gaming ping p95", unit: "ms", log: true },
  { value: "call", label: "Video-call delay p95", unit: "ms", log: true },
  { value: "netflix", label: "Netflix bitrate", unit: "Mb/s", log: false },
  { value: "bulk", label: "Download throughput", unit: "Mb/s", log: false },
];

function schedColour(pal: Palette, s: string): string {
  if (s.startsWith("flowpilot")) return pal.flowpilot;
  if (s === "fifo") return pal.baseline;
  return pal.neutral;
}

interface Agg {
  scheduler: string;
  label: string;
  n: number;
  m: Record<string, number | null>;
  ids: number[];
}

function aggregate(rows: BenchRow[]): Agg[] {
  const by = new Map<string, Agg>();
  for (const r of rows) {
    let a = by.get(r.scheduler);
    if (!a) {
      a = { scheduler: r.scheduler, label: schedLabel(r.scheduler, r.label), n: 0, m: {}, ids: [] };
      by.set(r.scheduler, a);
    }
    a.n += 1;
    a.ids.push(r.id);
    for (const mt of METRICS) {
      const v = r.summary[mt.key];
      if (isNum(v)) a.m[mt.key] = ((a.m[mt.key] ?? 0) as number) + v;
    }
  }
  const out = [...by.values()];
  for (const a of out) for (const k of Object.keys(a.m)) a.m[k] = (a.m[k] as number) / a.n;
  out.sort((x, y) => ORDER.indexOf(x.scheduler) - ORDER.indexOf(y.scheduler));
  return out;
}

// Evenly spaced ticks from zero (steps of 1, 2, 2.5 or 5 x 10^k), so no tick is silently dropped.
function axisTicks(max: number, ceiling?: number): number[] {
  if (ceiling) {
    const div = ceiling === 5 ? 5 : 4;
    return Array.from({ length: div + 1 }, (_, i) => +((ceiling * i) / div).toFixed(6));
  }
  if (!(max > 0)) return [0, 1];
  const raw = max / 4;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((x) => x >= raw) ?? 10 * p;
  return Array.from({ length: Math.ceil(max / step - 1e-9) + 1 }, (_, i) => +(i * step).toFixed(6));
}

function MetricBars({ metric, aggs }: { metric: Metric; aggs: Agg[] }) {
  const { pal } = useTheme();
  const data = aggs.filter((a) => isNum(a.m[metric.key])).map((a) => ({ name: a.label, sched: a.scheduler, v: a.m[metric.key] as number }));
  if (!data.length) return null;
  const vals = data.map((d) => d.v);
  const best = metric.better === "lower" ? Math.min(...vals) : Math.max(...vals);
  const ticks = axisTicks(Math.max(...vals), metric.ceiling);
  return (
    <Card title={metric.label} sub={`${metric.better === "lower" ? "Lower" : "Higher"} is better${metric.unit ? ` · ${metric.unit}` : ""}`}>
      <ResponsiveContainer width="100%" height={Math.max(150, data.length * 30 + 20)}>
        <BarChart data={data} layout="vertical" margin={{ top: 0, right: 54, bottom: 0, left: 0 }} barCategoryGap={6}>
          <CartesianGrid stroke={pal.grid} horizontal={false} />
          <XAxis
            type="number"
            domain={[0, ticks[ticks.length - 1]]}
            ticks={ticks}
            interval={0}
            tickFormatter={(v: number) => `${+v.toFixed(2)}`}
            stroke={pal.axis}
            tick={{ fill: pal.muted, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
          />
          <YAxis type="category" dataKey="name" width={156} stroke={pal.axis} tick={{ fill: pal.ink2, fontSize: 11.5 }} tickLine={false} axisLine={false} />
          <Tooltip
            cursor={{ fill: pal.grid, fillOpacity: 0.5 }}
            isAnimationActive={false}
            content={({ active, payload }) =>
              active && payload?.length ? (
                <div className="tooltip">
                  <div className="tt-title">{String(payload[0].payload.name)}</div>
                  <div className="tt-row"><span>{metric.label}</span><b>{(payload[0].value as number).toFixed(metric.digits)} {metric.unit}</b></div>
                </div>
              ) : null
            }
          />
          <Bar dataKey="v" barSize={14} radius={[0, 4, 4, 0]} isAnimationActive={false}>
            {data.map((d) => (
              <Cell key={d.sched} fill={schedColour(pal, d.sched)} />
            ))}
            <LabelList dataKey="v" content={tipLabel((v) => `${v.toFixed(metric.digits)}${v === best ? " ✓" : ""}`, pal.ink2)} />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </Card>
  );
}

function SeriesView({ aggs, mode, scenario }: { aggs: Agg[]; mode: string; scenario: string }) {
  const { pal } = useTheme();
  const [metric, setMetric] = useState("game");
  const comparators = aggs.filter((a) => !a.scheduler.startsWith("flowpilot"));
  const [cmp, setCmp] = useState("fifo");
  const [series, setSeries] = useState<Record<string, SeriesRow[]>>({});
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancel = false;
    setLoading(true);
    Promise.all(aggs.map((a) => getJSON<SeriesRow[]>(`/api/benchmarks/${a.ids[0]}/series`).then((rows) => [a.scheduler, rows] as const)))
      .then((pairs) => {
        if (!cancel) setSeries(Object.fromEntries(pairs));
      })
      .catch(() => !cancel && setSeries({}))
      .finally(() => !cancel && setLoading(false));
    return () => {
      cancel = true;
    };
  }, [mode, scenario, aggs]);

  useEffect(() => {
    if (!comparators.some((c) => c.scheduler === cmp) && comparators.length) setCmp(comparators[0].scheduler);
  }, [comparators, cmp]);

  const pick = (r: SeriesRow): number | null => {
    const apps = (r.apps ?? {}) as Record<string, { app_type?: string; latency_p95_ms?: number | null; bitrate_mbps?: number | null; kbps_down?: number }>;
    if (metric === "bulk") {
      let s = 0;
      for (const a of Object.values(apps)) if (a.app_type === "bulk") s += (a.kbps_down ?? 0) / 1000;
      return s;
    }
    const id = metric === "game" ? "game" : metric === "call" ? "call" : "netflix";
    const a = apps[id];
    if (!a) return null;
    const v = metric === "netflix" ? a.bitrate_mbps : a.latency_p95_ms;
    return isNum(v) && (metric === "netflix" || v > 0) ? v : null;
  };

  const rows = useMemo(() => {
    const byT = new Map<number, Record<string, number | null>>();
    for (const [sched, rs] of Object.entries(series)) {
      for (const r of rs) {
        const t = Math.round(r.t);
        const row = byT.get(t) ?? { t };
        row[sched] = pick(r);
        if (row.cap === undefined && isNum(r.cap_down)) row.cap = r.cap_down / 1e6;
        if (sched === "flowpilot") row.shaper_fp = isNum(r.shaper_down) ? r.shaper_down / 1e6 : null;
        if (sched === cmp) row.shaper_cmp = isNum(r.shaper_down) ? r.shaper_down / 1e6 : null;
        byT.set(t, row);
      }
    }
    return [...byT.values()].sort((a, b) => (a.t as number) - (b.t as number));
  }, [series, metric, cmp]); // eslint-disable-line react-hooks/exhaustive-deps

  const m = SERIES_METRICS.find((x) => x.value === metric)!;
  const present = Object.keys(series);
  const main = present.includes("flowpilot") ? "flowpilot" : present.find((x) => x.startsWith("flowpilot"));
  const specs: SeriesSpec[] = present
    .filter((s) => s !== cmp && s !== main)
    .map((s) => ({ key: s, name: aggs.find((a) => a.scheduler === s)?.label ?? s, colour: pal.neutral, width: 1.25, muted: true }));
  if (present.includes(cmp)) specs.push({ key: cmp, name: aggs.find((a) => a.scheduler === cmp)?.label ?? cmp, colour: pal.baseline, width: 2 });
  if (main) specs.push({ key: main, name: aggs.find((a) => a.scheduler === main)?.label ?? main, colour: pal.flowpilot, width: 2.5 });
  const hasData = rows.some((r) => present.some((s) => isNum(r[s])));
  const capSpecs: SeriesSpec[] = [
    { key: "cap", name: "True ISP capacity", colour: pal.ink2, width: 1.5 },
    { key: "shaper_fp", name: "FlowPilot shaper", colour: pal.flowpilot },
  ];
  if (rows.some((r) => isNum(r.shaper_cmp))) capSpecs.push({ key: "shaper_cmp", name: `${aggs.find((a) => a.scheduler === cmp)?.label ?? cmp} shaper`, colour: pal.baseline });

  return (
    <Card
      title="What happened over time"
      sub="First seed of every router, same scenario timeline"
      icon="activity"
      right={
        <div className="row" style={{ gap: 10, alignItems: "flex-end" }}>
          <Select label="Metric" value={metric} onChange={setMetric} options={SERIES_METRICS.map((x) => ({ value: x.value, label: x.label }))} width={190} />
          <Select
            label="Highlight comparator"
            value={cmp}
            onChange={setCmp}
            options={comparators.map((c) => ({ value: c.scheduler, label: c.label }))}
            width={200}
          />
        </div>
      }
    >
      {loading ? (
        <Empty icon="clock" title="Loading series..." />
      ) : !hasData ? (
        <Empty icon="activity" title="This metric is not part of the scenario" />
      ) : (
        <>
          <TimeChart
            data={rows}
            series={specs}
            unit={m.unit}
            digits={m.unit === "ms" ? 0 : 1}
            log={m.log}
            height={280}
            extraLegend={specs.some((s) => s.muted) ? [{ label: "other routers and variants", colour: pal.neutral }] : []}
          />
          {rows.some((r) => isNum(r.cap)) && (
            <div style={{ marginTop: 10 }}>
              <div className="card-sub" style={{ marginBottom: 4 }}>Downlink capacity vs shaper rate (Mb/s) - autorate tracking the true bottleneck</div>
              <TimeChart data={rows} series={capSpecs} unit="Mb/s" digits={1} height={130} />
            </div>
          )}
        </>
      )}
    </Card>
  );
}

export function BenchmarksPage({ caps }: { caps: Capabilities }) {
  const { pal } = useTheme();
  const [rows, setRows] = useState<BenchRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState("sim");
  const [scenario, setScenario] = useState<string>("");
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);

  useEffect(() => {
    getJSON<BenchRow[]>("/api/benchmarks").then(setRows).catch((e) => setErr(String(e)));
  }, []);

  const modes = useMemo(() => [...new Set((rows ?? []).map((r) => r.mode))].sort(), [rows]);
  useEffect(() => {
    if (modes.length && !modes.includes(mode)) setMode(modes[0]);
  }, [modes, mode]);
  const scenarios = useMemo(() => {
    const names = [...new Set((rows ?? []).filter((r) => r.mode === mode).map((r) => r.scenario))];
    const order = caps.scenarios.map((s) => s.name);
    return names.sort((a, b) => order.indexOf(a) - order.indexOf(b));
  }, [rows, mode, caps]);
  useEffect(() => {
    if (scenarios.length && !scenarios.includes(scenario)) setScenario(scenarios[0]);
  }, [scenarios, scenario]);

  const aggs = useMemo(() => aggregate((rows ?? []).filter((r) => r.mode === mode && r.scenario === scenario)), [rows, mode, scenario]);
  const seeds = useMemo(() => new Set((rows ?? []).filter((r) => r.mode === mode && r.scenario === scenario).map((r) => r.seed)).size, [rows, mode, scenario]);
  // hide metrics that are absent from the scenario or identical for every router
  const metrics = METRICS.filter((m) => {
    const v = aggs.map((a) => a.m[m.key]).filter(isNum);
    if (!v.length) return false;
    if (Math.max(...v) <= 0 && ["upload_mbps", "bulk_mbps", "stream_rebuffer_pct"].includes(m.key)) return false;
    return v.length === 1 || Math.max(...v) - Math.min(...v) > 1e-9;
  });
  const sc = caps.scenarios.find((s) => s.name === scenario);
  // 4 or 3 cards per row, whichever leaves fewer empty slots in the last row
  const empty = (c: number) => (c - (metrics.length % c)) % c;
  const cols = empty(3) < empty(4) ? 3 : 4;

  const sorted = useMemo(() => {
    if (!sort) return aggs;
    return aggs.slice().sort((x, y) => (((x.m[sort.key] ?? -1e18) as number) - ((y.m[sort.key] ?? -1e18) as number)) * sort.dir);
  }, [aggs, sort]);

  if (err) return <Card><Empty icon="alert" title="Could not load benchmarks">{err}</Empty></Card>;
  if (!rows) return <Card><Empty icon="clock" title="Loading benchmarks..." /></Card>;
  if (!rows.length)
    return (
      <Card>
        <Empty icon="flask" title="No benchmark results yet">
          Run <code>python -m flowpilot.bench.run_bench</code> and import with <code>python -m flowpilot.db.importer</code>.
        </Empty>
      </Card>
    );

  return (
    <div className="stack">
      <div className="row wrap" style={{ alignItems: "flex-end", justifyContent: "space-between" }}>
        <div>
          <div className="section-title">Measured against standard scheduling</div>
          <p className="section-sub">
            {seeds > 1
              ? `Every scenario was run with each router on ${seeds} seeds (identical traffic per seed); numbers are means.`
              : "Every scenario was run once with each router on identical traffic (same seed)."}{" "}
            Blue = FlowPilot, orange = today's standard router (drop-tail FIFO), grey = other queue managers (static QoS, fq_codel, CAKE, CAKE + autorate).
          </p>
        </div>
        {modes.length > 1 ? (
          <Seg value={mode} onChange={setMode} options={modes.map((m) => ({ value: m, label: m === "kernel" ? "Linux kernel testbed" : "Simulator" }))} />
        ) : (
          <span className="pill">
            <Icon name={mode === "kernel" ? "server" : "chip"} size={13} />
            {mode === "kernel" ? "Linux kernel testbed" : "Simulator results"}
          </span>
        )}
      </div>
      <div className="scenario-tabs">
        {scenarios.map((s) => {
          const meta = caps.scenarios.find((x) => x.name === s);
          return (
            <button key={s} className={`scenario-tab ${s === scenario ? "on" : ""}`} onClick={() => setScenario(s)}>
              <div className="st-title">{meta?.title ?? s}</div>
              <div className="st-sub">{meta?.duration ? `${meta.duration} s` : ""}</div>
            </button>
          );
        })}
      </div>
      {sc && <div className="banner"><b style={{ color: "var(--ink)" }}>{sc.title}.</b> {sc.description}</div>}
      <Legend items={[{ label: "FlowPilot", colour: pal.flowpilot, box: true }, { label: "Standard router (FIFO)", colour: pal.baseline, box: true }, { label: "Other queue managers", colour: pal.neutral, box: true }]} />
      <div className="metric-grid" style={{ "--cols": cols } as CSSProperties}>
        {metrics.map((m) => (
          <MetricBars key={m.key} metric={m} aggs={aggs} />
        ))}
      </div>
      <SeriesView aggs={aggs} mode={mode} scenario={scenario} />
      <Card title="All metrics" sub="Click a column to sort · ✓ marks the best router for that metric" icon="list">
        <div className="table-wrap" style={{ maxHeight: "none" }}>
          <table className="t">
            <thead>
              <tr>
                <th>Router</th>
                {metrics.map((m) => (
                  <th key={m.key} title={m.label} className="r sortable" onClick={() => setSort((s) => ({ key: m.key, dir: s?.key === m.key ? ((-s.dir) as 1 | -1) : m.better === "lower" ? 1 : -1 }))}>
                    {m.short ?? m.label}
                    {m.unit ? ` (${m.unit})` : ""}
                    {sort?.key === m.key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((a) => (
                <tr key={a.scheduler}>
                  <td>
                    <span className="row" style={{ gap: 8, alignItems: "center" }}>
                      <span className="side-swatch" style={{ background: schedColour(pal, a.scheduler), width: 10, height: 10 }} />
                      {a.label}
                    </span>
                  </td>
                  {metrics.map((m) => {
                    const vals = aggs.map((x) => x.m[m.key]).filter(isNum);
                    const best = m.better === "lower" ? Math.min(...vals) : Math.max(...vals);
                    const v = a.m[m.key];
                    return (
                      <td key={m.key} className={`r ${isNum(v) && v === best ? "best" : ""}`}>
                        {isNum(v) ? v.toFixed(m.digits) : "—"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
