import { useCallback, useEffect, useState } from "react";
import { api, ApiError, getServer, isNative, setServer } from "./api/client";
import type { PhaseName, Profile, SessionView } from "./api/types";
import { Dashboard } from "./components/Dashboard";
import { WellnessNotice, type Ctl } from "./components/Panels";
import { PhaseTimer, useCountdown } from "./components/PhaseTimer";
import { BlindfoldPhase, IntegrityDrill, PatternPhase, PerturbPhase, RecallPhase, SparringPhase } from "./phases/Phases";

const PHASE_TITLE: Record<PhaseName, string> = {
  BLINDFOLD_WARMUP: "Blindfold warm-up",
  PATTERN_INHIBITION: "Pattern & impulse control",
  SPARRING_VERBALIZED: "Think-aloud sparring",
  ADAPTABILITY_PERTURBATION: "Rule perturbation",
  RECALL_CONSOLIDATION: "Active recall",
};

type Tab = "train" | "dashboard" | "settings";

export default function App() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [session, setSession] = useState<SessionView | null>(null);
  const [last, setLast] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("train");

  const refreshProfile = useCallback(async () => setProfile(await api.me()), []);
  useEffect(() => {
    (async () => {
      try {
        await api.ensureUser();
        const p = await api.me();
        setProfile(p);
        if (p.active_session_id) setSession(await api.session(p.active_session_id));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Cannot reach the server.");
      }
    })();
  }, []);

  const ctl: Ctl | null = session && {
    sid: session.session_id,
    setError,
    refresh: async () => setSession(await api.session(session.session_id)),
    act: async (a) => {
      setError(null);
      try {
        const r = await api.act(session.session_id, a);
        setLast(r.result);
        setSession(r.session);
        if (r.session.state !== "ACTIVE") await refreshProfile();
        return r.result;
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "Something went wrong.");
        return null;
      }
    },
  };

  async function startSession() {
    setError(null);
    try {
      setLast(null);
      setSession(await api.start());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not start.");
      await refreshProfile();
    }
  }

  if ((isNative && !getServer()) || (error && !profile)) return <ServerSetup error={error} />;
  if (!profile) return <main className="shell"><p className="muted">Loading… A free-tier server that has been asleep can take up to a minute to wake up.</p></main>;
  if (!profile.wellness_acknowledged) {
    return (
      <main className="shell narrow">
        <h1>Cognito</h1>
        <p className="lead">A daily 10 to 30 minute chess workout that makes you do the thinking. No evaluation bars, no engine lines, no hints for free.</p>
        <WellnessNotice text={profile.wellness_notice} onAck={async () => setProfile(await api.ackWellness())} />
      </main>
    );
  }

  return (
    <main className="shell">
      <header className="top">
        <h1>Cognito</h1>
        <nav>
          {(["train", "dashboard", "settings"] as Tab[]).map((t) => (
            <button key={t} className={`tab ${tab === t ? "on" : ""}`} onClick={() => setTab(t)}>{t[0].toUpperCase() + t.slice(1)}</button>
          ))}
        </nav>
      </header>
      {error && <p className="warn" role="alert">{error}</p>}
      {tab === "dashboard" && <Dashboard />}
      {tab === "settings" && <SettingsView profile={profile} setProfile={setProfile} />}
      {tab === "train" && (
        session && session.state === "ACTIVE" && ctl ? (
          <Workout s={session} ctl={ctl} last={last} />
        ) : (
          <Home profile={profile} session={session} onStart={startSession} />
        )
      )}
    </main>
  );
}

function Workout({ s, ctl, last }: { s: SessionView; ctl: Ctl; last: Record<string, any> | null }) {
  const phase = s.phase!;
  const v = (s.view ?? {}) as Record<string, any>;
  const props = { s, v, ctl, last };
  return (
    <>
      <section className="phasebar">
        <ol className="phases">
          {Object.keys(PHASE_TITLE).map((p, i) => (
            <li key={p} className={i + 1 < (s.phase_index ?? 0) ? "done" : p === phase ? "on" : ""}>{PHASE_TITLE[p as PhaseName]}</li>
          ))}
        </ol>
        <div className="timers">
          <PhaseTimer label={`${PHASE_TITLE[phase]} · ${s.cognitive_target}`} deadline={s.phase_deadline} serverTime={s.server_time}
                      total={s.phase_seconds} onExpire={() => ctl.refresh()} />
          <PhaseTimer label="Session" deadline={s.hard_deadline} serverTime={s.server_time} onExpire={() => ctl.refresh()} />
        </div>
      </section>
      {s.yesterday_habit && phase === "BLINDFOLD_WARMUP" && <p className="habit">Today's habit from yesterday: <strong>{s.yesterday_habit}</strong></p>}
      {s.fatigue_notice && <p className="notice">{s.fatigue_notice}</p>}
      {s.integrity_hold ? <IntegrityDrill s={s} ctl={ctl} /> : v.done ? (
        <p className="muted">Moving to the next phase…</p>
      ) : (
        <>
          {phase === "BLINDFOLD_WARMUP" && <BlindfoldPhase {...props} />}
          {phase === "PATTERN_INHIBITION" && <PatternPhase {...props} />}
          {phase === "SPARRING_VERBALIZED" && <SparringPhase {...props} />}
          {phase === "ADAPTABILITY_PERTURBATION" && <PerturbPhase {...props} />}
          {phase === "RECALL_CONSOLIDATION" && <RecallPhase {...props} />}
        </>
      )}
      <footer className="row space">
        <span className="muted small">No evaluation bar. No arrows. Your brain is the engine.</span>
        <button className="ghost small" onClick={() => ctl.act({ type: "advance_phase" })}>Skip to next phase</button>
      </footer>
    </>
  );
}

function Home({ profile, session, onStart }: { profile: Profile; session: SessionView | null; onStart: () => void }) {
  const left = useCountdown(profile.next_unlock_at ?? undefined, profile.server_time);
  const m = session?.summary?.metrics;
  if (profile.gate === "CONSOLIDATING") {
    return (
      <section className="lock card">
        <h2>Consolidation window</h2>
        <p>Today's workout is complete. Memory consolidates between sessions, so training stays locked for 24 hours.</p>
        {left !== null && <p className="countdown mono">{`${Math.floor(left / 3600)}h ${String(Math.floor((left % 3600) / 60)).padStart(2, "0")}m ${String(left % 60).padStart(2, "0")}s`}</p>}
        {profile.habit_for_tomorrow && <p>Tomorrow's habit: <strong>{profile.habit_for_tomorrow}</strong></p>}
        {m && (
          <ul className="summary">
            {Object.entries(m).map(([k, val]) => <li key={k}><span>{k.replace(/_/g, " ")}</span><strong>{val === null ? "not measured" : k === "calculation_depth_index" ? `${val} ply` : `${Math.round((val as number) * 100)}%`}</strong></li>)}
          </ul>
        )}
      </section>
    );
  }
  return (
    <section className="card home">
      <h2>Today's workout · {profile.daily_budget_minutes} minutes</h2>
      <ol className="muted">
        <li>Blindfold warm-up (15%)</li><li>Pattern and impulse control (20%)</li><li>Think-aloud sparring (35%)</li>
        <li>Rule perturbation (15%)</li><li>Active recall (15%)</li>
      </ol>
      {profile.habit_for_tomorrow && <p>Habit to practise today: <strong>{profile.habit_for_tomorrow}</strong></p>}
      <button className="primary big" onClick={onStart}>Start session</button>
    </section>
  );
}

function ServerSetup({ error }: { error: string | null }) {
  const [url, setUrl] = useState(getServer() || "https://mrcognito.onrender.com");
  return (
    <main className="shell narrow">
      <h1>Cognito</h1>
      <section className="card stack">
        <h3>Connect to your Cognito server</h3>
        <p className="muted small">Enter your Cognito server's address, e.g. your Render URL (https://your-name.onrender.com) or http://your-computer-LAN-IP:8000 for a server on your Wi-Fi.</p>
        {error && <p className="warn">{error}</p>}
        <input value={url} onChange={(e) => setUrl(e.target.value)} inputMode="url" autoCapitalize="off" />
        <button className="primary" onClick={() => { setServer(url); window.location.reload(); }}>Connect</button>
      </section>
    </main>
  );
}

function SettingsView({ profile, setProfile }: { profile: Profile; setProfile: (p: Profile) => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  const [fen, setFen] = useState("");
  async function setBudget(b: number) {
    try { setProfile(await api.setBudget(b)); setMsg("Saved. Applies from your next session."); }
    catch (e) { setMsg(e instanceof ApiError ? e.message : "Could not save."); }
  }
  async function addFen() {
    try { const r = await api.addPosition(fen.trim()); setMsg(r.note); setFen(""); }
    catch (e) { setMsg(e instanceof ApiError ? e.message : "Invalid position."); }
  }
  async function photo(f: File | undefined) {
    if (!f) return;
    try {
      const r = await api.boardFromPhoto(f, "w");
      setFen(r.fen);
      setMsg(r.valid ? "Board recognised and saved for your next sparring phase." : `Check the position: ${r.issues.join(", ")}`);
    } catch (e) { setMsg(e instanceof ApiError ? e.message : "Recognition failed."); }
  }
  return (
    <section className="stack settings">
      <div className="card">
        <h3>Daily budget</h3>
        <div className="row">
          {[10, 20, 30].map((b) => <button key={b} className={`chip ${profile.daily_budget_minutes === b ? "on" : ""}`} onClick={() => setBudget(b)}>{b} min</button>)}
        </div>
        <p className="muted small">Changing the budget never shortens a consolidation lock.</p>
      </div>
      <div className="card">
        <h3>Bring your own position</h3>
        <p className="muted small">Paste a FEN or photograph a physical board. It becomes your next sparring position.</p>
        <div className="row"><input value={fen} onChange={(e) => setFen(e.target.value)} placeholder="FEN" /><button onClick={addFen}>Save</button></div>
        <label className="small">Photo of a board<input type="file" accept="image/*" capture="environment" onChange={(e) => photo(e.target.files?.[0])} /></label>
      </div>
      {isNative && (
        <div className="card">
          <h3>Server</h3>
          <p className="muted small mono">{getServer()}</p>
          <button onClick={() => { setServer(""); window.location.reload(); }}>Change server</button>
        </div>
      )}
      {msg && <p className="notice">{msg}</p>}
      <WellnessNotice text={profile.wellness_notice} compact />
    </section>
  );
}
