// Time-series chart used across the app: thin 2px lines, ~soft area fills for stacks,
// recessive solid hairline grid, crosshair tooltip, legend above the plot.
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useTheme } from "../lib/theme";
import { Legend } from "./ui";

type LegendItem = { label: string; colour: string; box?: boolean; dash?: boolean };

// 1-2-5 ticks for log axes, densified until at least four fall inside the data range.
function logTicks(lo: number, hi: number): number[] {
  let out: number[] = [];
  for (const seq of [[1, 2, 5], [1, 2, 3, 5, 7], [1, 1.5, 2, 3, 4, 5, 6, 7, 8, 9]]) {
    out = [];
    for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
      for (const m of seq) {
        const v = +(m * 10 ** e).toPrecision(3);
        if (v >= lo && v <= hi) out.push(v);
      }
    }
    if (out.length >= 4) break;
  }
  return out;
}

// Evenly spaced linear ticks (steps of 1, 2, 2.5 or 5 x 10^k) spanning [lo, hi].
function linTicks(lo: number, hi: number): number[] {
  if (!(hi > lo)) return [lo - 1, lo, lo + 1];
  const raw = (hi - lo) / 4;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((x) => x >= raw) ?? 10 * p;
  const out: number[] = [];
  for (let v = Math.floor(lo / step - 1e-9) * step; v <= hi + step - 1e-9; v += step) {
    out.push(+v.toFixed(6));
    if (v >= hi - 1e-9) break;
  }
  return out;
}

export interface SeriesSpec {
  key: string;
  name: string;
  colour: string;
  kind?: "line" | "area" | "step";
  stack?: string;
  width?: number;
  muted?: boolean;
  /** sparse checkpoints: straight segments with a marker on every point */
  dots?: boolean;
}

interface TTProps {
  active?: boolean;
  label?: number;
  payload?: { dataKey?: string | number; value?: number | null; color?: string }[];
  series: SeriesSpec[];
  unit: string;
  digits: number;
  xLabel: (t: number) => string;
}

function ChartTooltip({ active, label, payload, series, unit, digits, xLabel }: TTProps) {
  if (!active || !payload?.length) return null;
  const byKey = new Map(series.map((s) => [s.key, s]));
  const rows = payload
    .filter((p) => p.value !== null && p.value !== undefined && byKey.has(String(p.dataKey)))
    .map((p) => ({ s: byKey.get(String(p.dataKey))!, v: p.value as number }));
  if (!rows.length) return null;
  return (
    <div className="tooltip">
      <div className="tt-title">{xLabel(label ?? 0)}</div>
      {rows.map(({ s, v }) => (
        <div className="tt-row" key={s.key}>
          <span>
            <i style={{ width: 10, height: 3, borderRadius: 2, background: s.colour, display: "inline-block" }} />
            {s.name}
          </span>
          <b>
            {v.toFixed(digits)} {unit}
          </b>
        </div>
      ))}
    </div>
  );
}

export function TimeChart({
  data,
  series,
  unit,
  height = 220,
  digits = 0,
  refs = [],
  yMax,
  yMin = 0,
  log = false,
  xKey = "t",
  xLabel = (t: number) => `t = ${Math.round(t)} s`,
  legend = true,
  extraLegend = [],
  xRefs = [],
  xTick = (t: number) => `${Math.round(t)}s`,
  niceY = false,
}: {
  data: Record<string, number | null>[];
  series: SeriesSpec[];
  unit: string;
  height?: number;
  digits?: number;
  refs?: { y: number; label: string }[];
  yMax?: number | "auto";
  yMin?: number;
  log?: boolean;
  xKey?: string;
  xLabel?: (t: number) => string;
  legend?: boolean;
  extraLegend?: { label: string; colour: string; box?: boolean; dash?: boolean }[];
  xRefs?: { x: number; label: string }[];
  xTick?: (t: number) => string;
  /** fit evenly spaced ticks to the data and reference lines instead of Recharts' defaults */
  niceY?: boolean;
}) {
  const { pal } = useTheme();
  let domain: [number | string, number | string] = [yMin, yMax === undefined || yMax === "auto" ? "auto" : yMax];
  let yTicks: number[] | undefined;
  if (niceY && !log) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const row of data) {
      for (const s of series) {
        const v = row[s.key];
        if (typeof v === "number") {
          lo = Math.min(lo, v);
          hi = Math.max(hi, v);
        }
      }
    }
    for (const r of refs) {
      lo = Math.min(lo, r.y);
      hi = Math.max(hi, r.y);
    }
    if (lo <= hi) {
      yTicks = linTicks(lo, hi);
      domain = [yTicks[0], yTicks[yTicks.length - 1]];
    }
  }
  if (log) {
    // Explicit domain around the data: Recharts' auto log domain can end below the peak and clip it.
    let lo = Infinity;
    let hi = -Infinity;
    for (const row of data) {
      for (const s of series) {
        const v = row[s.key];
        if (typeof v === "number" && v > 0) {
          lo = Math.min(lo, v);
          hi = Math.max(hi, v);
        }
      }
    }
    for (const r of refs) if (r.y > 0) hi = Math.max(hi, r.y);
    if (hi > 0 && lo <= hi) {
      domain = [lo * 0.9, hi * 1.1];
      yTicks = logTicks(lo * 0.9, hi * 1.1);
    }
  }
  // Event markers are labelled in the top margin; near the right edge a label flips to the left
  // of its line, and one that would overlap the previous label drops to a row inside the plot.
  const t0 = data.length ? Number(data[0][xKey]) : 0;
  const t1 = data.length ? Number(data[data.length - 1][xKey]) : 0;
  const placed: { l: number; r: number; row: number }[] = [];
  // A reference label sits above its line unless data in the right part of the plot (where the
  // label is drawn) runs through that band; then it goes below, if that side is clear.
  const tf = (v: number) => (log ? Math.log10(Math.max(v, 1e-9)) : v);
  let spanLo = Infinity;
  let spanHi = -Infinity;
  if (typeof domain[0] === "number" && typeof domain[1] === "number") {
    spanLo = domain[0];
    spanHi = domain[1];
  } else {
    for (const row of data) {
      for (const s of series) {
        const v = row[s.key];
        if (typeof v === "number") {
          spanLo = Math.min(spanLo, v);
          spanHi = Math.max(spanHi, v);
        }
      }
    }
    for (const r of refs) {
      spanLo = Math.min(spanLo, r.y);
      spanHi = Math.max(spanHi, r.y);
    }
    if (!log) spanLo = Math.min(spanLo, yMin);
  }
  const band = ((tf(spanHi) - tf(spanLo)) * 15) / Math.max(40, height - 30);
  const refBelow = (y: number) => {
    const ty = tf(y);
    const xCut = t0 + (t1 - t0) * 0.7;
    let above = false;
    let below = false;
    for (const row of data) {
      if (Number(row[xKey]) < xCut) continue;
      for (const s of series) {
        const v = row[s.key];
        if (typeof v !== "number") continue;
        const tv = tf(v);
        if (tv > ty && tv <= ty + band) above = true;
        if (tv < ty && tv >= ty - band) below = true;
      }
    }
    return above && !below;
  };
  const eventLabel = (r: { x: number; label: string }, i: number) =>
    function EventLabel(p: { viewBox?: { x: number; y: number } }) {
      if (!p.viewBox) return <g />;
      const w = r.label.length * 5.8;
      const flip = t1 > t0 && (r.x - t0) / (t1 - t0) > 0.7;
      const x = flip ? p.viewBox.x - 4 : p.viewBox.x + 4;
      const l = flip ? x - w : x;
      const clash = placed.slice(0, i).some((q) => q.row === 0 && l < q.r + 8 && l + w > q.l - 8);
      placed[i] = { l, r: l + w, row: clash ? 1 : 0 };
      return (
        <text x={x} y={clash ? p.viewBox.y + 12 : p.viewBox.y - 5} textAnchor={flip ? "end" : "start"} fill={pal.muted} fontSize={10.5}>
          {r.label}
        </text>
      );
    };
  return (
    <div>
      {legend && (
        <div style={{ marginBottom: 6 }}>
          <Legend
            items={series
              .filter((s) => !s.muted)
              .map((s): LegendItem => ({ label: s.name, colour: s.colour, box: s.kind === "area" }))
              .concat(extraLegend)}
          />
        </div>
      )}
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={data} margin={{ top: xRefs.length ? 18 : 6, right: 14, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={pal.grid} vertical={false} />
          <XAxis
            dataKey={xKey}
            type="number"
            domain={["dataMin", "dataMax"]}
            tickFormatter={xTick}
            stroke={pal.axis}
            tick={{ fill: pal.muted, fontSize: 11 }}
            tickLine={false}
            minTickGap={36}
          />
          <YAxis
            scale={log ? "log" : "auto"}
            domain={domain}
            ticks={yTicks}
            interval={yTicks ? 0 : "preserveEnd"}
            allowDataOverflow={log}
            stroke={pal.axis}
            tick={{ fill: pal.muted, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={46}
            tickFormatter={(v: number) => (v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}K` : `${+v.toFixed(2)}`)}
          />
          <Tooltip
            cursor={{ stroke: pal.axis, strokeWidth: 1 }}
            isAnimationActive={false}
            content={<ChartTooltip series={series} unit={unit} digits={digits} xLabel={xLabel} />}
          />
          {refs.map((r) => {
            const below = refBelow(r.y);
            return (
              <ReferenceLine
                key={r.label}
                y={r.y}
                stroke={pal.muted}
                strokeWidth={1}
                strokeOpacity={0.8}
                ifOverflow="extendDomain"
                label={(p: { viewBox?: { x: number; y: number; width: number } }) =>
                  p.viewBox ? (
                    <text x={p.viewBox.x + p.viewBox.width - 4} y={below ? p.viewBox.y + 13 : p.viewBox.y - 5} textAnchor="end" fill={pal.muted} fontSize={11}>
                      {r.label}
                    </text>
                  ) : (
                    <g />
                  )
                }
              />
            );
          })}
          {xRefs.map((r, i) => (
            <ReferenceLine
              key={`x-${r.x}-${i}`}
              x={r.x}
              stroke={pal.axis}
              strokeWidth={1}
              label={eventLabel(r, i)}
            />
          ))}
          {series.map((s) =>
            s.kind === "area" ? (
              <Area
                key={s.key}
                type="monotone"
                dataKey={s.key}
                name={s.name}
                stackId={s.stack}
                stroke={s.colour}
                strokeWidth={1.5}
                fill={s.colour}
                fillOpacity={0.32}
                isAnimationActive={false}
                connectNulls
                dot={false}
                activeDot={false}
              />
            ) : (
              <Line
                key={s.key}
                type={s.kind === "step" ? "stepAfter" : s.dots ? "linear" : "monotone"}
                dataKey={s.key}
                name={s.name}
                stroke={s.colour}
                strokeWidth={s.width ?? 2}
                strokeOpacity={s.muted ? 0.45 : 1}
                dot={s.dots ? { r: 4, fill: s.colour, stroke: pal.surface, strokeWidth: 2 } : false}
                activeDot={s.muted ? false : { r: s.dots ? 5 : 4, stroke: pal.surface, strokeWidth: 2 }}
                isAnimationActive={false}
                connectNulls
              />
            ),
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
