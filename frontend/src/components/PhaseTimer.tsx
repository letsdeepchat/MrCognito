import { useEffect, useState } from "react";

/** Counts down to a server deadline, corrected for client/server clock skew. */
export function useCountdown(deadline?: string, serverTime?: string) {
  const [skew] = useState(() => (serverTime ? Date.parse(serverTime) - Date.now() : 0));
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(t);
  }, []);
  if (!deadline) return null;
  return Math.max(0, Math.round((Date.parse(deadline) - (now + skew)) / 1000));
}

export function fmt(s: number) {
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

export function PhaseTimer(props: { label: string; deadline?: string; serverTime?: string; total?: number; onExpire?: () => void }) {
  const left = useCountdown(props.deadline, props.serverTime);
  const [fired, setFired] = useState(false);
  useEffect(() => {
    if (left === 0 && !fired && props.onExpire) { setFired(true); props.onExpire(); }
  }, [left, fired, props]);
  if (left === null) return null;
  const pct = props.total ? Math.min(100, (left / props.total) * 100) : 0;
  return (
    <div className="timer" aria-live="off">
      <div className="timer-row"><span className="muted small">{props.label}</span><strong className="mono">{fmt(left)}</strong></div>
      {props.total ? <div className="bar"><div className="bar-fill" style={{ width: `${pct}%` }} /></div> : null}
    </div>
  );
}
