import { useEffect, useRef, useState } from "react";
import type { MaskedBoard, PlanFeedback, PostMortem, Query, SessionView } from "../api/types";
import { Board, Palette, piecesToPlacement } from "../components/Board";
import { CoachPanel, HintPanel, PlanGate, type Ctl } from "../components/Panels";
import { PhaseTimer } from "../components/PhaseTimer";
import { sanToSpeech, speak } from "../hooks/useSpeech";

type V = Record<string, any>;
type P = { s: SessionView; v: V; ctl: Ctl; last: Record<string, any> | null };

function Socratic({ r }: { r: Record<string, any> | null }) {
  if (!r) return null;
  const msg = r.message ?? r.debrief ?? r.coach?.message;
  const q = r.question ?? r.coach?.question;
  if (!msg && !q && !r.opponent_move) return null;
  return (
    <div className="socratic">
      {r.opponent_move && <p className="muted small">Opponent played <strong>{r.opponent_move}</strong></p>}
      {msg && <p>{msg}</p>}
      {q && <p className="q">{q}</p>}
      {r.post_mortem_reveal && <p className="small">Enter <strong>{r.post_mortem_reveal}</strong> on the board.</p>}
    </div>
  );
}

function PostMortemBoard({ pm, ctl }: { pm: PostMortem; ctl: Ctl }) {
  const board: MaskedBoard = fenToMasked(pm.fen);
  return (
    <section className="card">
      <h3>Guided walkthrough · step {Math.min(pm.step, pm.steps)} of {pm.steps}</h3>
      <p className="muted small">Enter every move of the forcing sequence yourself.</p>
      <Board board={board} onMove={(uci) => ctl.act({ type: "post_mortem_move", uci })} />
      {pm.played_san.length > 0 && <p className="mono small">{pm.played_san.join(" ")}</p>}
    </section>
  );
}

export function fenToMasked(fen: string): MaskedBoard {
  const [placement, turn] = fen.split(" ");
  const pieces: Record<string, string> = {};
  placement.split("/").forEach((row, i) => {
    let f = 0;
    for (const ch of row) {
      if (/\d/.test(ch)) f += Number(ch);
      else { pieces["abcdefgh"[f] + (8 - i)] = ch; f++; }
    }
  });
  const stm = turn === "b" ? "black" : "white";
  return { mask_level: 0, pieces, orientation: stm, side_to_move: stm, fen };
}

// ---------------------------------------------------------------- Phase 1
export function BlindfoldPhase({ v, ctl, last }: P) {
  const [revealed, setRevealed] = useState(0);
  const [answer, setAnswer] = useState("");
  const t0 = useRef(Date.now());
  const moves: string[] = v.dictated_moves ?? [];
  useEffect(() => {
    if (v.step !== "dictation") return;
    setRevealed(0);
    let i = 0;
    const tick = () => {
      if (i >= moves.length) return;
      speak(sanToSpeech(moves[i]));
      i++;
      setRevealed(i);
      timer = window.setTimeout(tick, 3200);
    };
    let timer = window.setTimeout(tick, 600);
    return () => window.clearTimeout(timer);
  }, [v.step, v.item]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { t0.current = Date.now(); }, [v.step, (v.queries ?? []).length]);
  const q: Query | undefined = (v.queries ?? [])[0];
  return (
    <div className="grid">
      <div>
        <Board board={v.board} disabled={v.step !== "final"}
               onMove={(uci) => ctl.act({ type: "blindfold_final_move", uci, think_ms: Date.now() - t0.current })} />
      </div>
      <div className="stack">
        <section className="card">
          <p className="muted small">Fragment {v.item}/{v.items} · {v.opening}</p>
          <p>{v.instruction}</p>
          {v.step === "study" && <button className="primary" onClick={() => ctl.act({ type: "blindfold_ready" })}>Ready: fade the pieces</button>}
          {(v.step === "dictation" || v.step === "queries") && (
            <div className="dictation" aria-live="polite">
              {moves.map((m, i) => (
                <span key={i} className={i < revealed || v.step === "queries" ? "" : "hidden"}>
                  <small>{i + 1}.</small> {i < revealed || v.step === "queries" ? m : "…"}
                </span>
              ))}
            </div>
          )}
          {v.step === "dictation" && (
            <div className="row">
              <button onClick={() => moves.forEach((m, i) => setTimeout(() => speak(sanToSpeech(m)), i * 2500))}>Replay audio</button>
              <button className="primary" disabled={revealed < moves.length} onClick={() => ctl.act({ type: "blindfold_dictation_done" })}>I have it</button>
            </div>
          )}
          {v.step === "queries" && q && (
            <form className="stack" onSubmit={(e) => { e.preventDefault(); ctl.act({ type: "blindfold_answer", query_id: q.id, answer, think_ms: Date.now() - t0.current }); setAnswer(""); }}>
              <label>{q.prompt}
                {q.kind === "yesno" ? (
                  <div className="row"><button type="submit" onClick={() => setAnswer("yes")}>Yes</button><button type="submit" onClick={() => setAnswer("no")}>No</button></div>
                ) : (
                  <input autoFocus value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder={q.kind === "count" ? "number" : "square, e.g. f5"} />
                )}
              </label>
              {q.kind !== "yesno" && <button className="primary" type="submit">Answer</button>}
            </form>
          )}
        </section>
        {last && "correct" in last && (
          <p className={last.correct ? "ok" : "warn"}>{last.correct ? "Correct." : `Not quite${last.expected !== undefined ? `: ${last.expected}` : ""}.`} {last.message ?? ""}</p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Phase 2
export function PatternPhase({ v, ctl, last }: P) {
  const [motif, setMotif] = useState("");
  const [tempting, setTempting] = useState("");
  const [why, setWhy] = useState("");
  const t0 = useRef(Date.now());
  useEffect(() => { t0.current = Date.now(); setMotif(""); setTempting(""); setWhy(""); }, [v.item]);
  const fen = v.board?.fen ?? "";
  return (
    <div className="grid">
      <div>
        <Board board={v.board} disabled={v.step !== "move"}
               onMove={(uci) => ctl.act({ type: "pattern_move", uci, think_ms: Date.now() - t0.current })} />
        <PhaseTimer label="Puzzle clock" deadline={v.item_deadline} total={v.time_budget_s} />
        <p className="muted small">Moves left: {v.attempts_left}</p>
      </div>
      <div className="stack">
        <section className="card">
          <p className="muted small">Puzzle {v.item}/{v.items}</p>
          <p>{v.instruction}</p>
          {v.step === "diagnose" && (
            <div className="stack">
              <fieldset className="chips"><legend>Underlying motif</legend>
                {(v.motif_options as string[]).map((m) => (
                  <button key={m} className={`chip ${motif === m ? "on" : ""}`} onClick={() => setMotif(m)}>{m.replace(/_/g, " ")}</button>
                ))}
              </fieldset>
              <label>The tempting move most players would play<input value={tempting} onChange={(e) => setTempting(e.target.value)} placeholder="SAN, e.g. Nxe5" /></label>
              <label>Why it fails<textarea rows={2} value={why} onChange={(e) => setWhy(e.target.value)} /></label>
              <button className="primary" disabled={!motif} onClick={() => ctl.act({ type: "pattern_diagnose", motif, tempting_move: tempting, why })}>
                Lock in diagnosis
              </button>
            </div>
          )}
          {v.diagnosis_feedback && (
            <ul className="small"><li>{v.diagnosis_feedback.motif}</li><li>{v.diagnosis_feedback.tempting}</li></ul>
          )}
          {v.candidates && <p>Compare: <strong>{v.candidates.join("  vs  ")}</strong></p>}
        </section>
        <Socratic r={last} />
        {v.post_mortem && <PostMortemBoard pm={v.post_mortem} ctl={ctl} />}
        {v.step !== "post_mortem" && <HintPanel key={fen} ctl={ctl} tier={v.hint_tier ?? 0} canForfeit />}
        <CoachPanel sid={ctl.sid} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Phase 3
export function SparringPhase({ v, ctl, last }: P) {
  const t0 = useRef(Date.now());
  const fen = v.board?.fen ?? "";
  useEffect(() => { t0.current = Date.now(); }, [fen]);
  return (
    <div className="grid">
      <div>
        <Board board={v.board} disabled={v.plan_required} lastMove={last?.opponent_move_uci}
               onMove={(uci) => ctl.act({ type: "sparring_move", uci, think_ms: Date.now() - t0.current })} />
        <p className="muted small">Move {v.moves_made + 1} of {v.max_moves} · you are {v.user_color}{v.theme ? ` · ${v.theme}` : ""}</p>
        {v.plan_required && <p className="lock-note">Board locked: critical moment. Submit your plan to unlock.</p>}
      </div>
      <div className="stack">
        <Socratic r={last} />
        {v.plan_required || v.plan_feedback ? (
          <PlanGate key={fen} ctl={ctl} fen={fen} feedback={v.plan_feedback as PlanFeedback | null} />
        ) : <section className="card"><p>{v.instruction}</p></section>}
        {v.candidates && <p>Compare: <strong>{v.candidates.join("  vs  ")}</strong></p>}
        {v.post_mortem && <PostMortemBoard pm={v.post_mortem} ctl={ctl} />}
        <HintPanel key={fen} ctl={ctl} tier={v.hint_tier ?? 0} canForfeit />
        <CoachPanel sid={ctl.sid} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Phase 4
export function PerturbPhase({ v, ctl, last }: P) {
  const t0 = useRef(Date.now());
  useEffect(() => { t0.current = Date.now(); }, [v.board?.fen]);
  return (
    <div className="grid">
      <div className={v.flipped ? "flip-anim" : ""}>
        <Board board={v.board} lastMove={last?.opponent_move_uci}
               onMove={(uci) => ctl.act({ type: "perturb_move", uci, think_ms: Date.now() - t0.current })} />
        {v.deadline && <PhaseTimer label="Compressed clock" deadline={v.deadline} total={45} />}
      </div>
      <div className="stack">
        <section className="card">
          <p className="muted small">Drill {v.drill}/{v.drills} · {String(v.kind).replace(/_/g, " ")}</p>
          <p className="big">{v.instruction}</p>
          {v.forbidden_piece && <p className="warn">Frozen: your {v.forbidden_piece}</p>}
          <p className="muted small">Moves left: {v.moves_left}</p>
        </section>
        <Socratic r={last} />
        {last?.drill_complete && <p className={last.result === "held" ? "ok" : "warn"}>Drill result: {last.result}</p>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Phase 5
export function RecallPhase({ v, ctl, last }: P) {
  const [pieces, setPieces] = useState<Record<string, string>>({});
  const [brush, setBrush] = useState<string | null>("P");
  const [moment, setMoment] = useState("");
  const [habit, setHabit] = useState("");
  return (
    <div className="grid">
      <div>
        {v.step === "reconstruct" ? (
          <>
            <Board board={v.board} edit={{ pieces, onChange: setPieces, brush }} />
            <Palette brush={brush} setBrush={setBrush} />
          </>
        ) : <Board board={v.board} disabled />}
      </div>
      <div className="stack">
        <section className="card">
          <p>{v.instruction}</p>
          {v.step === "reconstruct" && (
            <button className="primary" onClick={() => ctl.act({ type: "recall_reconstruct", placement: piecesToPlacement(pieces) })}>
              Submit reconstruction
            </button>
          )}
          {v.step === "reflect" && (
            <div className="stack">
              <p>Reconstruction accuracy: <strong>{Math.round((v.accuracy ?? last?.accuracy ?? 0) * 100)} percent</strong></p>
              <label>The key mistake or breakthrough, in your words<textarea rows={3} value={moment} onChange={(e) => setMoment(e.target.value)} /></label>
              <fieldset className="chips"><legend>One mental habit for tomorrow</legend>
                {(v.habit_tags as string[]).map((h) => <button key={h} className={`chip ${habit === h ? "on" : ""}`} onClick={() => setHabit(h)}>{h}</button>)}
              </fieldset>
              <input value={habit} onChange={(e) => setHabit(e.target.value)} placeholder="or write your own" />
              <button className="primary" disabled={moment.split(" ").length < 5 || !habit}
                      onClick={() => ctl.act({ type: "recall_reflect", key_moment: moment, habit_tag: habit })}>
                Finish today's workout
              </button>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

export function IntegrityDrill({ s, ctl }: { s: SessionView; ctl: Ctl }) {
  const d = s.integrity_drill!;
  const [ans, setAns] = useState<Record<string, string>>({});
  return (
    <div className="grid">
      <Board board={d.board} disabled />
      <section className="card">
        <h3>Blindfold verification</h3>
        <p>{d.instruction}</p>
        {d.queries.map((q) => (
          <label key={q.id}>{q.prompt}<input value={ans[q.id] ?? ""} onChange={(e) => setAns({ ...ans, [q.id]: e.target.value })} /></label>
        ))}
        <button className="primary" onClick={() => ctl.act({ type: "integrity_verify", answers: ans })}>Submit</button>
      </section>
    </div>
  );
}
