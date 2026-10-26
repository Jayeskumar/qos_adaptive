import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type Theme = "dark" | "light";

// Categorical slots validated with the dataviz validator against this app's own surfaces
// (dark #17191d, light #ffffff): adjacent-pair CVD dE >= 8.4, normal-vision dE >= 19.3.
const SERIES = {
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
};

export const STATUS = { good: "#0ca30c", warning: "#fab219", serious: "#ec835a", critical: "#d03b3b" };

export interface Palette {
  theme: Theme;
  series: string[];
  flowpilot: string;
  baseline: string;
  neutral: string;
  grid: string;
  axis: string;
  ink: string;
  ink2: string;
  muted: string;
  surface: string;
  app: Record<string, string>;
  cls: Record<number, string>;
}

function palette(theme: Theme): Palette {
  const s = SERIES[theme];
  const app = {
    streaming: s[0],
    bulk: s[1],
    video_call: s[2],
    web: s[3],
    upload: s[4],
    gaming: s[6],
  };
  return {
    theme,
    series: s,
    flowpilot: s[0],
    baseline: s[1],
    neutral: theme === "dark" ? "#5c6270" : "#a9adb6",
    grid: theme === "dark" ? "#262a31" : "#e6e8ec",
    axis: theme === "dark" ? "#3a3f48" : "#c7cad1",
    ink: theme === "dark" ? "#eef0f3" : "#0d0f12",
    ink2: theme === "dark" ? "#c1c6d0" : "#4b5160",
    muted: theme === "dark" ? "#8a909c" : "#727887",
    surface: theme === "dark" ? "#17191d" : "#ffffff",
    app,
    // class colours follow the application family each class exists for
    cls: { 10: app.gaming, 20: app.video_call, 30: app.web, 40: app.streaming, 50: app.bulk },
  };
}

interface Ctx {
  theme: Theme;
  setTheme: (t: Theme) => void;
  pal: Palette;
}

const ThemeCtx = createContext<Ctx | null>(null);

function initialTheme(): Theme {
  try {
    const t = localStorage.getItem("flowpilot.theme");
    if (t === "light" || t === "dark") return t;
  } catch {
    /* storage unavailable: fall through */
  }
  return "dark";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(initialTheme);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("flowpilot.theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  const value = useMemo(() => ({ theme, setTheme: setThemeState, pal: palette(theme) }), [theme]);
  return <ThemeCtx.Provider value={value}>{children}</ThemeCtx.Provider>;
}

export function useTheme(): Ctx {
  const c = useContext(ThemeCtx);
  if (!c) throw new Error("ThemeProvider missing");
  return c;
}

export function classColour(pal: Palette, tc: number): string {
  if (tc >= 50) return pal.cls[50];
  return pal.cls[tc] ?? pal.neutral;
}
