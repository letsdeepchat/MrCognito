import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Dashboard as D, DashboardPoint } from "../api/types";

type Key = "working_memory" | "calculation_depth" | "trap_resistance" | "reasoning_alignment" | "adaptability";
const SERIES: { key: Key; label: string; max: number; unit: string; hint: string }[] = [
  { key: "working_memory", label: "Working memory", max: 1, unit: "%", hint: "Blindfold queries and position recall accuracy" },
  { key: "calculation_depth", label: "Calculation depth", max: 6, unit: " ply", hint: "Accurate plies in the lines you articulate" },
  { key: "trap_resistance", label: "Trap resistance", max: 1, unit: "%", hint: "How often you avoid the tempting human blunder" },
  { key: "reasoning_alignment", label: "Reasoning alignment", max: 1, unit: "%", hint: "How well your stated plan matches what your move does" },
];

function fmtVal(v: number | null, unit: string) {
  if (v === null || v === undefined) return "no data";
  return unit === "%" ? `${Math.round(v * 100)}%` : `${v.toFixed(1)}${unit}`;
}

function Spark({ pts, k, max, unit, label }: { pts: DashboardPoint[]; k: Key; max: number; unit: string; label: string }) {
  const W = 300, H = 84, pad = 6;
  const xs = (i: number) => pad + (i * (W - 2 * pad)) / Math.max(1, pts.length - 1);
  const ys = (v: number) => H - pad - (Math.min(v, max) / max) * (H - 2 * pad);
  const known = pts.map((p, i) => [i, p[k]] as const).filter(([, v]) => v !== null) as [number, number][];
  const path = known.map(([i, v], j) => `${j ? "L" : "M"}${xs(i).toFixed(1)},${ys(v).toFixed(1)}`).join(" ");
  const lastV = known.length ? known[known.length - 1][1] : null;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="spark" role="img" aria-label={`${label}: latest ${fmtVal(lastV, unit)}`}>
      <line x1={pad} x2={W - pad} y1={ys(max * 0.72)} y2={ys(max * 0.72)} className="ref" />
      {path && <path d={path} className="line" />}
      {known.map(([i, v]) => <circle key={i} cx={xs(i)} cy={ys(v)} r={2.2} className="dot"><title>{`${pts[i].day}: ${fmtVal(v, unit)}`}</title></circle>)}
    </svg>
  );
}

export function Dashboard() {
  const [d, setD] = useState<D | null>(null);
  useEffect(() => { api.dashboard(30).then(setD).catch(() => setD(null)); }, []);
  if (!d) return <p className="muted">Loading your 30-day trends…</p>;
  const latest = (k: Key) => [...d.series].reverse().find((p) => p[k] !== null)?.[k] ?? null;
  return (
    <section className="dashboard">
      <h2>Cognitive reserve, last 30 days</h2>
      <p className="muted small">No trophies, no streak counters: these trends measure effort and accuracy. The dashed line marks the 72 percent challenge zone.</p>
      <div className="tiles">
        {SERIES.map((s) => (
          <article key={s.key} className="card tile">
            <header><h3>{s.label}</h3><strong className="mono">{fmtVal(latest(s.key), s.unit)}</strong></header>
            <Spark pts={d.series} k={s.key} max={s.max} unit={s.unit} label={s.label} />
            <p className="muted small">{s.hint}</p>
          </article>
        ))}
        <article className="card tile">
          <header><h3>Daily consistency</h3><strong className="mono">{d.consistency.days_trained}/{d.days} days</strong></header>
          <div className="calendar" role="img" aria-label={`${d.consistency.days_trained} of ${d.days} days trained`}>
            {d.series.map((p) => <span key={p.day} title={`${p.day}: ${p.active_minutes} min`} className={p.adherent ? "on" : p.budget_minutes ? "part" : ""} />)}
          </div>
          <p className="muted small">One 10 to 30 minute session per day is the whole protocol.</p>
        </article>
      </div>
    </section>
  );
}
