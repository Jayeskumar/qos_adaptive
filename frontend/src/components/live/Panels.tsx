import { useMemo, useState } from "react";
import type { Capabilities, ClassStatus, Decision, FlowRow, SideSnap } from "../../lib/types";
import type { LiveRow } from "../../lib/useLive";
import { classColour, useTheme } from "../../lib/theme";
import { fmt, isNum, mbps } from "../../lib/format";
import { APP_ICON } from "../../lib/qoe";
import { Icon } from "../Icon";
import { Card, Empty, Pill, Seg } from "../ui";

const KIND: Record<string, { icon: string; label: string }> = {
  classify: { icon: "brain", label: "Classified" },
  allocate: { icon: "sliders", label: "Allocation" },
  autorate: { icon: "gauge", label: "Autorate" },
  protect: { icon: "shield", label: "Protection" },
  policy: { icon: "cog", label: "Policy" },
};

export function DecisionFeed({ decisions, title }: { decisions: Decision[]; title: string }) {
  const [filter, setFilter] = useState<string>("all");
  const shown = decisions.filter((d) => filter === "all" || d.kind === filter).slice(0, 80);
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const d of decisions) c[d.kind] = (c[d.kind] ?? 0) + 1;
    return c;
  }, [decisions]);
  return (
    <Card
      title={title}
      icon="activity"
      sub="Every action the controller takes, in plain language"
      right={
        <select className="select" style={{ height: 28, minWidth: 0, fontSize: 12 }} value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="all">All ({decisions.length})</option>
          {Object.keys(KIND).map((k) => (
            <option key={k} value={k}>{KIND[k].label} ({counts[k] ?? 0})</option>
          ))}
        </select>
      }
    >
      {shown.length === 0 ? (
        <Empty icon="brain" title="No decisions yet">Decisions appear as flows are classified and bandwidth is re-allocated.</Empty>
      ) : (
        <div className={`feed${shown.length > 5 ? " fade" : ""}`}>
          {shown.map((d, i) => {
            const k = KIND[d.kind] ?? { icon: "info", label: d.kind };
            const why = (d.details as { why?: string } | undefined)?.why;
            return (
              <div className="feed-item" key={`${d.t}-${i}-${d.message.slice(0, 20)}`}>
                <div className={`feed-icon kind-${d.kind}`}><Icon name={k.icon} size={14} /></div>
                <div>
                  <div className="feed-msg">{d.message}</div>
                  {why && <div className="feed-why">Why: {why}</div>}
                  <div className="feed-meta"><span>{k.label}</span><span className="num">t = {fmt(d.t, 1)} s</span></div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

export function FlowTable({ flows, caps, accuracy }: { flows: FlowRow[]; caps: Capabilities; accuracy?: number | null }) {
  const { pal } = useTheme();
  const rows = flows.slice(0, 40);
  return (
    <Card
      title="Flows the AI is managing"
      icon="list"
      sub="Behavioural classification - no payload or port inspection"
      right={isNum(accuracy) ? <span className="pill" style={{ background: "var(--accent-wash)" }}><Icon name="target" size={13} />{(accuracy * 100).toFixed(1)}% match ground truth</span> : undefined}
    >
      {rows.length === 0 ? (
        <Empty icon="list" title="No flows yet" />
      ) : (
        <div className="table-wrap">
          <table className="t">
            <thead>
              <tr>
                <th>Device · flow</th>
                <th>Detected application</th>
                <th>Class</th>
                <th className="r">Mb/s ↓ / ↑</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((f) => {
                const meta = caps.classes[String(f.tc >= 50 ? 50 : f.tc)];
                return (
                  <tr key={f.key.join("|")} title={f.explanation || undefined}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{f.device}</div>
                      <div className="mono muted">{f.proto.toUpperCase()} :{f.wan_port}</div>
                    </td>
                    <td>
                      <div className="conf">
                        {f.label && <Icon name={APP_ICON[f.label] ?? "activity"} size={13} style={{ color: "var(--muted)" }} />}
                        <span>{f.label_name}</span>
                      </div>
                      {f.label && (
                        <div className="conf" style={{ marginTop: 3, display: "flex" }}>
                          <span className="conf-bar"><i style={{ width: `${Math.round(f.confidence * 100)}%` }} /></span>
                          <span className="small muted num">{Math.round(f.confidence * 100)}% sure</span>
                        </div>
                      )}
                    </td>
                    <td>
                      <Pill colour={classColour(pal, f.tc)} title={meta ? `HTB prio ${meta.prio}, DSCP ${meta.dscp}, ${meta.wmm}` : undefined}>
                        {f.class_name}
                        {f.demoted ? " (protected)" : ""}
                      </Pill>
                    </td>
                    <td className="r">
                      <div>{(f.kbps_down / 1000).toFixed(2)}</div>
                      <div className="muted small">{(f.kbps_up / 1000).toFixed(2)}</div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="footer-note">Hover a row for the classifier's explanation.</div>
    </Card>
  );
}

const CLASS_ORDER = ["10", "20", "30", "40", "50"];

export function ClassAlloc({ snap }: { snap: SideSnap }) {
  const { pal } = useTheme();
  const [dir, setDir] = useState<"down" | "up">("down");
  const classes = snap.classes?.[dir] ?? {};
  const cap = (dir === "down" ? snap.shaper.down : snap.shaper.up) ?? (dir === "down" ? snap.capacity.down : snap.capacity.up);
  const rows = CLASS_ORDER.map((k) => [k, classes[k]] as [string, ClassStatus | undefined]).filter(([, c]) => !!c);
  return (
    <Card
      title="Allocation by class"
      icon="layers"
      sub={`Guarantee (soft) vs use (solid) · shaper ${mbps(cap, 1)} Mb/s`}
      right={<Seg value={dir} onChange={(v) => setDir(v)} options={[{ value: "down", label: "Downlink" }, { value: "up", label: "Uplink" }]} />}
    >
      {rows.length === 0 ? (
        <Empty icon="layers" title="Allocation appears once FlowPilot is running" />
      ) : (
        <div className="alloc">
          {rows.map(([k, c]) => {
            const col = classColour(pal, Number(k));
            const scale = Math.max(cap ?? 1, 1);
            const g = Math.min(100, (c!.rate / scale) * 100);
            const s = Math.min(100, (c!.served / scale) * 100);
            return (
              <div key={k}>
                <div className="alloc-row">
                  <div className="alloc-name"><span className="pdot" style={{ width: 9, height: 9, borderRadius: 3, background: col }} />{c!.name}</div>
                  <div className="alloc-track" title={`guaranteed ${mbps(c!.rate, 2)} Mb/s, ceiling ${mbps(c!.ceil, 1)} Mb/s`}>
                    <div className="alloc-guar" style={{ width: `${g}%`, background: col }} />
                    <div className="alloc-served" style={{ width: `${s}%`, background: col }} />
                  </div>
                </div>
                <div className="alloc-row" style={{ marginTop: 2 }}>
                  <span />
                  <div className="alloc-meta">
                    <span>using <b className="num" style={{ color: "var(--ink)" }}>{mbps(c!.served, 1)}</b> / guaranteed {mbps(c!.rate, 1)} Mb/s</span>
                    <span>queue {fmt(c!.queue_delay_ms, 0)} ms</span>
                    <span>{c!.flows} flow{c!.flows === 1 ? "" : "s"}</span>
                    {c!.saturated && <span>· wants more</span>}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// Non-breaking space keeps a value and its unit on the same line.
const ms = (v: number) => `${v.toFixed(0)}\u00a0ms`;

function mean(xs: (number | null)[]): number | null {
  const v = xs.filter(isNum);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

export function Hero({ history, labels }: { history: LiveRow[]; labels: { a: string; b: string } }) {
  const recent = history.slice(-10);
  const ga = mean(recent.map((r) => r.game_a));
  const gb = mean(recent.map((r) => r.game_b));
  const ca = mean(recent.map((r) => r.call_a));
  const cb = mean(recent.map((r) => r.call_b));
  let figure: string | null = null;
  let text = (
    <>
      Same household, same events, two routers. <strong>Trigger an event</strong> - for example <strong>Steam update</strong> -
      and watch the standard router's latency explode while FlowPilot keeps the game and the call responsive.
    </>
  );
  if (isNum(ga) && isNum(gb) && gb > 0 && ga / gb >= 1.25) {
    figure = `${(ga / gb).toFixed(1)}×`;
    text = (
      <>
        <strong>lower gaming latency with FlowPilot</strong> over the last 10 s: in-game ping p95 {ms(ga)} on {labels.a} vs{" "}
        <strong>{ms(gb)}</strong> with FlowPilot
        {isNum(ca) && isNum(cb) ? <> · video-call delay {ms(ca)} vs <strong>{ms(cb)}</strong></> : null}.
      </>
    );
  } else if (isNum(ca) && isNum(cb) && cb > 0 && ca / cb >= 1.25) {
    figure = `${(ca / cb).toFixed(1)}×`;
    text = (
      <>
        <strong>lower video-call delay with FlowPilot</strong> over the last 10 s: {ms(ca)} on {labels.a} vs <strong>{ms(cb)}</strong>.
      </>
    );
  }
  return (
    <div className="hero">
      {figure ? <div className="hero-figure num">{figure}</div> : <Icon name="zap" size={28} style={{ color: "var(--accent-ink)" }} />}
      <div className="hero-text">{text}</div>
    </div>
  );
}
