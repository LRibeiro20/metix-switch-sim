# `metix-switch-sim` — Architectural & Financial Invariants

This specification defines the non-negotiable financial, protocol, concurrency, and regulatory invariants enforced across `metix-switch-sim`, modeled on Mozambique's **METIX / SIMOrede** instant payment switch architecture under **Banco de Moçambique Notice No. 1/GBM/2026**.

---

## 1. Platform & Runtime Stack
- **Backend (`backend/`):** Python 3.14+, FastAPI, async SQLAlchemy 2.x (`asyncpg`), Pydantic v2, PostgreSQL 16, Redis 7 (`redis.asyncio` with AOF `everysec` + RDB persistence).
- **Package Management & Static Analysis:** `uv` (`uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest`).
- **Frontend (`frontend/`):** Next.js 15 (TypeScript, App Router), React 19, Tailwind CSS v4, Meta Astryx (`@astryxdesign/core`), managed with `bun`.
- **Infrastructure:** `docker-compose.yml` orchestrating `postgres` (16), `redis` (7), `switch-api` (FastAPI), `sla-sweeper` (dedicated 25s SPIM SLA worker), and `frontend` (Next.js 15).

---

## 2. Financial & Double-Entry Ledger Invariants
1. **Minor Units Only:** All monetary amounts are stored as `BIGINT` in PostgreSQL and computed strictly as Python `int` in **centavos** (`1.00 MT = 100 centavos`). Floating-point arithmetic is prohibited for money.
2. **Currency Enforcement:** ISO 4217 numeric code `943` (MZN — Mozambican Metical) is enforced via PostgreSQL `CHECK` constraints (`currency = '943'`) and validated on every ISO 8583 `DE 49`.
3. **Double-Entry Immutability:** `accounts.balance_centavos` is never mutated directly. Every balance change must pass through `post_balanced_journal(...)`, writing one `journal_entries` header and balanced `journal_postings` lines where `SUM(DEBIT) == SUM(CREDIT)` inside a single ACID transaction.
4. **Deadlock-Free Optimistic Locking:** Every balance update runs inside a PostgreSQL savepoint (`BEGIN NESTED`), verifies solvency (`balance_centavos >= amount_centavos` on non-suspense accounts, rejecting shortfalls with `DE 39 = 51`), sorts account updates deterministically by `accounts.id` (`UUID`) to prevent opposite-direction circular deadlocks, and enforces compare-and-swap on `accounts.version` (`WHERE id = :id AND version = :expected_version`), retrying cleanly on conflict.
5. **Two-Leg Suspense Clearing (`METIX-SUSPENSE-943`):**
   - On `ROUTED`: Leg 1 (`CLEARING_HOLD`) moves funds from Originator Account -> `METIX_SWITCH` Suspense Account.
   - On `0210` (`DE 39 = 00`): Leg 2 (`SETTLEMENT`) moves funds from Suspense -> Beneficiary Account.
   - On `0210` decline (e.g., `DE 39 = 91`) or `0420`/`0430` timeout reversal (`DE 39 = 68`): `REVERSAL` moves funds from Suspense -> Originator Account.
   - On unacknowledged `0420` (`0430` dropped or non-`00`): transaction transitions to `RECON_HOLD` and funds remain ring-fenced in Suspense pending manual reconciliation.

---

## 3. ISO 8583 Wire Protocol & Monotonic State Machine
1. **Canonical MTIs:**
   - `0200` — Financial Request
   - `0210` — Financial Response
   - `0420` — Reversal Advice (requires 42-char `DE 90` Original Data Elements)
   - `0430` — Reversal Response
2. **Required Data Elements:** `DE 2`, `DE 3`, `DE 4` (12-digit centavos), `DE 7` (`MMDDhhmmss`), `DE 11` (6-digit STAN), `DE 37` (12-char RRN), `DE 38` (6-char Auth ID on approval), `DE 39` (Response Code), `DE 49` (`943`), `DE 90` (on `0420`/`0430`), `DE 102`, `DE 103`.
3. **Dual Wire Audit Logging:** Every inbound and outbound ISO 8583 message is persisted to `iso8583_message_log` with both structured `JSONB` (`payload_json`) and packed ASCII wire framing (`raw_iso_snippet`, including 128-bit primary+secondary hex bitmap).
4. **Strict State Machine (`switch_transactions.status`):**
   - `INITIATED` -> `ROUTED` -> `COMPLETED`
   - `INITIATED` -> `DECLINED` (pre-route validation, daily cap `DE 39 = 61`, or insufficient funds `DE 39 = 51`)
   - `ROUTED` -> `DECLINED` (beneficiary returned non-`00`, e.g., `DE 39 = 91`)
   - `ROUTED` -> `TIMED_OUT` -> `REVERSAL_SENT` -> `REVERSED` | `RECON_HOLD`
   - Terminal states (`COMPLETED`, `DECLINED`, `REVERSED`, `RECON_HOLD`) are strictly absorbing and never transition backward (`InvalidStateTransitionError`).

---

## 4. Idempotency, Regulatory Limits & 25-Second SPIM SLA
1. **Atomic Redis Idempotency:** Transaction initiation acquires `SET key value NX EX ttl` keyed on `idempotency_key` (plus a secondary `STAN + RRN` lock). Concurrent duplicate storms and sequential retries return the canonical transaction without double-debiting.
2. **Notice No. 1/GBM/2026 Daily Limits (`Africa/Maputo`, CAT):** Enforced atomically via Redis Lua scripts per originator account per CAT calendar day:
   - `INDIVIDUAL`: `20,000,000` centavos (`200,000.00 MT`)
   - `BUSINESS`: `50,000,000` centavos (`500,000.00 MT`)
   - Exceeding the daily cap rejects the transfer with `DE 39 = 61` without writing journal postings. Declined (`DE 39 = 51`, `91`) and reversed (`DE 39 = 68`) transfers release reserved daily quota via a clamped atomic Lua decrement.
3. **25-Second SPIM SLA Recovery:** Every `ROUTED` transaction persists `sla_deadline_at = created_at + 25s`. The `sla-sweeper` worker claims expired rows via `SELECT ... FOR UPDATE SKIP LOCKED` in Phase A (releasing database locks before network I/O), dispatches `0420` Reversal Advice in Phase B, and posts balanced reversal journals on `0430` (`DE 39 = 00`) or escalates to `RECON_HOLD`. Late `0210` arrivals lock the row (`FOR UPDATE`) and refuse settlement if the status has moved past `ROUTED`.

---

## 5. Verification Contract
- All backend changes must pass `uv --directory backend run ruff check .`, `uv --directory backend run ruff format --check .`, and the full `uv --directory backend run pytest -v` suite against live PostgreSQL 16 and Redis 7 instances.
- Core integration coverage validates:
  1. Happy-path `0200 -> 0210` two-leg clearing and balanced double-entry settlement.
  2. Concurrent (`5×`) and sequential duplicate retry idempotency with single ledger execution.
  3. Dropped `0210` / `>25s` SPIM SLA expiry triggering automated `0420 -> 0430` reversal and full balance restoration.
  4. Notice No. 1/GBM/2026 `200,000.00 MT` individual daily cap enforcement (`DE 39 = 61`).
  5. Beneficiary inoperative (`DE 39 = 91`) decline with clearing-hold reversal.
  6. Concurrent optimistic-locking solvency contention (`5×` distinct transfers exceeding available balance) and opposite-direction (`A <-> B`) deadlock-free execution.
