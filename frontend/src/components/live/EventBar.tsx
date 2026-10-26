import type { Capabilities, Tick } from "../../lib/types";
import { APP_ICON } from "../../lib/qoe";
import { Icon } from "../Icon";
import { PRESET_SHORT } from "../../lib/labels";

export function EventBar({ caps, tick, onAction, disabled }: {
  caps: Capabilities;
  tick: Tick | null;
  onAction: (a: Record<string, unknown>) => void;
  disabled: boolean;
}) {
  const sides = tick ? Object.values(tick.sides) : [];
  const running = new Map<string, { label: string; type: string }>();
  for (const s of sides) {
    for (const a of s.apps) if (a.active && !running.has(a.app_id)) running.set(a.app_id, { label: a.label, type: a.app_type });
  }
  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="event-bar">
        <span className="label">Trigger an event</span>
        {caps.presets.map((p) => (
          <button
            key={p.name}
            className="btn event"
            disabled={disabled}
            title={p.description}
            onClick={() => onAction({ type: "preset", name: p.name })}
          >
            <Icon name={p.icon} size={14} />
            {PRESET_SHORT[p.name] ?? p.label}
          </button>
        ))}
      </div>
      <div className="event-bar">
        <span className="label">Running apps</span>
        {running.size === 0 && <span className="small muted">none yet</span>}
        {[...running.entries()].map(([id, a]) => (
          <span className="chip" key={id} title={`Stop ${a.label}`}>
            <Icon name={APP_ICON[a.type] ?? "activity"} size={13} />
            {a.label}
            <button disabled={disabled} onClick={() => onAction({ type: "stop_app", app_id: id })} aria-label={`stop ${a.label}`}>
              <Icon name="x" size={12} />
            </button>
          </span>
        ))}
      </div>
    </div>
  );
}
