# `metix-switch-sim` — Workspace Rules & Architectural Invariants

This project models Mozambique's **METIX / SIMOrede** instant payment switch architecture under **Banco de Moçambique Notice No. 1/GBM/2026**. Every change to this repository must preserve the invariants below.

---

## 1. Tech Stack & Tooling
- **Backend (`backend/`):** Python 3.14, FastAPI, async SQLAlchemy 2.x (`asyncpg`), Pydantic v2, PostgreSQL 16, Redis 7 (`redis.asyncio`).
- **Package Management & Linting:** Use `uv` for dependency management and virtual environments (`uv sync`, `uv run pytest`, `uv run ruff check .`). All Python code must pass `ruff check` and `ruff format --check`.
- **Frontend (`frontend/`):** Next.js (TypeScript, App Router) + Tailwind CSS.
- **Infrastructure:** `docker-compose.yml` orchestrating `postgres` (16), `redis` (7), `switch-api` (FastAPI), `sla-sweeper` (background worker), and `frontend` (Next.js).

---

## 2. Financial & Ledger Invariants (Non-Negotiable)
1. **Minor Units Only:** All monetary amounts are stored and computed as `int` (`BIGINT` in PostgreSQL) in **centavos** (1 MZN = 100 centavos). Never use `float` for money.
2. **Currency Code:** ISO 4217 numeric code `943` (MZN — Mozambican Metical) is enforced on all transactions and ISO 8583 `DE 49`.
3. **Double-Entry Immutability:** Never mutate `accounts.balance_centavos` directly without creating a `journal_entries` record and balanced `journal_postings` lines where `SUM(DEBIT) == SUM(CREDIT)`.
4. **Optimistic Locking:** Every balance update on `accounts` must check and increment `accounts.version` (`WHERE id = :id AND version = :expected_version`), retrying or failing cleanly on conflict.
5. **Suspense Clearing:** On `ROUTED`, funds move from Originator Account -> `METIX_SWITCH` Suspense Account. On `0210` (`DE 39 = 00`), funds move from Suspense -> Beneficiary Account. On `0420` reversal or `DECLINED`, funds move from Suspense -> Originator Account.

---

## 3. ISO 8583 Message & State Machine Invariants
1. **Canonical MTIs:**
   - `0200` Financial Request
   - `0210` Financial Response
   - `0420` Reversal Advice (requires `DE 90` Original Data Elements)
   - `0430` Reversal Response
2. **Required Data Elements:** `DE 2`, `DE 3`, `DE 4` (12-digit centavos), `DE 7` (`MMDDhhmmss`), `DE 11` (6-digit STAN), `DE 37` (12-char RRN), `DE 38` (Auth ID on approval), `DE 39` (Response Code), `DE 49` (`943`), `DE 90` (on `0420`/`0430`), `DE 102`, `DE 103`.
3. **Wire Trace Logging:** Every inbound and outbound ISO 8583 message must be persisted to `iso8583_message_log` with both structured `JSONB` (`payload_json`) and raw ASCII wire representation (`raw_iso_snippet`).
4. **Strict State Machine (`switch_transactions.status`):**
   - `INITIATED` -> `ROUTED` -> `COMPLETED`
   - `INITIATED` -> `DECLINED` (pre-route validation, limit, or balance failure)
   - `ROUTED` -> `DECLINED` (beneficiary returned non-`00`, e.g., `DE 39 = 91`)
   - `ROUTED` -> `TIMED_OUT` -> `REVERSAL_SENT` -> `REVERSED` | `RECON_HOLD`
   - Terminal states (`COMPLETED`, `DECLINED`, `REVERSED`, `RECON_HOLD`) must never transition backward.

---

## 4. Idempotency, Regulatory Limits & 25s SPIM SLA
1. **Redis Idempotency:** All transaction initiation requests must acquire an atomic Redis lock (`SET key value NX EX ttl`) keyed on `idempotency_key` (and `STAN`+`RRN`). Duplicate retries must return the original outcome without double-debiting.
2. **Notice No. 1/GBM/2026 Daily Limits:** Enforced atomically in Redis per originator account per UTC/CAT day:
   - `INDIVIDUAL`: `20,000,000` centavos (`200,000.00 MT`)
   - `BUSINESS`: `50,000,000` centavos (`500,000.00 MT`)
   - Exceeding the limit rejects the transaction with `DE 39 = 61`.
3. **25-Second SPIM SLA:** Every `ROUTED` transaction has `sla_deadline_at = created_at + 25s`. The background SLA sweeper must detect expired `ROUTED` transactions, mark them `TIMED_OUT` (`DE 39 = 68`), dispatch `0420` Reversal Advice, and post balanced reversal journal entries.

---

## 5. Verification Standard
- Never claim tests pass without running `uv run pytest` after the final edit.
- All 4 core integration scenarios (happy path, idempotency retry, >25s SLA timeout reversal, 200k MT individual daily limit) must pass against real PostgreSQL and Redis instances.
