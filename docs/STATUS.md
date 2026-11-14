# Status: verified, pluggable, and stubbed

Checked on 2026-11-14.

## Engines and models

| Component | Status | Notes |
|---|---|---|
| Stockfish | **Real.** Any UCI build. `backend/Dockerfile` compiles from the `SF_REF` tag (default `sf_19`). | The npm `stockfish` package is at 19.0.1 (Oct 2026), which is consistent with a Stockfish 19 release, but GitHub was not reachable from the build sandbox, so I could **not** confirm the exact git tag name or the "SFNNv16" network name from the spec. If `sf_19` is wrong, pass `SF_REF=<tag>`. All local testing used Stockfish 16 (Ubuntu package); nothing in the code depends on the version. |
| Stockfish WASM on the client | **Deliberately not shipped.** | A browser-side engine is one devtools call away from the answer, which breaks the zero-answer invariant. If you want it, gate it to a post-lock review mode. |
| Maia-2 | **Adapter implemented** (`engines/human_model.py`, `Maia2Model`), matches the `maia2` 0.10/0.11 API (`model.from_pretrained`, `inference.prepare`, `inference.inference_each`). | Its weights download from Google Drive, which was blocked here, so it was not executed. Enable with `pip install -r requirements-ml.txt` and `COGNITO_HUMAN_MODEL=maia2`. |
| Human-model fallback | **Real, default.** `HeuristicHumanModel`: rating-dependent softmax over engine MultiPV + salience priors (captures, checks, recaptures, big-piece attacks). | Good enough to mine traps and drive a human-like opponent; not a substitute for Maia-2 calibration. |
| Chessformer | **Not integrated.** | Implement the `HumanMoveModel` protocol (one `predict` method). |
| Pure-Python engine | **Real, test/dev fallback.** | The whole test-suite also passes on it (`COGNITO_FORCE_MATERIAL_ENGINE=1`). |

## Coach

| Component | Status |
|---|---|
| Orchestrator, strict JSON tool schemas (5 tools + `socratic_reply`), schema validation, output guardrail, rewrite-then-fallback | Real, tested |
| Rule-based Socratic coach | Real, default, offline |
| Claude (`anthropic` provider) | Implemented with the official SDK (strict tools, low effort, server-side refusal fallback). Not called in tests: no API key in the sandbox |
| OpenAI-compatible provider (vLLM + LoRA) | Implemented, not exercised (no model server here) |
| LoRA/QLoRA fine-tune | `training/build_sft_dataset.py` (runs; 80 bootstrap examples from mined positions, every target guardrail-checked) and `training/finetune_lora.py` (TRL + PEFT, 4-bit). **The grandmaster Socratic dialogue corpus is not included**: it has to be curated/licensed; the builder takes it via `--dialogues`. |
| RAG | 26 move-free principles (`coach/knowledge/principles.yaml`). Feature-hashing embedder; in-memory store (default) and pgvector store (verified against Postgres in docker-compose). Swap in a sentence-transformer by replacing `HashingEmbedder.embed`. |

## Content

* `app/content/puzzles.json`: 40 trap puzzles mined locally with Stockfish 16 + the heuristic human model
  (`scripts/mine_content.py`). Motif labels come from a heuristic classifier of the refutation and skew
  toward `hanging_piece`; for production, mine from the Lichess puzzle database (its theme tags are
  human-reviewed) and re-run the trap filter with Maia-2.
* `app/content/sparring.json`: 74 balanced middlegame positions from the same run.
* Opening lines (Phase 1) and endgames (Phase 4) are hand-written and validated by a test.

## Vision (photo → FEN)

Server adapter, homography grid mapping and FEN validation are implemented and unit-tested
(`app/vision/recognizer.py`). **No trained weights**: see `backend/training/vision/README.md`.
Without a model the endpoint returns 503 and the UI falls back to FEN entry.

## Verification log

* `pytest`: 58 passed on real Stockfish; 57 passed on the pure-Python engine before the vision test was added.
* Frontend: `tsc` + `vite build` clean; clicked through all five phases in headless Chromium.
* docker-compose: images built and run (Postgres 16 + pgvector, API, nginx). A scripted user completed a
  full 10-minute session through nginx with every response audited for leaks; telemetry, the pgvector RAG
  table, the 24 h lock (`423` on restart) and the dashboard were all confirmed against Postgres.
  The sandbox could not reach GitHub or Debian mirrors, so for that run the image used the host's
  Stockfish 16 binary instead of compiling `sf_19`; the compile stage itself has not been executed.

## Known limitations / next steps

* Auth is a bearer token per anonymous user; put a real identity provider in front for multi-device use.
* Schema is created with `create_all`; add Alembic before the first production migration.
* Day boundaries for the dashboard are UTC.
* RMA is a transparent heuristic; an LLM judge could replace the purpose↔consequence term.
