# Cognito: a cognitive-reserve chess workout

A once-a-day, 10/20/30-minute chess workout that refuses to think for you.
No evaluation bar, no engine arrows, no free answers. The server holds every
piece of engine truth and only ever hands the user (or the LLM coach)
categorical, move-free facts.

```
browser (React/TS)  ──►  nginx  ──►  FastAPI ──► Stockfish pool (async UCI)
  masked board            /api        │        ├► human-move model (Maia-2 | heuristic)
  plan gate + mic                     │        ├► Socratic orchestrator ─► LLM (rule | Claude | vLLM+LoRA)
  timers, dashboard                   │        │     strict JSON tools, output guardrail
                                      │        └► RAG (pgvector | in-memory)
                                      └► PostgreSQL (profiles, sessions, telemetry, daily metrics)
```

## Quick start

```bash
# Full stack: Postgres + pgvector, API with Stockfish built from source, nginx frontend
docker compose up --build            # http://localhost:8080
STOCKFISH_SOURCE=apt docker compose up --build   # faster: distro Stockfish instead of compiling

# Local dev
cd backend && pip install -r requirements-dev.txt && uvicorn app.main:app --reload   # SQLite by default
cd frontend && npm install && npm run dev                                            # http://localhost:5173
cd backend && pytest                                                                 # 58 tests
```

Pick the coach with `COGNITO_LLM_PROVIDER`: `rule` (default, offline, deterministic),
`anthropic` (Claude via the Anthropic SDK, `ANTHROPIC_API_KEY`, model `COGNITO_LLM_MODEL`,
default `claude-opus-5-5`), or `openai_compat` (any OpenAI-compatible server such as vLLM
serving the LoRA adapter, `COGNITO_LLM_BASE_URL`). All config is in `backend/app/config.py`.

## How the invariants are enforced (server-side)

| Invariant | Where | Proven by |
|---|---|---|
| No raw best move, centipawns or win probability during active phases | Phase handlers build views from scratch; engine lines live only in `phase_state`. `LeakGuardRoute` scans every `/api` JSON response for engine keys and eval-shaped values and fails closed (500). LLM tools return categorical facts only. Every coach reply passes `guardrails.check_output` (move tokens not in the allow-list, `+1.3`, `cp`, `%`, `mate in N`, engine words, tier-1 square/piece names); one rewrite, then the deterministic coach. | `tests/test_no_leak.py`: an adversarial bot plays 6 full sessions (10/20/30 min, timed and untimed), spams offloading requests and hint-skips, and checks every response against the independently computed best move for every user-to-move position. Also: planted-leak route, jail-broken LLM, tool outputs. |
| Hints escalate exactly one tier per attempt | `coach/hints.py` `HintLadder`: next tier only, needs a new attempt since the last hint; tier 4 needs 3 failed attempts or forfeit | `tests/test_hint_ladder.py` (state machine + API) |
| Daily lock at the 10–30 min threshold, 24 h consolidation | `protocol/cooldown.py`: READY → ACTIVE → CONSOLIDATING; lock anchored at the hard deadline even if the user walks away; budget changes never unlock | `tests/test_daily_lock.py` (each budget at deadline −1 s / deadline / +24 h −1 s / +24 h) |
| Think-aloud gate on critical turns | `p3_sparring._move` returns 423 until a 3-part plan is accepted | no-leak bot exercises it every session |
| Blindfold masking cannot be bypassed in devtools | `masked_board()` strips colour / identity / everything before sending | `test_masking_hides_information_server_side` |
| Anti-engine-lookup | `integrity.py`: top-1 match ≥ 85 % with latency CV ≤ 0.25 over 8 critical moves → session hold + blindfold verification drill | `test_integrity_monitor` |

Two reveals are deliberate and spec-mandated: the unlabeled tier-3 candidate pair, and the
tier-4 guided post-mortem (after forfeit or an exhausted move budget, where the user still
enters every move by hand). The leak test treats both as explicit exceptions.

## The five phases

| Phase | Share | What the server does |
|---|---|---|
| 1 Blindfold warm-up | 15 % | Study → mask (silhouettes → coordinates → empty) → dictated moves (TTS) → computed spatial queries (piece location, attacker counts, two-move bishop reach, doubly-attacked squares) → play the final move from memory |
| 2 Pattern & inhibition | 20 % | Mined "trap" positions (human model likes a move that loses ≥ 200 cp). Name the motif and the tempting move *before* the board unlocks; per-puzzle clock and 3-move budget |
| 3 Think-aloud sparring | 35 % | Game vs a human-like opponent at your rating + 100. Critical turns (gap ≥ 80 cp, trap present, or serious threat) lock the board until you submit candidates / opponent's best reply / purpose (push-to-talk or typed). Overlooking a forcing refutation triggers a Socratic question and a recalculation |
| 4 Perturbation | 15 % | Constraint shift (a piece type frozen for 5 moves), tempo compression (one untimed move, then 45 s), perspective flip (defend the side you just played against) |
| 5 Recall | 15 % | Board cleared; rebuild the phase-3 turning point (F1 over pieces), name the key moment, tag a habit that greets you tomorrow |

Metrics (`app/telemetry.py`): CDI (accurate plies in articulated lines), WMRS, TRR, RMA
(plan↔move consistency, reply foresight, purpose↔consequence overlap), adaptability, a
fatigue curve, and a staircase that steers difficulty toward ~72 % success.

## What is real, what is stubbed

See [docs/STATUS.md](docs/STATUS.md) for the verification log and each pluggable adapter.

## Deploy to Render (no local server)

`render.yaml` is a Render Blueprint: one free web service built from the root `Dockerfile`
(React UI + FastAPI + Stockfish on one origin) and one free Postgres. On Render: **New → Blueprint**,
pick this repo, **Apply**. The app is then at `https://<service-name>.onrender.com` (UI at `/`, API at `/api`).
Free tier: the service sleeps after ~15 idle minutes (about a minute to wake), runs on 0.1 CPU (the
Blueprint lowers analysis depth to 10), and the free database expires after 30 days unless upgraded.
The APK defaults to `https://mrcognito.onrender.com`; to point builds elsewhere set the repository
variable `COGNITO_API_BASE` and re-run the "Android APK" workflow.

## Android app (APK)

Every push to `main` that touches `frontend/` builds a debug-signed APK in GitHub Actions
(`.github/workflows/android-apk.yml`) and publishes it to the **`android-latest`** release:
<https://github.com/letsdeepchat/MrCognito/releases/tag/android-latest>.

1. Run the backend where the phone can reach it: on Render (above), or `docker compose up --build` on a computer
   on the same Wi-Fi (the API listens on port 8000 on all interfaces).
2. Install the APK (allow "install unknown apps" for your browser or file manager).
3. On first launch enter the server address, e.g. `https://mrcognito.onrender.com` or `http://192.168.1.10:8000`.
   You can change it later under Settings → Server.

The app is the same React UI wrapped with Capacitor 8 (`frontend/android`). Build it yourself with
`cd frontend && npm ci && npm run build && npx cap sync android && cd android && ./gradlew assembleDebug`.
Push-to-talk relies on the Web Speech API, which the Android WebView usually lacks, so the plan gate falls back
to typing there.
