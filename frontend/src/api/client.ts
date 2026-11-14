import type { ActionResult, Dashboard, Hint, Profile, SessionView } from "./types";

const TOKEN_KEY = "cognito.token";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public detail: any) {
    super(message);
  }
}

function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
function setToken(t: string) {
  try { localStorage.setItem(TOKEN_KEY, t); } catch { /* private mode: token lives in memory only */ }
  memToken = t;
}
let memToken: string | null = null;

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = memToken ?? getToken();
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (!(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(path, { ...init, headers });
  const text = await r.text();
  const body = text ? JSON.parse(text) : null;
  if (!r.ok) {
    const d = body?.detail ?? {};
    throw new ApiError(r.status, d.code ?? String(r.status), d.message ?? (typeof d === "string" ? d : r.statusText), d);
  }
  return body as T;
}

export const api = {
  async ensureUser(): Promise<void> {
    if (memToken ?? getToken()) {
      try { await req<Profile>("/api/me"); return; } catch (e) { if (!(e instanceof ApiError && e.status === 401)) throw e; }
    }
    const u = await req<{ token: string }>("/api/users", { method: "POST", body: JSON.stringify({}) });
    setToken(u.token);
  },
  me: () => req<Profile>("/api/me"),
  ackWellness: () => req<Profile>("/api/me/wellness-ack", { method: "POST" }),
  setBudget: (m: number) => req<Profile>("/api/me/settings", { method: "PUT", body: JSON.stringify({ daily_budget_minutes: m }) }),
  start: () => req<SessionView>("/api/sessions", { method: "POST" }),
  current: () => req<SessionView>("/api/sessions/current"),
  session: (id: string) => req<SessionView>(`/api/sessions/${id}`),
  act: (id: string, action: Record<string, unknown>) =>
    req<ActionResult>(`/api/sessions/${id}/action`, { method: "POST", body: JSON.stringify({ action }) }),
  hint: (id: string, requested_tier?: number, forfeit = false) =>
    req<{ hint: Hint; session: SessionView }>(`/api/sessions/${id}/hint`, {
      method: "POST", body: JSON.stringify({ requested_tier, forfeit }),
    }),
  coach: (id: string, message: string) =>
    req<{ refused: boolean; message: string; question: string }>(`/api/sessions/${id}/coach`, {
      method: "POST", body: JSON.stringify({ message }),
    }),
  parsePlan: (fen: string, transcript: string) =>
    req<{ candidates: string[]; opponent_reply: string; purpose: string; complete: boolean }>("/api/plan/parse", {
      method: "POST", body: JSON.stringify({ fen, transcript }),
    }),
  dashboard: (days = 30) => req<Dashboard>(`/api/dashboard?days=${days}`),
  addPosition: (fen: string) => req<{ saved: boolean; fen: string; note: string }>("/api/positions", { method: "POST", body: JSON.stringify({ fen }) }),
  boardFromPhoto: (file: File, side: "w" | "b") => {
    const fd = new FormData();
    fd.append("image", file);
    return req<{ fen: string; valid: boolean; confidence: number; issues: string[] }>(`/api/vision/board-to-fen?side_to_move=${side}`, { method: "POST", body: fd });
  },
};
