from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TransferCreateRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=4, max_length=128)
    originator_account_number: str = Field(..., min_length=6, max_length=32)
    beneficiary_account_number: str = Field(..., min_length=6, max_length=32)
    amount_centavos: int = Field(..., gt=0, description="Transfer amount in centavos (int)")
    stan: str | None = Field(default=None, pattern=r"^\d{6}$")
    rrn: str | None = Field(default=None, min_length=12, max_length=12)


class IsoMessageOut(BaseModel):
    id: uuid.UUID
    mti: str
    direction: str
    stan: str
    rrn: str
    response_code: str | None
    payload_json: dict[str, Any]
    raw_iso_snippet: str
    signature_key_id: str | None = None
    signature_verified: bool = True
    created_at: datetime


class JournalPostingOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    account_number: str | None = None
    institution_code: str | None = None
    direction: str
    amount_centavos: int
    created_at: datetime


class JournalEntryOut(BaseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID | None
    entry_type: str
    description: str
    created_at: datetime
    postings: list[JournalPostingOut]


class TransactionOut(BaseModel):
    id: uuid.UUID
    idempotency_key: str
    stan: str
    rrn: str
    transmission_datetime: str
    originator_code: str
    beneficiary_code: str
    originator_account_id: uuid.UUID
    beneficiary_account_id: uuid.UUID
    originator_account_number: str
    beneficiary_account_number: str
    originator_hold_ref: str | None = None
    amount_centavos: int
    currency: str
    status: str
    response_code: str | None
    response_description: str | None
    auth_id: str | None
    failure_reason: str | None
    fraud_score: int = 0
    fraud_status: str = "CLEAR"
    processing_latency_ms: int | None = None
    signature_verified: bool = True
    settlement_cycle_id: uuid.UUID | None = None
    sla_deadline_at: datetime
    created_at: datetime
    updated_at: datetime
    iso_messages: list[IsoMessageOut] = []
    journal_entries: list[JournalEntryOut] = []


class TransferResponse(BaseModel):
    idempotent_replay: bool
    transaction: TransactionOut


class AccountOut(BaseModel):
    id: uuid.UUID
    institution_code: str
    institution_name: str
    institution_role: str
    bic_code: str
    account_number: str
    holder_name: str
    account_tier: str
    currency: str
    balance_centavos: int
    version: int
    daily_limit_centavos: int | None
    daily_used_centavos: int
    updated_at: datetime


class LedgerSummaryOut(BaseModel):
    total_debits_centavos: int
    total_credits_centavos: int
    is_balanced: bool
    suspense_balance_centavos: int
    entries: list[JournalEntryOut]


class OAuthTokenRequest(BaseModel):
    grant_type: str = Field(default="password", pattern=r"^(password|client_credentials)$")
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    client_secret: str | None = None


class OAuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    subject: str
    display_name: str
    role: str
    scopes: list[str]


class MakerCheckerDecisionRequest(BaseModel):
    approve: bool
    notes: str = Field(..., min_length=2, max_length=512)


class ReconAdjustProposeRequest(BaseModel):
    adjustment_mode: str = Field(
        default="RELEASE_SUSPENSE_TO_ORIGINATOR",
        pattern=r"^(RELEASE_SUSPENSE_TO_ORIGINATOR|FORCE_SETTLE_TO_BENEFICIARY|DISMISS_WITH_NOTES)$",
    )
    reason: str = Field(..., min_length=3, max_length=512)


class FraudDecisionRequest(BaseModel):
    decision: str = Field(..., pattern=r"^(RELEASE|CONFIRM_FRAUD)$")
    notes: str = Field(..., min_length=3, max_length=512)


class SettlementRunRequest(BaseModel):
    simulate_failure_institution: str | None = None


class ShortfallToggleRequest(BaseModel):
    institution_code: str
    enabled: bool


class LiquidityTopupRequest(BaseModel):
    institution_code: str
    amount_centavos: int = Field(..., gt=0)
    reason: str = Field(default="Intraday BM RTGS liquidity top-up", max_length=256)
    immediate: bool = False


class BankChaosUpdateRequest(BaseModel):
    preset: str | None = None
    latency_ms: int | None = Field(default=None, ge=0, le=60_000)
    drop_0210: bool | None = None
    force_de39: str | None = Field(default=None, pattern=r"^[A-Z0-9]{2}$")
    fail_0430: bool | None = None
    offline: bool | None = None


class BankRestrictionProposalRequest(BaseModel):
    status: str | None = Field(default=None, pattern=r"^(ACTIVE|PND|PNC|FROZEN|CLOSED)$")
    per_tx_limit_centavos: int | None = Field(default=None, gt=0)
    daily_limit_centavos: int | None = Field(default=None, gt=0)
    reason: str = Field(..., min_length=3, max_length=256)
    immediate: bool = False


class ReconFaultProxyRequest(BaseModel):
    bank_code: str = Field(..., pattern=r"^(BCI_MZ|STDB_MZ)$")
    fault_type: str = Field(
        ...,
        pattern=r"^(ORPHAN_CBS_POSTING|DUPLICATE_CBS_POSTING|STALE_ACTIVE_HOLD)$",
    )
    account_number: str
    amount_centavos: int = Field(default=150_000, gt=0)
    switch_tx_id: uuid.UUID | None = None
    stan: str | None = None
    rrn: str | None = None
