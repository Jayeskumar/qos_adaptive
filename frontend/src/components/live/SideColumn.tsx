import type { Capabilities, SideSnap, AppSnap } from "../../lib/types";
import type { LiveRow } from "../../lib/useLive";
import { appStatus, deviceStatus, DEVICE_ICON, isFlowPilot, mosStatus, STATUS_LABEL, type Status } from "../../lib/qoe";
import { fmt, isNum, mbps } from "../../lib/format";
import { Icon } from "../Icon";
import { StatusBadge } from "../ui";

const INTENT_LABEL: Record<string, string> = {
  balanced: "Balanced",
  gaming: "Gaming first",
  work_from_home: "Work from home",
  movie_night: "Movie night",
};

function Topology({ snap, caps }: { snap: SideSnap; caps: Capabilities }) {
  const qd = snap.isp_queue_ms.down ?? 0;
  const qu = snap.isp_queue_ms.up ?? 0;
  const q = Math.max(qd, qu);
  const hot = q >= 50 ? "hot" : q >= 15 ? "warm" : "";
  const shaper = snap.shaper.down;
  return (
    <div className="topo" style={{ gridTemplateColumns: "auto minmax(8px,0.5fr) auto minmax(8px,0.6fr) auto minmax(8px,0.4fr) minmax(0,3.2fr)" }}>
      <div className="topo-cloud" title="Internet servers: game, call, CDN, download mirror, web, backup">
        <Icon name="cloud" size={20} />
        <span>Internet</span>
      </div>
      <div className={`topo-link ${hot}`} />
      <div className="topo-node" title="ISP access link: true capacity and its (deep) buffer">
        <span className="n-title"><Icon name="link" size={12} />ISP link</span>
        <span className="n-value">{mbps(snap.capacity.down, 0)}/{mbps(snap.capacity.up, 0)} Mb/s</span>
        <span className="n-sub" style={{ color: hot === "hot" ? "var(--ink)" : undefined }}>
          queue {fmt(qd, 0)} ↓ · {fmt(qu, 0)} ↑ ms
        </span>
      </div>
      <div className={`topo-link ${hot}`} />
      <div className="topo-node" title="Home router: queue management under test">
        <span className="n-title"><Icon name="router" size={12} />Router</span>
        <span className="n-value">{isFlowPilot(snap.scheduler) ? "FlowPilot" : snap.label.replace(/ \(.*\)/, "")}</span>
        <span className="n-sub">{isNum(shaper) ? `shaper ${mbps(shaper, 1)} Mb/s` : "no shaping"}</span>
      </div>
      <div className="topo-link" />
      <div className="topo-devices">
        {caps.devices.map((d) => {
          const st = deviceStatus(snap, d.name);
          const apps = snap.apps.filter((a) => a.device === d.name && a.active).map((a) => a.label);
          return (
            <div className="dev" key={d.name} title={apps.length ? apps.join(", ") : `${d.kind}: idle`}>
              <span className={`sdot ${st}`} />
              <Icon name={DEVICE_ICON[d.name] ?? "monitor"} size={13} style={{ color: "var(--muted)", flex: "none" }} />
              <span className="d-name">{d.name}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Kpi({ icon, label, value, unit, sub, status, badge }: {
  icon: string; label: string; value: string; unit?: string; sub: string; status: Status; badge?: string;
}) {
  return (
    <div className="kpi">
      <div className="kpi-label"><Icon name={icon} size={13} />{label}</div>
      <div className="kpi-value num">
        {value}
        {unit && value !== "—" && <span className="unit">{unit}</span>}
      </div>
      <div className="kpi-sub" title={sub}>{sub}</div>
      <StatusBadge status={status} label={badge} />
    </div>
  );
}

function lastNonNull(history: LiveRow[], key: string, within = 30): number | null {
  for (let i = history.length - 1; i >= 0 && i >= history.length - within; i--) {
    const v = history[i][key];
    if (isNum(v)) return v;
  }
  return null;
}

function Kpis({ snap, side, history }: { snap: SideSnap; side: string; history: LiveRow[] }) {
  const act = snap.apps.filter((a) => a.active);
  const one = (t: string): AppSnap | undefined => act.find((a) => a.app_type === t);
  const game = one("gaming");
  const call = one("video_call");
  const stream = act.find((a) => a.app_id === "netflix") ?? one("streaming");
  const web = one("web");
  const bulk = act.filter((a) => a.app_type === "bulk");
  const up = act.filter((a) => a.app_type === "upload");
  const dl = bulk.reduce((s, a) => s + a.kbps_down, 0) / 1000;
  const ul = up.reduce((s, a) => s + a.kbps_up, 0) / 1000;
  const plt = web ? (isNum(web.plt_s) ? web.plt_s : lastNonNull(history, `plt_${side}`)) : null;
  const pltStatus: Status = !isNum(plt) ? "idle" : plt <= 1.5 ? "good" : plt <= 3 ? "warning" : "critical";
  const mosBadge = (a: AppSnap | undefined, st: Status) =>
    a && isNum(a.mos) ? `${STATUS_LABEL[st]} · MOS ${a.mos.toFixed(1)}` : undefined;
  const gameSt = game ? mosStatus(game.mos) : "idle";
  const callSt = call ? mosStatus(call.mos) : "idle";
  return (
    <div className="kpis">
      <Kpi
        icon="gamepad"
        label="Game ping p95"
        value={game ? fmt(game.latency_p95_ms, 0) : "—"}
        unit="ms"
        sub={game ? `${fmt(game.loss_pct, 1)}% packet loss` : "not running"}
        status={gameSt}
        badge={mosBadge(game, gameSt)}
      />
      <Kpi
        icon="video"
        label="Call delay p95"
        value={call ? fmt(call.latency_p95_ms, 0) : "—"}
        unit="ms"
        sub={call ? `video ${fmt(isNum(call.video_kbps_down) ? call.video_kbps_down / 1000 : null, 1)} Mb/s` : "not running"}
        status={callSt}
        badge={mosBadge(call, callSt)}
      />
      <Kpi
        icon="tv"
        label="Netflix bitrate"
        value={stream ? fmt(stream.bitrate_mbps, 1) : "—"}
        unit="Mb/s"
        sub={stream ? (stream.stalled ? "stalled · rebuffering" : `buffer ${fmt(stream.buffer_s, 0)} s`) : "not running"}
        status={stream ? appStatus(stream) : "idle"}
      />
      <Kpi
        icon="globe"
        label="Page load"
        value={web ? fmt(plt, 2) : "—"}
        unit="s"
        sub={web ? "recent pages" : "not running"}
        status={web ? pltStatus : "idle"}
      />
      <Kpi
        icon="download"
        label="Downloads"
        value={bulk.length || up.length ? fmt(bulk.length ? dl : ul, 1) : "—"}
        unit="Mb/s"
        sub={bulk.length ? (up.length ? `+ ${ul.toFixed(1)} Mb/s upload` : `${bulk.length} transfer${bulk.length > 1 ? "s" : ""}`) : up.length ? "upload" : "none running"}
        status={bulk.length || up.length ? "good" : "idle"}
      />
    </div>
  );
}

export function SideColumn({ side, snap, caps, history, role, colour, onIntent }: {
  side: string; snap: SideSnap; caps: Capabilities; history: LiveRow[]; role: string; colour: string;
  onIntent?: (intent: string) => void;
}) {
  const fp = isFlowPilot(snap.scheduler);
  const acc = snap.classifier?.accuracy;
  return (
    <section className={`card side-col ${fp ? "fp" : "base"}`}>
      <div className="side-head">
        <span className="side-swatch" style={{ background: colour }} />
        <div style={{ minWidth: 0 }}>
          <div className="side-role">{role}</div>
          <div className="side-name">{snap.label}</div>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
          {fp && snap.classifier && (
            <span className="pill" title="Classifier labels compared with the simulator's ground truth" style={{ background: "var(--accent-wash)" }}>
              <Icon name="brain" size={13} />
              {isNum(acc) ? `${(acc * 100).toFixed(0)}% correct` : "classifying"} · {snap.classifier.flows ?? 0} flows
            </span>
          )}
          {fp && snap.intent && onIntent && (
            <label className="intent" title="Household intent: what FlowPilot should favour right now">
              <span>Intent</span>
              <select className="select sm" value={snap.intent} onChange={(e) => onIntent(e.target.value)}>
                {caps.intents.map((i) => (
                  <option key={i} value={i}>{INTENT_LABEL[i] ?? i}</option>
                ))}
              </select>
            </label>
          )}
        </div>
      </div>
      <Topology snap={snap} caps={caps} />
      <Kpis snap={snap} side={side} history={history} />
    </section>
  );
}
