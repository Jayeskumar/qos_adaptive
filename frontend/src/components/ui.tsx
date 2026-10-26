import type { ReactNode } from "react";
import { Icon } from "./Icon";
import { STATUS_LABEL, type Status } from "../lib/qoe";

export function Card({ title, sub, icon, right, children, className = "", tight = false }: {
  title?: ReactNode; sub?: ReactNode; icon?: string; right?: ReactNode; children?: ReactNode; className?: string; tight?: boolean;
}) {
  return (
    <section className={`card ${tight ? "tight" : ""} ${className}`}>
      {(title || right) && (
        <div className="card-head">
          <div>
            {title && (
              <div className="card-title">
                {icon && <Icon name={icon} size={15} style={{ color: "var(--muted)" }} />}
                {title}
              </div>
            )}
            {sub && <div className="card-sub">{sub}</div>}
          </div>
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

const STATUS_ICON: Record<Status, string> = { good: "check", warning: "alert", serious: "alert", critical: "x", idle: "clock" };

export function StatusBadge({ status, label }: { status: Status; label?: string }) {
  return (
    <span className={`status ${status}`}>
      <span className="sicon"><Icon name={STATUS_ICON[status]} size={10} style={{ strokeWidth: 3 }} /></span>
      {label ?? STATUS_LABEL[status]}
    </span>
  );
}

export function Seg<T extends string>({ value, options, onChange, disabled }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; disabled?: boolean;
}) {
  return (
    <div className="seg" role="radiogroup">
      {options.map((o) => (
        <button
          key={o.value}
          className={o.value === value ? "on" : ""}
          onClick={() => !disabled && onChange(o.value)}
          disabled={disabled}
          role="radio"
          aria-checked={o.value === value}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Select({ label, value, onChange, options, disabled, width }: {
  label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[];
  disabled?: boolean; width?: number;
}) {
  return (
    <div className="field">
      <label>{label}</label>
      <select className="select" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} style={{ width }}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </div>
  );
}

export function Pill({ colour, children, title }: { colour: string; children: ReactNode; title?: string }) {
  return (
    <span className="pill" title={title} style={{ background: `color-mix(in srgb, ${colour} 20%, var(--surface-2))` }}>
      <span className="pdot" style={{ background: colour }} />
      {children}
    </span>
  );
}

export function Legend({ items }: { items: { label: string; colour: string; box?: boolean; dash?: boolean }[] }) {
  return (
    <div className="chart-legend">
      {items.map((i) => (
        <span className="key" key={i.label}>
          <span className={`swatch ${i.box ? "box" : ""} ${i.dash ? "dash" : ""}`} style={{ background: i.colour }} />
          {i.label}
        </span>
      ))}
    </div>
  );
}

export function Empty({ icon = "info", title, children }: { icon?: string; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <Icon name={icon} size={22} />
      <div style={{ color: "var(--ink-2)", fontWeight: 600 }}>{title}</div>
      {children && <div className="small">{children}</div>}
    </div>
  );
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="stat">
      <div className="s-label">{label}</div>
      <div className="s-value">{value}</div>
      {sub && <div className="s-sub">{sub}</div>}
    </div>
  );
}

/** Recharts LabelList renderer for horizontal bars: value at the bar tip, never wrapped. */
export function tipLabel(format: (v: number) => string, fill: string) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return function TipLabel(props: any) {
    const { x, y, width, height, value } = props;
    if (typeof value !== "number") return null;
    return (
      <text x={Number(x) + Number(width) + 6} y={Number(y) + Number(height) / 2} dy="0.35em" fill={fill} fontSize={11.5} fontWeight={600}>
        {format(value)}
      </text>
    );
  };
}
