// Shapes of the FlowPilot API (backend/flowpilot/api). Everything numeric may be null when
// a metric has no samples in the interval, so the UI must tolerate gaps.

export type Num = number | null | undefined;

export interface LatencySummary {
  n: number;
  mean: Num;
  p50: Num;
  p95: Num;
  p99: Num;
  max: Num;
  loss_pct?: Num;
  jitter_ms?: Num;
}

export interface AppSnap {
  app_id: string;
  app_type: string;
  device: string;
  label: string;
  active: boolean;
  kbps_down: number;
  kbps_up: number;
  latency_ms?: Num;
  latency_p95_ms?: Num;
  latency_p99_ms?: Num;
  jitter_ms?: Num;
  loss_pct?: Num;
  mos?: Num;
  base_rtt_ms?: Num;
  base_owd_ms?: Num;
  bitrate_mbps?: Num;
  buffer_s?: Num;
  stalled?: boolean;
  rebuffer_s?: Num;
  startup_s?: Num;
  plt_s?: Num;
  throughput_mbps?: Num;
  flows?: number;
  video_kbps_down?: Num;
  video_kbps_up?: Num;
  freeze_pct?: Num;
  error?: string;
}

export interface ClassStatus {
  name: string;
  rate: number;
  ceil: number;
  prio: number;
  limit_bytes: number;
  demand: Num;
  saturated: boolean;
  arrival: number;
  served: number;
  backlog_bytes: number;
  flows: number;
  note: string;
  queue_delay_ms: number;
  drop_rate: number;
}

export interface FlowRow {
  key: (string | number)[];
  proto: string;
  device: string;
  lan_ip: string;
  lan_port: number;
  wan_ip: string;
  wan_port: number;
  label: string | null;
  label_name: string;
  confidence: number;
  tc: number;
  class_name: string;
  kbps_up: number;
  kbps_down: number;
  age_s: number;
  explanation: string;
  demoted: boolean;
  pinned: number | null;
  probs: Record<string, number>;
}

export interface Decision {
  t: number;
  kind: string;
  message: string;
  details?: unknown;
}

export interface ClassifierLive {
  kind: string;
  flows?: number;
  accuracy?: Num;
  byte_accuracy?: Num;
  classified?: number;
  reclassified?: number;
  demoted?: number;
}

export interface SideSnap {
  scheduler: string;
  label: string;
  capacity: { down: number; up: number };
  shaper: { down: Num; up: Num };
  isp_queue_ms: { down: Num; up: Num };
  apps: AppSnap[];
  decisions?: Decision[];
  classes?: { down?: Record<string, ClassStatus>; up?: Record<string, ClassStatus> };
  flows?: FlowRow[];
  classifier?: ClassifierLive;
  intent?: string;
}

export interface SessionInfo {
  status: string;
  params?: {
    mode: string;
    scenario: string;
    scheduler: string;
    compare_with?: string | null;
    seed: number;
    speed: number;
    intent: string;
  };
  run_ids?: Record<string, number>;
  mode?: string;
  ticks?: number;
  error?: string | null;
}

export interface Tick {
  type: "tick";
  t: number;
  speed: number;
  mode: string;
  session: SessionInfo;
  sides: Record<string, SideSnap>;
  events: { t: number; event: string }[];
}

export interface Scenario {
  name: string;
  title: string;
  description: string;
  duration?: number;
}

export interface Preset {
  name: string;
  label: string;
  description: string;
  icon: string;
}

export interface ClassMeta {
  name: string;
  prio: number;
  dscp: number;
  wmm: string;
  colour: string;
  target_delay_ms: number;
}

export interface Capabilities {
  kernel: { root?: boolean; netns?: boolean; qdiscs?: Record<string, boolean> };
  kernel_available: boolean;
  modes: string[];
  schedulers: Record<string, string[]>;
  scheduler_labels: Record<string, string>;
  scenarios: Scenario[];
  presets: Preset[];
  intents: string[];
  classes: Record<string, ClassMeta>;
  apps: Record<string, string>;
  devices: { name: string; ip: string; kind: string }[];
}

export type Summary = Record<string, number | null | undefined | Record<string, unknown>>;

export interface BenchRow {
  id: number;
  mode: string;
  scenario: string;
  scheduler: string;
  seed: number;
  label: string;
  summary: Summary;
  flow_accuracy?: { flows?: number; accuracy?: Num; byte_accuracy?: Num } | null;
}

export interface SeriesRow {
  t: number;
  side?: string;
  cap_down?: Num;
  cap_up?: Num;
  shaper_down?: Num;
  shaper_up?: Num;
  isp_q_down?: Num;
  isp_q_up?: Num;
  // benchmark rows: apps keyed by id; live rows: list of snapshots
  apps?: Record<string, Partial<AppSnap>> | AppSnap[];
  capacity?: { down: number; up: number };
  shaper?: { down: Num; up: Num };
  isp_queue_ms?: { down: Num; up: Num };
  scheduler?: string;
}

export interface RunRow {
  id: number;
  created_at: string | null;
  finished_at: string | null;
  kind: string;
  mode: string;
  scenario: string;
  scheduler: string;
  seed: number;
  intent: string;
  status: string;
  label: string;
  duration_s: Num;
}

export interface Profile {
  device: string;
  ip: string;
  kind: string;
  profile: Record<string, { seen_s: number; avg_kbps_down: number; avg_kbps_up: number; peak_kbps_down: number; peak_kbps_up: number }>;
  updated_at: string | null;
}

export interface DbStats {
  backend: string;
  runs: number;
  samples: number;
  decisions: number;
  flows: number;
}

export interface ClassifierCard {
  model: string;
  trained_at: string;
  n_samples: number;
  n_train: number;
  n_test: number;
  n_households: number;
  labels: string[];
  features: string[];
  test: {
    accuracy: number;
    macro_f1: number;
    per_class: Record<string, { precision: number; recall: number; f1: number; support: number }>;
    confusion: number[][];
    by_age: { age_lo: number; age_hi: number | null; n: number; accuracy: number }[];
  };
  random_forest: { accuracy: number; macro_f1: number };
  rule_based: { accuracy: number; macro_f1: number };
  importance: { feature: string; importance: number }[];
  train_seconds: number;
  inference_ms_per_64_flows: number;
  class_counts: Record<string, number>;
}

export interface RlEval {
  policy: string;
  mean_reward: number;
  utilization: number;
  delay_mean_ms: number;
  delay_p95_ms: number;
  delay_p99_ms: number;
  frac_over_20ms: number;
  by_kind: Record<string, { utilization: number; delay_p95_ms: number }>;
}

export interface RlReport {
  training?: {
    history: { step: number; transitions?: number; eps?: number; episode_return: number; elapsed_s?: number }[];
    /** greedy-policy evaluation at checkpoints on fixed held-out traces */
    eval_curve?: { step: number; transitions?: number; mean_reward: number; utilization?: number; frac_over_20ms?: number }[];
    /** the hand-tuned autorate on the same traces */
    eval_reference?: { mean_reward: number; utilization?: number; frac_over_20ms?: number };
  };
  eval?: RlEval[];
}
