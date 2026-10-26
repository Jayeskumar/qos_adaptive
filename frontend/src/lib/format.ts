import type { Num } from "./types";

export const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function fmt(v: Num, digits = 0, unit = ""): string {
  if (!isNum(v)) return "—";
  return `${v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}${unit}`;
}

/** Mb/s from bit/s with sensible precision. */
export function mbps(bps: Num, digits?: number): string {
  if (!isNum(bps)) return "—";
  const m = bps / 1e6;
  const d = digits ?? (m >= 100 ? 0 : m >= 10 ? 1 : 2);
  return m.toFixed(d);
}

export function kbpsToMbps(kbps: Num, digits = 1): string {
  if (!isNum(kbps)) return "—";
  return (kbps / 1000).toFixed(digits);
}

export function pct(v: Num, digits = 1): string {
  if (!isNum(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export function compact(n: Num): string {
  if (!isNum(n)) return "—";
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e3).toFixed(0)}K`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${Math.round(n)}`;
}

export function ago(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return `${Math.max(0, Math.round(s))} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return d.toLocaleString();
}

export function clockTime(t: Num): string {
  if (!isNum(t)) return "—";
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function titleCase(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
