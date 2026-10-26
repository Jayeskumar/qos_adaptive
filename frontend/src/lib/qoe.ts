// QoE semantics shared by tiles, device chips and tables. Status colours are reserved for
// state (good / warning / serious / critical) and always ship with an icon + label.
import type { AppSnap, SideSnap } from "./types";
import { isNum } from "./format";

export type Status = "good" | "warning" | "serious" | "critical" | "idle";

export const STATUS_LABEL: Record<Status, string> = {
  good: "Good",
  warning: "Fair",
  serious: "Poor",
  critical: "Bad",
  idle: "Idle",
};

const RANK: Record<Status, number> = { idle: 0, good: 1, warning: 2, serious: 3, critical: 4 };

export function worst(a: Status, b: Status): Status {
  return RANK[a] >= RANK[b] ? a : b;
}

export function mosStatus(mos: unknown): Status {
  if (!isNum(mos)) return "idle";
  if (mos >= 4.0) return "good";
  if (mos >= 3.4) return "warning";
  if (mos >= 2.6) return "serious";
  return "critical";
}

export function appStatus(a: AppSnap): Status {
  if (!a.active) return "idle";
  switch (a.app_type) {
    case "gaming":
    case "video_call":
      return mosStatus(a.mos);
    case "streaming":
      if (a.stalled) return "critical";
      if (!isNum(a.bitrate_mbps)) return "idle";
      if (a.bitrate_mbps >= 11.9) return "good";
      if (a.bitrate_mbps >= 4.3) return "warning";
      return "serious";
    case "web":
      if (!isNum(a.plt_s)) return "idle";
      if (a.plt_s <= 1.5) return "good";
      if (a.plt_s <= 3) return "warning";
      return "critical";
    default:
      return "good";
  }
}

export function deviceStatus(side: SideSnap | undefined, device: string): Status {
  if (!side) return "idle";
  let s: Status = "idle";
  for (const a of side.apps) {
    if (a.device === device && a.active) s = worst(s, appStatus(a));
  }
  return s;
}

export const APP_ICON: Record<string, string> = {
  gaming: "gamepad",
  video_call: "video",
  streaming: "tv",
  web: "globe",
  bulk: "download",
  upload: "upload",
};

export const DEVICE_ICON: Record<string, string> = {
  console: "gamepad",
  laptop: "laptop",
  tv: "tv",
  pc: "monitor",
  phone: "phone",
  tablet: "tablet",
};

export const APP_NAME: Record<string, string> = {
  gaming: "Online gaming",
  video_call: "Video call",
  streaming: "Video streaming",
  web: "Web browsing",
  bulk: "Bulk download",
  upload: "Cloud backup",
};

export function isFlowPilot(scheduler: string | undefined): boolean {
  return !!scheduler && scheduler.startsWith("flowpilot");
}
