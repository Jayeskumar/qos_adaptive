import { useEffect, useState } from "react";
import type { Capabilities, SessionInfo } from "../../lib/types";
import { clockTime } from "../../lib/format";
import { Icon } from "../Icon";
import { Select } from "../ui";

export interface StartParams {
  mode: string;
  scenario: string;
  scheduler: string;
  compare_with: string | null;
  speed: number;
  intent: string;
}

const MODE_LABEL: Record<string, string> = {
  sim: "Simulation (digital twin)",
  kernel: "Linux testbed (real kernel)",
};

export function SessionBar({ caps, session, t, onStart, onStop, busy }: {
  caps: Capabilities;
  session: SessionInfo | null;
  t: number | null;
  onStart: (p: StartParams) => void;
  onStop: () => void;
  busy: boolean;
}) {
  const running = session?.status === "running";
  const [mode, setMode] = useState("sim");
  const [scheduler, setScheduler] = useState("flowpilot");
  const [compare, setCompare] = useState("fifo");
  const [scenario, setScenario] = useState("sandbox");
  const [speed, setSpeed] = useState("1");

  // reflect the running session in the controls (e.g. after a page reload)
  useEffect(() => {
    const p = session?.params;
    if (!p || session?.status !== "running") return;
    setMode(p.mode);
    setScheduler(p.scheduler);
    setCompare(p.compare_with ?? "none");
    setScenario(p.scenario);
    setSpeed(String(p.speed ?? 1));
  }, [session?.status, session?.params]);

  const scheds = caps.schedulers[mode] ?? caps.schedulers.sim;
  const label = (s: string) => caps.scheduler_labels[s] ?? s;
  const baselines = scheds.filter((s) => !s.startsWith("flowpilot"));
  const kernel = mode === "kernel";

  useEffect(() => {
    if (!scheds.includes(scheduler)) setScheduler(scheds.includes("flowpilot") ? "flowpilot" : scheds[0]);
    if (kernel) setCompare("none");
  }, [mode]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = () =>
    onStart({
      mode,
      scenario,
      scheduler,
      compare_with: kernel || compare === "none" ? null : compare,
      speed: kernel ? 1 : Number(speed),
      intent: session?.params?.intent ?? "balanced",
    });

  return (
    <div className="session-bar">
      <Select
        label="Network"
        value={mode}
        onChange={setMode}
        options={caps.modes.map((m) => ({ value: m, label: MODE_LABEL[m] ?? m }))}
        width={200}
      />
      <Select
        label="Router under test"
        value={scheduler}
        onChange={setScheduler}
        options={scheds.map((s) => ({ value: s, label: label(s) }))}
        width={196}
      />
      <Select
        label="Compare with (A/B)"
        value={kernel ? "none" : compare}
        onChange={setCompare}
        disabled={kernel}
        options={[{ value: "none", label: "No comparison" }].concat(
          baselines.filter((b) => b !== scheduler).map((s) => ({ value: s, label: label(s) })),
        )}
        width={196}
      />
      <Select
        label="Scenario"
        value={scenario}
        onChange={setScenario}
        options={caps.scenarios.map((s) => ({ value: s.name, label: s.name === "sandbox" ? "Live household" : s.title }))}
        width={208}
      />
      {!kernel && (
        <Select
          label="Speed"
          value={speed}
          onChange={setSpeed}
          options={["1", "2", "4"].map((s) => ({ value: s, label: `${s}x speed` }))}
          width={104}
        />
      )}
      <div className="field">
        <label>&nbsp;</label>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn primary" onClick={start} disabled={busy}>
            <Icon name="play" size={14} />
            {running ? "Restart" : "Start session"}
          </button>
          {running && (
            <button className="btn danger" onClick={onStop} disabled={busy}>
              <Icon name="stop" size={13} />
              Stop
            </button>
          )}
        </div>
      </div>
      <div className="session-status">
        {running ? (
          <>
            <span className="dot live" />
            <span title={MODE_LABEL[session?.params?.mode ?? "sim"]}>
              Live <strong>{clockTime(t)}</strong>
            </span>
          </>
        ) : (
          <>
            <span className="dot" />
            <span>{session?.status === "finished" ? "Scenario finished" : "Idle"}</span>
          </>
        )}
      </div>
    </div>
  );
}
