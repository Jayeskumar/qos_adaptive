import { useMemo, useState } from "react";
import type { Capabilities, SessionInfo } from "../lib/types";
import type { LiveState } from "../lib/useLive";
import { APP_ORDER } from "../lib/useLive";
import { postJSON } from "../lib/api";
import { useTheme } from "../lib/theme";
import { APP_NAME, isFlowPilot } from "../lib/qoe";
import { isNum } from "../lib/format";
import { Card, Empty, Seg } from "../components/ui";
import { Icon } from "../components/Icon";
import { TimeChart, type SeriesSpec } from "../components/Chart";
import { SessionBar, type StartParams } from "../components/live/SessionBar";
import { EventBar } from "../components/live/EventBar";
import { SideColumn } from "../components/live/SideColumn";
import { ClassAlloc, DecisionFeed, FlowTable, Hero } from "../components/live/Panels";

const EVENT_NAMES: Record<string, string> = {
  "start steam": "Steam update",
  "start backup": "Photo backup",
  "start sync": "OneDrive sync",
  "start torrent": "16-conn download",
  "start netflix2": "2nd stream",
};

// Short series names so the seven-item throughput legend fits on one line.
const THR_NAME: Record<string, string> = {
  streaming: "Streaming",
  bulk: "Downloads",
  video_call: "Video call",
  web: "Web",
  upload: "Uploads",
  gaming: "Gaming",
};

export function LivePage({ caps, live, session, refresh }: {
  caps: Capabilities;
  live: LiveState;
  session: SessionInfo | null;
  refresh: () => void;
}) {
  const { pal } = useTheme();
  const [busy, setBusy] = useState(false);
  const [nfx, setNfx] = useState<"br" | "buf">("br");
  const [err, setErr] = useState<string | null>(null);
  const tick = live.tick;
  const finished = session?.status === "finished";
  const showData = !!tick && !live.stopped;
  const effSession = finished || !showData ? session : tick.session;
  const running = effSession?.status === "running";
  const sides = showData ? Object.keys(tick!.sides).sort() : [];
  const compare = sides.length > 1;
  const fpSide = sides.find((s) => isFlowPilot(tick!.sides[s].scheduler)) ?? sides[sides.length - 1];
  const colour = (s: string) => (isFlowPilot(tick!.sides[s].scheduler) ? pal.flowpilot : pal.baseline);
  const role = (s: string) => (compare ? (s === "a" ? "A · Today's router" : "B · With FlowPilot AI") : "Router under test");

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      refresh();
    }
  };
  const start = (p: StartParams) =>
    run(async () => {
      await postJSON("/api/session/start", p);
      live.reset();
    });
  const stop = () => run(() => postJSON("/api/session/stop"));
  const action = (a: Record<string, unknown>) => run(() => postJSON("/api/session/action", a));

  const hist = live.history;
  const events = useMemo(() => {
    const t0 = hist.length ? hist[0].t : 0;
    return live.events
      .filter((e) => e.t >= t0 && !/^start (game|call|netflix|web)$/.test(e.event))
      .map((e) => ({ x: e.t, label: EVENT_NAMES[e.event] ?? e.event.replace(/^start /, "") }))
      .slice(-4);
  }, [live.events, hist]);

  const gameApp = tick && sides.length ? tick.sides[sides[0]].apps.find((a) => a.app_type === "gaming") : undefined;
  const callApp = tick && sides.length ? tick.sides[sides[0]].apps.find((a) => a.app_type === "video_call") : undefined;
  const gameSlo = isNum(gameApp?.base_rtt_ms) ? gameApp!.base_rtt_ms! + 30 : null;
  const callSlo = isNum(callApp?.base_owd_ms) ? callApp!.base_owd_ms! + 50 : null;

  const sideSeries = (prefix: string): SeriesSpec[] =>
    sides.map((s) => ({ key: `${prefix}_${s}`, name: tick!.sides[s].label, colour: colour(s) }));

  const thrSeries: SeriesSpec[] = fpSide
    ? APP_ORDER.filter((t) => hist.some((r) => (r[`thr_${t}_${fpSide}`] ?? 0) > 0.05)).map((t) => ({
        key: `thr_${t}_${fpSide}`,
        name: THR_NAME[t] ?? APP_NAME[t],
        colour: pal.app[t],
        kind: "area" as const,
        stack: "thr",
      }))
    : [];
  if (fpSide) {
    thrSeries.push({ key: `cap_${fpSide}`, name: "ISP capacity", colour: pal.ink2, width: 1.5 });
    if (hist.some((r) => isNum(r[`shaper_${fpSide}`]))) thrSeries.push({ key: `shaper_${fpSide}`, name: "Shaper rate", colour: pal.muted, width: 1.5 });
  }

  const labels = { a: tick?.sides.a?.label ?? "A", b: tick?.sides.b?.label ?? "B" };
  const fp = fpSide ? tick!.sides[fpSide] : undefined;

  return (
    <div className="stack">
      <Card>
        <SessionBar caps={caps} session={effSession ?? null} t={tick?.t ?? null} onStart={start} onStop={stop} busy={busy} />
        {err && (
          <div className="banner error" style={{ marginTop: 12 }}>
            <Icon name="alert" size={16} />
            {err}
          </div>
        )}
        {effSession?.error && running && (
          <div className="banner error" style={{ marginTop: 12 }}>
            <Icon name="alert" size={16} />
            {effSession.error}
          </div>
        )}
      </Card>

      {!showData || !tick ? (
        <Card>
          <Empty icon="router" title={running ? "Waiting for the first telemetry tick..." : "Start a live session"}>
            <div style={{ maxWidth: 640, margin: "6px auto 0" }}>
              Choose <b>Simulation</b>, router <b>FlowPilot (AI)</b>, compare with <b>Standard router (FIFO)</b> and the{" "}
              <b>Live household</b> scenario, then press <b>Start session</b>. A game, a Teams call, 4K Netflix and web browsing start
              automatically - then trigger congestion with one click.
            </div>
          </Empty>
        </Card>
      ) : (
        <>
          <Card tight>
            <EventBar caps={caps} tick={tick} onAction={action} disabled={busy || !running} />
          </Card>

          {compare && <Hero history={hist} labels={labels} />}

          <div className={compare ? "grid-2" : "stack"}>
            {sides.map((s) => (
              <SideColumn
                key={s}
                side={s}
                snap={tick.sides[s]}
                caps={caps}
                history={hist}
                role={role(s)}
                colour={colour(s)}
                onIntent={(name) => action({ type: "intent", name })}
              />
            ))}
          </div>

          <div className="grid-2">
            <Card title="Gaming latency" sub="In-game ping, 95th percentile per second" icon="gamepad">
              {hist.some((r) => sides.some((s) => isNum(r[`game_${s}`]))) ? (
                <TimeChart
                  data={hist}
                  series={sideSeries("game")}
                  unit="ms"
                  refs={gameSlo ? [{ y: gameSlo, label: "SLO: base + 30 ms" }] : []}
                  xRefs={events}
                  height={230}
                />
              ) : (
                <Empty icon="gamepad" title="No game running" />
              )}
            </Card>
            <Card title="Video-call delay" sub="One-way delay of the call, 95th percentile (worse direction)" icon="video">
              {hist.some((r) => sides.some((s) => isNum(r[`call_${s}`]))) ? (
                <TimeChart
                  data={hist}
                  series={sideSeries("call")}
                  unit="ms"
                  refs={callSlo ? [{ y: callSlo, label: "SLO: base + 50 ms" }] : []}
                  xRefs={events}
                  height={230}
                />
              ) : (
                <Empty icon="video" title="No call running" />
              )}
            </Card>
            <Card
              title="Downlink throughput by application"
              sub={fp ? `${fp.label} · stacked, with ISP capacity and shaper rate` : undefined}
              icon="activity"
            >
              {thrSeries.length > 1 ? (
                <TimeChart data={hist} series={thrSeries} unit="Mb/s" digits={1} height={230} xRefs={events} />
              ) : (
                <Empty icon="activity" title="Waiting for traffic" />
              )}
            </Card>
            <Card
              title={nfx === "br" ? "Netflix bitrate" : "Netflix buffer"}
              sub={nfx === "br" ? "Adaptive-bitrate rung chosen by the player (16 Mb/s = 4K)" : "Seconds of video buffered ahead - 0 means a stall"}
              icon="tv"
              right={<Seg value={nfx} onChange={setNfx} options={[{ value: "br", label: "Bitrate" }, { value: "buf", label: "Buffer" }]} />}
            >
              {hist.some((r) => sides.some((s) => isNum(r[`br_${s}`]))) ? (
                <TimeChart
                  data={hist}
                  series={sides.map((s) => ({ key: `${nfx}_${s}`, name: tick.sides[s].label, colour: colour(s), kind: nfx === "br" ? ("step" as const) : ("line" as const) }))}
                  unit={nfx === "br" ? "Mb/s" : "s"}
                  digits={1}
                  yMax={nfx === "br" ? 20 : "auto"}
                  height={230}
                  xRefs={events}
                />
              ) : (
                <Empty icon="tv" title="No stream running" />
              )}
            </Card>
          </div>

          {fp && isFlowPilot(fp.scheduler) ? (
            <div className="grid-live-bottom">
              <DecisionFeed decisions={live.decisions[fpSide!] ?? []} title="AI decisions" />
              <FlowTable flows={fp.flows ?? []} caps={caps} accuracy={fp.classifier?.accuracy ?? null} />
              <ClassAlloc snap={fp} />
            </div>
          ) : (
            <Card>
              <Empty icon="brain" title="The AI panels appear when FlowPilot is one of the routers">
                Select <b>FlowPilot (AI)</b> as the router under test to see classifications, decisions and allocations.
              </Empty>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
