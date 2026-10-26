import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { ClassifierCard, RlReport } from "../lib/types";
import { ApiError, getJSON } from "../lib/api";
import { useTheme } from "../lib/theme";
import { APP_NAME } from "../lib/qoe";
import { compact, isNum } from "../lib/format";
import { Card, Empty, Stat, tipLabel } from "../components/ui";
import { APP_SHORT } from "../lib/labels";
import { TimeChart } from "../components/Chart";
import { Icon } from "../components/Icon";

const FEATURE_LABEL: Record<string, string> = {
  is_udp: "UDP transport",
  log_age_s: "Flow age",
  log_pps_up: "Packet rate up",
  log_pps_down: "Packet rate down",
  log_kbps_up: "Bit rate up",
  log_kbps_down: "Bit rate down",
  mean_size_up: "Mean packet size up",
  mean_size_down: "Mean packet size down",
  std_size_up: "Packet size spread up",
  std_size_down: "Packet size spread down",
  frac_small_up: "Share of tiny packets up",
  frac_large_down: "Share of full packets down",
  frac_large_up: "Share of full packets up",
  frac_small_down: "Share of tiny packets down",
  iat_cv_up: "Inter-arrival regularity up",
  iat_cv_down: "Inter-arrival regularity down",
  log_iat_mean_up_ms: "Mean inter-arrival up",
  log_iat_mean_down_ms: "Mean inter-arrival down",
  burst_cv_up: "Burstiness up",
  burst_cv_down: "Burstiness down (ON/OFF)",
  on_frac_up: "Active time share up",
  on_frac_down: "Active time share down",
  log_ratio_up_down: "Up/down byte ratio",
  log_peak_kbps_down: "Peak 100 ms rate down",
  log_host_flows: "Device's concurrent flows",
  log_host_same_dst: "Parallel connections to same server",
};

function HBars({ data, colour, digits = 1, unit = "", height, domain }: {
  data: { name: string; v: number; colour?: string }[]; colour: string; digits?: number; unit?: string; height?: number; domain?: [number, number];
}) {
  const { pal } = useTheme();
  return (
    <ResponsiveContainer width="100%" height={height ?? data.length * 28 + 24}>
      <BarChart data={data} layout="vertical" margin={{ top: 0, right: 56, bottom: 0, left: 0 }} barCategoryGap={5}>
        <CartesianGrid stroke={pal.grid} horizontal={false} />
        <XAxis type="number" domain={domain ?? [0, "auto"]} stroke={pal.axis} tick={{ fill: pal.muted, fontSize: 11 }} tickLine={false} axisLine={false} />
        <YAxis type="category" dataKey="name" width={210} tick={{ fill: pal.ink2, fontSize: 11.5 }} tickLine={false} axisLine={false} />
        <Tooltip
          cursor={{ fill: pal.grid, fillOpacity: 0.5 }}
          isAnimationActive={false}
          content={({ active, payload }) =>
            active && payload?.length ? (
              <div className="tooltip">
                <div className="tt-title">{String(payload[0].payload.name)}</div>
                <div className="tt-row"><span>value</span><b>{(payload[0].value as number).toFixed(digits)}{unit}</b></div>
              </div>
            ) : null
          }
        />
        <Bar dataKey="v" barSize={14} radius={[0, 4, 4, 0]} isAnimationActive={false}>
          {data.map((d) => <Cell key={d.name} fill={d.colour ?? colour} />)}
          <LabelList dataKey="v" content={tipLabel((v) => `${v.toFixed(digits)}${unit}`, pal.ink2)} />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function Confusion({ card }: { card: ClassifierCard }) {
  const { pal } = useTheme();
  const m = card.test.confusion;
  const labels = card.labels;
  return (
    <div className="table-wrap" style={{ maxHeight: "none" }}>
      <table className="t heat">
        <thead>
          <tr>
            <th>True \ predicted</th>
            {labels.map((l) => <th key={l} className="r" title={APP_NAME[l] ?? l}>{APP_SHORT[l] ?? l}</th>)}
            <th className="r">Recall</th>
          </tr>
        </thead>
        <tbody>
          {m.map((row, i) => {
            const tot = row.reduce((a, b) => a + b, 0) || 1;
            return (
              <tr key={labels[i]}>
                <td style={{ fontWeight: 600 }}>{APP_NAME[labels[i]] ?? labels[i]}</td>
                {row.map((v, j) => {
                  const f = v / tot;
                  const bg = f > 0 ? `color-mix(in srgb, ${pal.flowpilot} ${Math.round(12 + 88 * f)}%, var(--surface-2))` : undefined;
                  return (
                    <td key={j} className="cell num" style={{ background: bg, color: f > 0.5 ? "#fff" : undefined }} title={`${v} samples (${(f * 100).toFixed(1)}% of true ${labels[i]})`}>
                      {v ? compact(v) : "·"}
                    </td>
                  );
                })}
                <td className="r num">{(card.test.per_class[labels[i]].recall * 100).toFixed(1)}%</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const REALTIME = new Set(["gaming", "video_call"]);

// The largest off-diagonal cells of the confusion matrix, and whether any error touches a real-time class.
function Mistakes({ card }: { card: ClassifierCard }) {
  const m = card.test.confusion;
  const L = card.labels;
  const errs: { t: string; p: string; v: number; f: number }[] = [];
  m.forEach((row, i) => {
    const tot = row.reduce((a, b) => a + b, 0) || 1;
    row.forEach((v, j) => {
      if (i !== j && v > 0) errs.push({ t: L[i], p: L[j], v, f: v / tot });
    });
  });
  errs.sort((a, b) => b.v - a.v);
  const total = m.flat().reduce((a, b) => a + b, 0) || 1;
  const wrong = errs.reduce((a, e) => a + e.v, 0);
  const rtClean = !errs.some((e) => REALTIME.has(e.t) || REALTIME.has(e.p));
  if (!errs.length) return null;
  return (
    <div style={{ marginTop: 16 }}>
      <div className="card-sub" style={{ marginBottom: 8 }}>
        Where it still errs: {compact(wrong)} of {compact(total)} samples ({((wrong / total) * 100).toFixed(2)}%), largest first
      </div>
      <div className="mistakes">
        {errs.slice(0, 3).map((e) => (
          <div className="mistake" key={`${e.t}-${e.p}`}>
            <b>{APP_NAME[e.t] ?? e.t}</b>
            <span className="muted">taken for</span>
            <b>{APP_NAME[e.p] ?? e.p}</b>
            <span className="m-share">{e.v} samples · {(e.f * 100).toFixed(1)}% of true</span>
          </div>
        ))}
      </div>
      {rtClean && (
        <div className="banner" style={{ marginTop: 10, fontSize: 12.5 }}>
          <Icon name="shield" size={15} />
          No game or call flow was mistaken for anything else, and nothing was mistaken for one: the errors only move bandwidth between non-real-time classes.
        </div>
      )}
    </div>
  );
}

function ClassifierSection({ card }: { card: ClassifierCard }) {
  const { pal } = useTheme();
  const imp = card.importance.slice(0, 12).map((d) => ({ name: FEATURE_LABEL[d.feature] ?? d.feature, v: d.importance * 100 }));
  const ages = card.test.by_age.map((b) => ({
    name: b.age_hi ? (b.age_lo === 0 ? `<= ${b.age_hi} s` : `${b.age_lo}-${b.age_hi} s`) : `> ${b.age_lo} s`,
    v: b.accuracy * 100,
  }));
  const cmp = [
    { name: "Hand-written rules", v: card.rule_based.accuracy * 100, colour: pal.neutral },
    { name: "Random forest", v: card.random_forest.accuracy * 100, colour: pal.neutral },
    { name: "Boosted trees (deployed)", v: card.test.accuracy * 100, colour: pal.flowpilot },
  ];
  return (
    <div className="stack">
      <div>
        <div className="section-title">Learning which applications need bandwidth</div>
        <p className="section-sub">
          A gradient-boosted tree classifier reads the <b>shape</b> of each flow - packet sizes, rates, timing regularity,
          ON/OFF burstiness, parallel connections - never payloads or port numbers, so it keeps working on encrypted TLS/QUIC
          traffic. It is evaluated on <b>households it never saw in training</b>.
        </p>
      </div>
      <div className="stat-tiles">
        <Stat label="Accuracy (unseen homes)" value={`${(card.test.accuracy * 100).toFixed(2)}%`} sub={`macro-F1 ${card.test.macro_f1.toFixed(3)}`} />
        <Stat label="Training data" value={compact(card.n_samples)} sub={`flow snapshots from ${card.n_households} households`} />
        <Stat label="Classified within 1 s" value={`${((card.test.by_age[0]?.accuracy ?? 0) * 100).toFixed(1)}%`} sub="on flows younger than 1 s" />
        <Stat label="Inference cost" value={`${(card.inference_ms_per_64_flows / 64).toFixed(2)} ms`} sub="per flow, single CPU thread" />
        <Stat label="Input" value={`${card.features.length} features`} sub="behavioural, port-agnostic" />
      </div>
      <div className="grid-2">
        <Card title="Confusion matrix" sub={`${compact(card.n_test)} held-out samples · darker = larger share of the true class`} icon="target">
          <Confusion card={card} />
          <Mistakes card={card} />
        </Card>
        <div className="stack">
          <Card title="Model comparison" sub="Accuracy on the same held-out households (%)" icon="flask">
            <HBars data={cmp} colour={pal.flowpilot} digits={1} unit="%" domain={[0, 100]} />
          </Card>
          <Card title="Accuracy by flow age" sub="How quickly a new flow is recognised (%)" icon="clock">
            <HBars data={ages} colour={pal.flowpilot} digits={1} unit="%" domain={[0, 100]} />
          </Card>
        </div>
      </div>
      <div className="grid-2">
        <Card title="What the model looks at" sub="Permutation importance: accuracy lost when a feature is shuffled (percentage points)" icon="brain">
          <HBars data={imp} colour={pal.flowpilot} digits={1} unit=" pp" />
        </Card>
        <Card title="Per-class quality" sub="Precision, recall and F1 on held-out households" icon="list">
          <div className="table-wrap" style={{ maxHeight: "none" }}>
            <table className="t">
              <thead>
                <tr><th>Application</th><th className="r">Precision</th><th className="r">Recall</th><th className="r">F1</th><th className="r">Test samples</th><th className="r">Train share</th></tr>
              </thead>
              <tbody>
                {card.labels.map((l) => {
                  const pc = card.test.per_class[l];
                  const tot = Object.values(card.class_counts).reduce((a, b) => a + b, 0);
                  return (
                    <tr key={l}>
                      <td>{APP_NAME[l] ?? l}</td>
                      <td className="r">{(pc.precision * 100).toFixed(1)}%</td>
                      <td className="r">{(pc.recall * 100).toFixed(1)}%</td>
                      <td className="r">{pc.f1.toFixed(3)}</td>
                      <td className="r">{compact(pc.support)}</td>
                      <td className="r">{((card.class_counts[l] / tot) * 100).toFixed(1)}%</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="footer-note">Trained {card.trained_at} in {card.train_seconds.toFixed(1)} s · {card.model}</div>
        </Card>
      </div>
    </div>
  );
}

const POLICY_LABEL: Record<string, string> = { static: "Static shaper (95 %)", heuristic: "Hand-tuned autorate", rl: "RL autorate (DQN)" };
const KIND_LABEL: Record<string, string> = { fixed: "Fixed line + congestion", fwa: "5G fixed wireless", lte: "LTE-like regimes" };

function RlSection({ rep }: { rep: RlReport }) {
  const { pal } = useTheme();
  const hist = rep.training?.history ?? [];
  const evalCurve = rep.training?.eval_curve ?? [];
  const evalRef = rep.training?.eval_reference;
  // Greedy policy at each checkpoint on fixed held-out traces: the honest learning curve when present.
  // Utilisation and time over 20 ms are shown rather than the scalar reward, which an under-using
  // policy can score well on; the reward is kept only as a fallback.
  const learn = evalCurve.map((e) => ({
    t: (e.transitions ?? e.step) / 1000,
    rl: isNum(e.mean_reward) ? e.mean_reward : null,
    util: isNum(e.utilization) ? e.utilization * 100 : null,
    over: isNum(e.frac_over_20ms) ? e.frac_over_20ms * 100 : null,
  }));
  const hasMetrics = learn.filter((l) => isNum(l.util) && isNum(l.over)).length >= 2;
  const kTick = (t: number) => `${Math.round(t)}K`;
  const kLabel = (t: number) => `${t.toFixed(0)}K transitions`;
  // Fallback: mean return of recent exploration episodes (noisy by design) with a trailing 7-point median.
  const curve = useMemo(() => {
    const out: Record<string, number | null>[] = [];
    for (let i = 0; i < hist.length; i++) {
      const w = hist.slice(Math.max(0, i - 6), i + 1).map((h) => h.episode_return).filter(isNum).sort((a, b) => a - b);
      const n = w.length;
      const med = n < 3 ? null : n % 2 ? w[(n - 1) / 2] : (w[n / 2 - 1] + w[n / 2]) / 2;
      out.push({ t: (hist[i].transitions ?? hist[i].step) / 1000, raw: isNum(hist[i].episode_return) ? hist[i].episode_return : null, ret: med });
    }
    return out;
  }, [hist]);
  const ev = rep.eval ?? [];
  const kinds = ev.length ? Object.keys(ev[0].by_kind) : [];
  return (
    <div className="stack">
      <div>
        <div className="section-title">Learning the link: reinforcement-learning autorate</div>
        <p className="section-sub">
          A Double-DQN agent sets the router's shaper every 200 ms from probe delays and its own throughput, trained on a fast
          fluid model of fixed-line, 5G-FWA and LTE capacity traces, and deployed inside a safety shield. Below: evaluation on
          held-out link traces against a static shaper and the hand-tuned (cake-autorate-style) controller.
        </p>
      </div>
      <div className="stat-tiles">
        {ev.map((e) => (
          <Stat key={e.policy} label={POLICY_LABEL[e.policy] ?? e.policy} value={`${(e.utilization * 100).toFixed(1)}%`}
            sub={`utilisation · queue p95 ${e.delay_p95_ms.toFixed(0)} ms · >20 ms ${(e.frac_over_20ms * 100).toFixed(1)}% of time · reward ${e.mean_reward.toFixed(3)}`} />
        ))}
      </div>
      <div className="grid-2">
        {learn.length >= 2 ? (
          <Card title="Learning curve" sub="Greedy policy at each checkpoint on the same held-out link traces; grey line = hand-tuned autorate" icon="activity">
            {hasMetrics ? (
              <div className="stack" style={{ gap: 10 }}>
                <div>
                  <div className="mini-title">Link utilisation · higher is better</div>
                  <TimeChart
                    data={learn}
                    series={[{ key: "util", name: "RL autorate (DQN)", colour: pal.flowpilot, dots: true }]}
                    unit="%"
                    digits={1}
                    niceY
                    refs={isNum(evalRef?.utilization) ? [{ y: evalRef!.utilization! * 100, label: `Hand-tuned ${(evalRef!.utilization! * 100).toFixed(0)} %` }] : []}
                    legend={false}
                    height={150}
                    xLabel={kLabel}
                    xTick={kTick}
                  />
                </div>
                <div>
                  <div className="mini-title">Time with queueing delay over 20 ms · lower is better</div>
                  <TimeChart
                    data={learn}
                    series={[{ key: "over", name: "RL autorate (DQN)", colour: pal.flowpilot, dots: true }]}
                    unit="%"
                    digits={1}
                    niceY
                    refs={isNum(evalRef?.frac_over_20ms) ? [{ y: evalRef!.frac_over_20ms! * 100, label: `Hand-tuned ${(evalRef!.frac_over_20ms! * 100).toFixed(0)} %` }] : []}
                    legend={false}
                    height={150}
                    xLabel={kLabel}
                    xTick={kTick}
                  />
                </div>
              </div>
            ) : (
              <TimeChart
                data={learn}
                series={[{ key: "rl", name: "RL autorate (DQN)", colour: pal.flowpilot, dots: true }]}
                unit=""
                digits={3}
                niceY
                refs={isNum(evalRef?.mean_reward) ? [{ y: evalRef!.mean_reward, label: "Hand-tuned autorate" }] : []}
                legend={false}
                height={240}
                xLabel={kLabel}
                xTick={kTick}
              />
            )}
          </Card>
        ) : (
          <Card title="Training curve" sub="Mean return of recent exploration (ε-greedy) episodes - noisy by design; line = trailing 7-point median, faint = raw" icon="activity">
            {curve.length ? (
              <TimeChart
                data={curve}
                series={[
                  { key: "raw", name: "Recent episodes", colour: pal.flowpilot, width: 1, muted: true },
                  { key: "ret", name: "Rolling median", colour: pal.flowpilot },
                ]}
                unit=""
                digits={1}
                niceY
                legend={false}
                height={240}
                xLabel={(t) => `${t.toFixed(0)}K transitions`}
                xTick={(t) => `${Math.round(t)}K`}
              />
            ) : (
              <Empty icon="activity" title="No training history in the report" />
            )}
          </Card>
        )}
        <Card title="Per link type" sub="Utilisation (higher is better) and ISP queueing delay p95 (lower is better)" icon="gauge">
          <div className="table-wrap" style={{ maxHeight: "none" }}>
            <table className="t">
              <thead>
                <tr>
                  <th>Link type</th>
                  {ev.map((e) => <th key={e.policy} className="r">{POLICY_LABEL[e.policy] ?? e.policy}</th>)}
                </tr>
              </thead>
              <tbody>
                {kinds.map((k) => (
                  <tr key={k}>
                    <td>{KIND_LABEL[k] ?? k}</td>
                    {ev.map((e) => (
                      <td key={e.policy} className="r">
                        {(e.by_kind[k].utilization * 100).toFixed(1)}% · {e.by_kind[k].delay_p95_ms.toFixed(0)} ms
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  );
}

export function ModelsPage() {
  const [card, setCard] = useState<ClassifierCard | null>(null);
  const [rep, setRep] = useState<RlReport | null>(null);
  const [cErr, setCErr] = useState<string | null>(null);
  const [rErr, setRErr] = useState<string | null>(null);
  useEffect(() => {
    getJSON<ClassifierCard>("/api/models/classifier").then(setCard).catch((e) => setCErr(e instanceof ApiError && e.status === 404 ? "not trained yet" : String(e)));
    getJSON<RlReport>("/api/models/rl").then(setRep).catch((e) => setRErr(e instanceof ApiError && e.status === 404 ? "training in progress" : String(e)));
  }, []);
  return (
    <div className="stack" style={{ gap: 28 }}>
      {card ? <ClassifierSection card={card} /> : <Card><Empty icon="brain" title="Classifier model card unavailable">{cErr ?? "Loading..."}</Empty></Card>}
      {rep && rep.eval?.length ? (
        <RlSection rep={rep} />
      ) : (
        <Card>
          <Empty icon="gauge" title="RL autorate report not available yet">
            {rErr === "training in progress" || !rErr
              ? "The DQN agent is still training - the evaluation appears here when it finishes."
              : rErr}
          </Empty>
        </Card>
      )}
    </div>
  );
}
