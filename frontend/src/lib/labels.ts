// Short, consistent display names used where space is tight (charts, tables).
export const SCHED_SHORT: Record<string, string> = {
  fifo: "Standard router (FIFO)",
  static_qos: "Static QoS",
  fq_codel: "SQM fq_codel",
  cake: "SQM CAKE",
  cake_autorate: "CAKE + autorate",
  flowpilot: "FlowPilot (AI)",
  flowpilot_fq: "FlowPilot + fq_codel",
  flowpilot_rl: "FlowPilot + RL autorate",
  flowpilot_rules: "FlowPilot (rules)",
};

export const PRESET_SHORT: Record<string, string> = {
  steam: "Steam update",
  backup: "Photo backup",
  sync: "OneDrive sync (laptop)",
  torrent: "16-conn download",
  second_tv: "2nd 4K stream",
  congestion: "ISP congestion -55%",
  restore: "Restore ISP",
};

export const APP_SHORT: Record<string, string> = {
  gaming: "Gaming",
  video_call: "Call",
  streaming: "Streaming",
  web: "Web",
  bulk: "Download",
  upload: "Backup",
};

export function schedLabel(s: string, fallback?: string): string {
  return SCHED_SHORT[s] ?? fallback ?? s;
}
