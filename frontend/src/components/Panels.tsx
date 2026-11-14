import { useCallback, useState } from "react";
import { api, ApiError } from "../api/client";
import type { Hint, PlanFeedback } from "../api/types";
import { usePushToTalk } from "../hooks/useSpeech";

export interface Ctl {
  sid: string;
  act: (a: Record<string, unknown>) => Promise<Record<string, any> | null>;
  refresh: () => Promise<void>;
  setError: (e: string | null) => void;
}

/** Mandatory 3-part think-aloud gate (Phase 3). */
export function PlanGate({ ctl, fen, feedback }: { ctl: Ctl; fen: string; feedback?: PlanFeedback | null }) {
  const [c1, setC1] = useState("");
  const [c2, setC2] = useState("");
  const [reply, setReply] = useState("");
  const [purpose, setPurpose] = useState("");
  const [line, setLine] = useState("");
  const [transcript, setTranscript] = useState("");
  const [busy, setBusy] = useState(false);

  const onFinal = useCallback(async (text: string) => {
    setTranscript(text);
    try {
      const p = await api.parsePlan(fen, text);
      if (p.candidates[0]) setC1(p.candidates[0]);
      if (p.candidates[1]) setC2(p.candidates[1]);
      if (p.opponent_reply) setReply(p.opponent_reply);
      if (p.purpose) setPurpose(p.purpose);
    } catch { /* leave fields for manual entry */ }
  }, [fen]);
  const ptt = usePushToTalk(onFinal);

  async function submit() {
    setBusy(true);
    await ctl.act({
      type: "sparring_plan", candidates: [c1, c2].filter(Boolean), chosen: c1, opponent_reply: reply,
      purpose, variation: line.split(/[\s,]+/).filter(Boolean).slice(0, 6), transcript,
    });
    setBusy(false);
  }

  return (
    <section className="card plan">
      <h3>Think aloud before you move</h3>
      <div className="ptt">
        <button
          className={`mic ${ptt.listening ? "live" : ""}`}
          disabled={!ptt.supported}
          onPointerDown={ptt.start}
          onPointerUp={ptt.stop}
          onPointerLeave={() => ptt.listening && ptt.stop()}
          aria-pressed={ptt.listening}
        >
          {ptt.listening ? "Listening… release to finish" : ptt.supported ? "Hold to talk" : "Voice input not supported in this browser"}
        </button>
        <p className="muted small">Say it like: “Candidates knight f5 and rook d1. They reply bishop takes f5. Purpose: pressure d6 and stop …”</p>
        {ptt.interim && <p className="interim">{ptt.interim}</p>}
      </div>
      <label>1 · Candidate moves (at least two)
        <div className="row"><input value={c1} onChange={(e) => setC1(e.target.value)} placeholder="first choice, e.g. Nf5" />
          <input value={c2} onChange={(e) => setC2(e.target.value)} placeholder="alternative" /></div>
      </label>
      <label>2 · Opponent's strongest reply to your first choice
        <input value={reply} onChange={(e) => setReply(e.target.value)} placeholder="e.g. Bxf5, or 'check on the b-file'" />
      </label>
      <label>3 · Strategic purpose
        <textarea value={purpose} onChange={(e) => setPurpose(e.target.value)} rows={2} placeholder="How it improves coordination or restricts counterplay" />
      </label>
      <label className="muted small">Optional: the line you calculated after that (SAN, space separated)
        <input value={line} onChange={(e) => setLine(e.target.value)} placeholder="e.g. Qg4 g6 Nh6+" />
      </label>
      <button className="primary" disabled={busy || !c1 || !c2 || !reply || purpose.split(" ").length < 3} onClick={submit}>
        Submit plan
      </button>
      {feedback && <Feedback f={feedback} />}
    </section>
  );
}

function Feedback({ f }: { f: PlanFeedback }) {
  return (
    <div className={`socratic ${f.decision}`}>
      <p>{f.message}</p>
      <p className="q">{f.question}</p>
    </div>
  );
}

const TIER_LABEL = ["", "Concept", "Sector", "Compare two", "Walkthrough"];

export function HintPanel({ ctl, tier, canForfeit }: { ctl: Ctl; tier: number; canForfeit?: boolean }) {
  const [hints, setHints] = useState<Hint[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  async function ask(forfeit = false) {
    setMsg(null);
    try {
      const r = await api.hint(ctl.sid, tier + 1, forfeit);
      setHints((h) => [...h, r.hint]);
      await ctl.refresh();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Hint unavailable.");
    }
  }
  return (
    <section className="card hints">
      <div className="row space">
        <h3>Socratic ladder</h3>
        <ol className="ladder" aria-label="hint tiers">
          {[1, 2, 3, 4].map((t) => <li key={t} className={t <= tier ? "on" : ""}>{TIER_LABEL[t]}</li>)}
        </ol>
      </div>
      {hints.map((h, i) => (
        <div key={i} className="socratic">
          <p className="muted small">Tier {h.tier} · {TIER_LABEL[h.tier]}</p>
          <p>{h.message}</p>
          <p className="q">{h.question}</p>
        </div>
      ))}
      {tier < 4 && <button onClick={() => ask(false)}>Next hint (tier {tier + 1})</button>}
      {canForfeit && tier === 3 && <button className="ghost" onClick={() => ask(true)}>I give up: walk me through it</button>}
      {msg && <p className="warn small">{msg}</p>}
      <p className="muted small">One tier per attempt. Make a move or state a plan before the next hint.</p>
    </section>
  );
}

export function CoachPanel({ sid }: { sid: string }) {
  const [log, setLog] = useState<{ who: "you" | "coach"; text: string }[]>([]);
  const [text, setText] = useState("");
  async function send() {
    if (!text.trim()) return;
    const t = text.trim();
    setText("");
    setLog((l) => [...l, { who: "you", text: t }]);
    try {
      const r = await api.coach(sid, t);
      setLog((l) => [...l, { who: "coach", text: `${r.message} ${r.question}`.trim() }]);
    } catch (e) {
      setLog((l) => [...l, { who: "coach", text: e instanceof ApiError ? e.message : "Coach unavailable." }]);
    }
  }
  return (
    <section className="card coach">
      <h3>Coach</h3>
      <div className="chat" aria-live="polite">
        {log.map((m, i) => <p key={i} className={m.who}>{m.text}</p>)}
      </div>
      <div className="row">
        <input value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()}
               placeholder="Ask about ideas, not answers" />
        <button onClick={send}>Ask</button>
      </div>
    </section>
  );
}

export function WellnessNotice({ text, onAck, compact }: { text: string; onAck?: () => void; compact?: boolean }) {
  return (
    <section className={`card wellness ${compact ? "compact" : ""}`} role="note" aria-label="wellness notice">
      <h3>About this app</h3>
      <p>{text}</p>
      {onAck && <button className="primary" onClick={onAck}>I understand</button>}
    </section>
  );
}
