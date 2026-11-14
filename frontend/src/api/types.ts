export type Gate = "READY" | "ACTIVE" | "CONSOLIDATING";
export type PhaseName =
  | "BLINDFOLD_WARMUP"
  | "PATTERN_INHIBITION"
  | "SPARRING_VERBALIZED"
  | "ADAPTABILITY_PERTURBATION"
  | "RECALL_CONSOLIDATION";

export interface MaskedBoard {
  mask_level: 0 | 1 | 2 | 3;
  pieces: Record<string, string>;
  orientation: "white" | "black";
  side_to_move: "white" | "black";
  fen?: string;
}

export interface Profile {
  user_id: string;
  gate: Gate;
  daily_budget_minutes: 10 | 20 | 30;
  next_unlock_at: string | null;
  last_session_completed_at: string | null;
  active_session_id: string | null;
  estimated_human_elo: number;
  cognitive_indices: Record<string, number>;
  habit_for_tomorrow: string | null;
  wellness_acknowledged: boolean;
  wellness_notice: string;
  server_time: string;
}

export interface Query { id: string; kind: string; prompt: string }
export interface PostMortem { fen: string; step: number; steps: number; done: boolean; played_san: string[] }
export interface PlanFeedback { decision: string; message: string; question: string; source?: string }

// Phase views are open-ended dicts from the server; fields used by the UI are typed loosely.
export interface PhaseView {
  done?: boolean;
  step?: string;
  item?: number;
  items?: number;
  board?: MaskedBoard;
  instruction?: string;
  [k: string]: unknown;
}

export interface SessionView {
  session_id: string;
  state: "ACTIVE" | "COMPLETED";
  budget_minutes: number;
  started_at: string;
  hard_deadline: string;
  phase?: PhaseName;
  phase_index?: number;
  phase_count?: number;
  cognitive_target?: string;
  phase_started_at?: string;
  phase_deadline?: string;
  phase_seconds?: number;
  server_time?: string;
  mask_level?: number;
  integrity_hold?: boolean;
  integrity_drill?: { instruction: string; board: MaskedBoard; queries: Query[] };
  yesterday_habit?: string | null;
  fatigue_notice?: string | null;
  view?: PhaseView;
  end_reason?: string;
  ended_at?: string | null;
  next_unlock_at?: string | null;
  summary?: { metrics: Record<string, number | null>; active_minutes: number; fatigue_curve: { minute: number; mean_latency_ms: number; accuracy: number }[]; habit_for_tomorrow?: string };
}

export interface ActionResult { result: Record<string, any>; session: SessionView }
export interface Hint { tier: number; tier_name: string; message: string; question: string; candidates?: string[]; post_mortem?: PostMortem }

export interface DashboardPoint {
  day: string;
  working_memory: number | null;
  calculation_depth: number | null;
  trap_resistance: number | null;
  reasoning_alignment: number | null;
  adaptability: number | null;
  active_minutes: number;
  budget_minutes: number | null;
  adherent: boolean;
}
export interface Dashboard { days: number; series: DashboardPoint[]; consistency: { days_trained: number; adherence_rate: number } }
