// Live telemetry: WebSocket /ws/live -> latest tick + rolling chart history + decision log.
import { useEffect, useRef, useState } from "react";
import { wsUrl } from "./api";
import type { Decision, Tick } from "./types";

export const APP_ORDER = ["streaming", "bulk", "video_call", "web", "upload", "gaming"] as const;
const HISTORY = 150;
const MAX_DECISIONS = 250;

export type LiveRow = { t: number } & Record<string, number | null>;

function sum(xs: number[]): number {
  return xs.reduce((a, b) => a + b, 0);
}

export function deriveRow(tick: Tick): LiveRow {
  const row: LiveRow = { t: tick.t };
  for (const [side, s] of Object.entries(tick.sides)) {
    const active = s.apps.filter((a) => a.active);
    const first = (type: string) => active.find((a) => a.app_type === type);
    row[`game_${side}`] = first("gaming")?.latency_p95_ms ?? null;
    row[`call_${side}`] = first("video_call")?.latency_p95_ms ?? null;
    const stream = active.find((a) => a.app_id === "netflix") ?? first("streaming");
    row[`br_${side}`] = stream?.bitrate_mbps ?? null;
    row[`buf_${side}`] = stream?.buffer_s ?? null;
    row[`plt_${side}`] = first("web")?.plt_s ?? null;
    row[`dl_${side}`] = sum(active.filter((a) => a.app_type === "bulk").map((a) => a.kbps_down)) / 1000;
    row[`ul_${side}`] = sum(active.filter((a) => a.app_type === "upload").map((a) => a.kbps_up)) / 1000;
    row[`cap_${side}`] = s.capacity.down / 1e6;
    row[`capup_${side}`] = s.capacity.up / 1e6;
    row[`shaper_${side}`] = s.shaper.down != null ? s.shaper.down / 1e6 : null;
    row[`ispq_${side}`] = s.isp_queue_ms.down ?? null;
    for (const type of APP_ORDER) {
      row[`thr_${type}_${side}`] = sum(active.filter((a) => a.app_type === type).map((a) => a.kbps_down)) / 1000;
    }
  }
  return row;
}

export interface LiveState {
  tick: Tick | null;
  history: LiveRow[];
  decisions: Record<string, Decision[]>;
  connected: boolean;
  stopped: boolean;
  events: { t: number; event: string }[];
  reset: () => void;
}

export function useLive(): LiveState {
  const [tick, setTick] = useState<Tick | null>(null);
  const [history, setHistory] = useState<LiveRow[]>([]);
  const [decisions, setDecisions] = useState<Record<string, Decision[]>>({});
  const [connected, setConnected] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [events, setEvents] = useState<{ t: number; event: string }[]>([]);
  const runKey = useRef("");
  const wsRef = useRef<WebSocket | null>(null);

  const reset = () => {
    runKey.current = "";
    setHistory([]);
    setDecisions({});
    setEvents([]);
    setTick(null);
  };

  useEffect(() => {
    let alive = true;
    let retry = 1000;
    let timer: number | undefined;
    let ping: number | undefined;

    const connect = () => {
      const ws = new WebSocket(wsUrl("/ws/live"));
      wsRef.current = ws;
      ws.onopen = () => {
        retry = 1000;
        setConnected(true);
        ping = window.setInterval(() => ws.readyState === 1 && ws.send("ping"), 20000);
      };
      ws.onmessage = (ev) => {
        if (typeof ev.data !== "string" || ev.data === "pong") return;
        let msg: { type?: string; sides?: unknown };
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === "stopped") {
          setStopped(true);
          return;
        }
        if (msg.type !== "tick" || !msg.sides) return;
        const t = msg as unknown as Tick;
        const key = JSON.stringify(t.session?.run_ids ?? {});
        const fresh = key !== runKey.current;
        if (fresh) runKey.current = key;
        setStopped(false);
        setTick(t);
        const row = deriveRow(t);
        setHistory((h) => {
          const base = fresh ? [] : h;
          if (base.length && base[base.length - 1].t >= row.t) return base;
          const next = base.concat(row);
          return next.length > HISTORY ? next.slice(next.length - HISTORY) : next;
        });
        setDecisions((d) => {
          const out: Record<string, Decision[]> = fresh ? {} : { ...d };
          for (const [side, s] of Object.entries(t.sides)) {
            const incoming = (s.decisions ?? []).slice().reverse();
            if (incoming.length || fresh) {
              out[side] = incoming.concat(out[side] ?? []).slice(0, MAX_DECISIONS);
            }
          }
          return out;
        });
        if (t.events?.length) {
          setEvents((e) => {
            const seen = new Set((fresh ? [] : e).map((x) => `${x.t}|${x.event}`));
            const add = t.events.filter((x) => !seen.has(`${x.t}|${x.event}`));
            return (fresh ? [] : e).concat(add).slice(-60);
          });
        } else if (fresh) {
          setEvents([]);
        }
      };
      ws.onclose = () => {
        setConnected(false);
        window.clearInterval(ping);
        if (alive) {
          timer = window.setTimeout(connect, retry);
          retry = Math.min(5000, retry * 1.6);
        }
      };
      ws.onerror = () => ws.close();
    };
    connect();
    return () => {
      alive = false;
      window.clearTimeout(timer);
      window.clearInterval(ping);
      wsRef.current?.close();
    };
  }, []);

  return { tick, history, decisions, connected, stopped, events, reset };
}
