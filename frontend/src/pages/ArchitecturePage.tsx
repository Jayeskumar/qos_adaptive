import { Card } from "../components/ui";

function Box({ x, y, w, h, title, lines = [], accent = false, small = false }: {
  x: number; y: number; w: number; h: number; title: string; lines?: string[]; accent?: boolean; small?: boolean;
}) {
  return (
    <g>
      <rect
        x={x}
        y={y}
        width={w}
        height={h}
        rx={10}
        style={{
          fill: accent ? "var(--accent-wash)" : "var(--surface-2)",
          stroke: accent ? "var(--accent)" : "var(--border-strong)",
          strokeWidth: 1.2,
        }}
      />
      <text x={x + 12} y={y + 22} style={{ fill: "var(--ink)", fontSize: small ? 12.5 : 13.5, fontWeight: 650 }}>{title}</text>
      {lines.map((l, i) => (
        <text key={i} x={x + 12} y={y + 42 + i * 17} style={{ fill: "var(--ink-2)", fontSize: 11.5 }}>{l}</text>
      ))}
    </g>
  );
}

function Chip({ x, y, w, label, colour }: { x: number; y: number; w: number; label: string; colour: string }) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={24} rx={6} style={{ fill: "var(--surface)", stroke: "var(--border-strong)" }} />
      <rect x={x + 8} y={y + 8} width={8} height={8} rx={2} style={{ fill: colour }} />
      <text x={x + 22} y={y + 16.5} style={{ fill: "var(--ink)", fontSize: 11 }}>{label}</text>
    </g>
  );
}

function Arrow({ d, label, lx, ly }: { d: string; label?: string; lx?: number; ly?: number }) {
  return (
    <g>
      <path d={d} markerEnd="url(#arrow)" style={{ fill: "none", stroke: "var(--muted)", strokeWidth: 1.4 }} />
      {label && <text x={lx} y={ly} style={{ fill: "var(--muted)", fontSize: 10.5 }}>{label}</text>}
    </g>
  );
}

function Band({ y, h, label }: { y: number; h: number; label: string }) {
  return (
    <g>
      <rect x={6} y={y} width={1228} height={h} rx={14} style={{ fill: "none", stroke: "var(--border)", strokeDasharray: "0" }} />
      <text x={20} y={y + 18} style={{ fill: "var(--muted)", fontSize: 10.5, fontWeight: 700, letterSpacing: "0.08em" }}>{label}</text>
    </g>
  );
}

function Diagram() {
  const cls = [
    { l: "1:10 Real-time", c: "#9085e9" },
    { l: "1:20 Conferencing", c: "#199e70" },
    { l: "1:30 Interactive", c: "#c98500" },
    { l: "1:40 Streaming", c: "#3987e5" },
    { l: "1:50 Bulk -> 1:51..59", c: "#d95926" },
  ];
  return (
    <svg className="diagram" viewBox="0 0 1240 640" width="100%" role="img" aria-label="FlowPilot architecture">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0L10 5L0 10z" style={{ fill: "var(--muted)" }} />
        </marker>
      </defs>
      <Band y={8} h={196} label="DATA PLANE · LINUX KERNEL OF THE HOME ROUTER" />
      <Box x={22} y={40} w={150} h={140} title="Home devices" lines={["console · laptop", "smart TV · PC", "phone · tablet", "6 network namespaces"]} />
      <Box x={204} y={40} w={226} h={140} title="nftables (postrouting)" lines={["flow class = conntrack mark", "mark -> skb priority (HTB class)", "DSCP for Wi-Fi WMM / NQB", "ACKs, DNS, probes: fixed rules"]} />
      <g>
        <rect x={462} y={30} width={444} height={160} rx={10} style={{ fill: "var(--accent-wash)", stroke: "var(--accent)", strokeWidth: 1.2 }} />
        <text x={474} y={52} style={{ fill: "var(--ink)", fontSize: 13.5, fontWeight: 650 }}>HTB tree per direction (r-lan down, r-wan up)</text>
        <text x={474} y={72} style={{ fill: "var(--ink-2)", fontSize: 11.5 }}>1:1 root = capacity from autorate · rate = guarantee, ceil = borrow limit</text>
        {cls.map((c, i) => (
          <Chip key={c.l} x={474 + (i % 3) * 142} y={84 + Math.floor(i / 3) * 32} w={134} label={c.l} colour={c.c} />
        ))}
        <text x={474} y={176} style={{ fill: "var(--ink-2)", fontSize: 11.5 }}>leaves: fq_codel, or bfifo sized to rate x delay budget</text>
      </g>
      <Box x={938} y={40} w={136} h={140} title="ISP access" lines={["capacity varies", "(evening, 5G FWA)", "deep FIFO buffer", "TBF in fp-isp"]} />
      <Box x={1098} y={40} w={124} h={140} title="Internet" lines={["game · call", "CDN · mirror", "web · backup", "+ delay line"]} />
      <Arrow d="M172 110 H202" />
      <Arrow d="M430 110 H460" />
      <Arrow d="M906 110 H936" />
      <Arrow d="M1074 110 H1096" />

      <Band y={214} h={86} label="TELEMETRY · WHAT THE ROUTER CAN SEE" />
      <Box x={204} y={238} w={300} h={52} title="AF_PACKET flow monitor" lines={["headers only (64 B), both directions, before the shaper"]} small />
      <Box x={530} y={238} w={300} h={52} title="tc class counters" lines={["bytes, backlog, drops per HTB class"]} small />
      <Box x={856} y={238} w={366} h={52} title="20 Hz reflector probes" lines={["one-way delay up / down, kernel timestamps"]} small />
      <Arrow d="M316 180 V236" />
      <Arrow d="M684 190 V236" />
      <Arrow d="M1006 180 V236" />

      <Band y={310} h={176} label="CONTROL PLANE · FLOWPILOT BRAIN (EVERY 500 MS)" />
      {[
        { t: "1  Classify", l: ["gradient-boosted trees", "26 behavioural features", "hysteresis, sibling inheritance"] },
        { t: "2  Estimate demand", l: ["offered load per class", "100 ms peaks for calls/games", "learned stream bitrate"] },
        { t: "3  Track capacity", l: ["probe-delay autorate", "DQN policy + safety shield", "keeps the queue in the router"] },
        { t: "4  Allocate (NUM)", l: ["inelastic first, capped", "weighted water-filling (intent)", "per-device fairness in bulk"] },
        { t: "5  Act", l: ["conntrack mark per flow", "tc class change rate/ceil/prio", "buffer = rate x delay budget"] },
      ].map((s, i) => (
        <Box key={s.t} x={22 + i * 243} y={340} w={222} h={132} title={s.t} lines={s.l} accent={i === 3} />
      ))}
      {[0, 1, 2, 3].map((i) => <Arrow key={i} d={`M${244 + i * 243} 406 H${263 + i * 243}`} />)}
      <Arrow d="M204 266 H14 V406 H20" label="flow features" lx={30} ly={284} />
      <Arrow d="M680 290 V318 H470 V338" label="queues, rates" lx={560} ly={313} />
      <Arrow d="M1006 290 V326 H619 V338" label="one-way delays" lx={870} ly={322} />
      <Arrow d="M1216 406 H1232 V24 H684 V28" label="plans + marks" lx={1150} ly={20} />

      <Band y={496} h={136} label="PRODUCT · DIGITAL TWIN, API, STORAGE, DASHBOARD" />
      <Box x={22} y={526} w={300} h={92} title="Digital twin (packet simulator)" lines={["same brain code as the router", "trains classifier + RL agent", "A/B demos on any laptop"]} />
      <Box x={352} y={526} w={250} h={92} title="FastAPI service" lines={["REST + WebSocket (1 Hz ticks)", "live sim or kernel sessions", "benchmark import"]} accent />
      <Box x={632} y={526} w={300} h={92} title="PostgreSQL" lines={["runs · samples · decisions", "classified flows", "learned device profiles"]} />
      <Box x={962} y={526} w={260} h={92} title="React dashboard" lines={["live A/B comparison", "benchmarks, AI model cards", "history and profiles"]} />
      <Arrow d="M322 572 H350" />
      <Arrow d="M602 572 H630" />
      <Arrow d="M932 572 H960" />
    </svg>
  );
}

export function ArchitecturePage() {
  return (
    <div className="stack">
      <div>
        <div className="section-title">How FlowPilot works</div>
        <p className="section-sub">
          A software-defined QoS controller for the home router. The data plane uses only primitives every Linux router kernel has
          (HTB, FIFO/fq_codel, nftables, conntrack) - the intelligence lives in a control loop that learns what each flow is,
          what it needs, and how much capacity the ISP link really has right now.
        </p>
      </div>
      <Card><Diagram /></Card>
      <div className="steps">
        {[
          { n: 1, h: "See", p: "Router-side telemetry only: packet headers from both directions, HTB queue counters and 20 Hz latency probes to a public reflector. No payload inspection, no app cooperation." },
          { n: 2, h: "Understand", p: "A port-agnostic ML classifier recognises gaming, calls, streaming, web, downloads and backups from flow behaviour - 99.7 % on unseen households, mostly within the first second." },
          { n: 3, h: "Learn the link", p: "Delay-based autorate (or the learned DQN policy) keeps the shaper just below the true capacity, so the queue - and the latency - stays in the router instead of the ISP's bloated buffer." },
          { n: 4, h: "Decide", p: "Network Utility Maximisation: latency-critical, low-rate traffic is protected first; elastic traffic shares the rest by household intent (balanced, gaming, work-from-home, movie night) with no starvation." },
          { n: 5, h: "Act and explain", p: "Per-flow conntrack marks and HTB class updates every 500 ms; every action is logged in plain language and stored in PostgreSQL for audit and learning." },
        ].map((s) => (
          <div className="step" key={s.n}>
            <div className="step-n">{s.n}</div>
            <h4>{s.h}</h4>
            <p>{s.p}</p>
          </div>
        ))}
      </div>
      <Card title="Why this design" icon="info">
        <div className="prose">
          <h3>Same brain, two worlds</h3>
          <p>
            The controller (<code>flowpilot/controller/brain.py</code>) is data-plane agnostic. It drives a packet-level digital
            twin - used to train the models and to demo on any laptop - and the real Linux kernel of the testbed router
            (<code>flowpilot/testbed</code>), where results are measured on real TCP/UDP stacks and real qdiscs.
          </p>
          <h3>Classes follow DiffServ practice</h3>
          <p>
            Real-time (games, VoIP, probes, DNS), Conferencing, Interactive (web and brand-new flows), Streaming and Bulk (RFC 8622
            lower-effort) mirror RFC 4594 service classes; FlowPilot also writes the matching DSCP so Wi-Fi WMM (RFC 8325) and
            NQB-aware networks (RFC 9956) can honour the decision beyond the router.
          </p>
          <h3>Safe by construction</h3>
          <p>
            Guarantees never exceed capacity, every elastic class can borrow idle bandwidth (work-conserving), bulk keeps a floor
            so it never starves, flows that exceed their class budget are demoted (traffic protection), and the RL policy runs
            inside a shield that forbids unsafe rate increases.
          </p>
        </div>
      </Card>
    </div>
  );
}
