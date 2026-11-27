from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CbsAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PND = "PND"  # Post No Debit
    PNC = "PNC"  # Post No Credit
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"


class CbsAccountOut(BaseModel):
    id: uuid.UUID
    bank_code: str
    bank_name: str
    bic_code: str
    account_number: str
    holder_name: str
    holder_name_masked: str
    account_tier: str
    currency: str
    ledger_balance_centavos: int
    available_balance_centavos: int
    earmarked_centavos: int
    status: str
    per_tx_limit_centavos: int
    daily_limit_centavos: int
    daily_used_centavos: int
    version: int
    pii_encrypted_at_rest: bool = True
    updated_at: datetime


class CbsDebitAuthorizeRequest(BaseModel):
    switch_tx_id: uuid.UUID
    idempotency_key: str
    stan: str
    rrn: str
    account_number: str
    counterparty_bank: str
    counterparty_account: str
    amount_centavos: int = Field(..., gt=0)


class CbsDebitAuthorizeResponse(BaseModel):
    approved: bool
    response_code: str
    hold_ref: str | None = None
    cbs_ref: str | None = None
    reason: str | None = None
    available_balance_centavos: int | None = None
    ledger_balance_centavos: int | None = None


class CbsDebitCaptureRequest(BaseModel):
    switch_tx_id: uuid.UUID
    hold_ref: str
    stan: str
    rrn: str


class CbsDebitReleaseRequest(BaseModel):
    switch_tx_id: uuid.UUID
    hold_ref: str
    stan: str
    rrn: str
    reason: str = "Transaction reversed or declined"


class CbsHoldActionResponse(BaseModel):
    ok: bool
    hold_ref: str
    status: str
    available_balance_centavos: int
    ledger_balance_centavos: int


class CbsInternalTransferRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=4, max_length=128)
    originator_account_number: str = Field(..., min_length=6, max_length=32)
    beneficiary_account_number: str = Field(..., min_length=6, max_length=32)
    amount_centavos: int = Field(..., gt=0)
    reference: str | None = Field(default=None, max_length=64)


class CbsInternalTransferResponse(BaseModel):
    idempotent_replay: bool = False
    approved: bool
    bank_code: str
    cbs_ref: str | None = None
    auth_id: str | None = None
    response_code: str
    reason: str | None = None
    originator_account_number: str
    beneficiary_account_number: str
    amount_centavos: int
    currency: str = "943"
    originator_available_balance_centavos: int | None = None
    originator_ledger_balance_centavos: int | None = None
    beneficiary_available_balance_centavos: int | None = None
    beneficiary_ledger_balance_centavos: int | None = None
    processing_latency_ms: int = 0


class CbsRestrictionUpdateRequest(BaseModel):
    status: CbsAccountStatus | None = None
    per_tx_limit_centavos: int | None = Field(default=None, gt=0)
    daily_limit_centavos: int | None = Field(default=None, gt=0)
    reason: str = Field(default="Operations update", max_length=256)


class CbsTransactionOut(BaseModel):
    id: uuid.UUID
    cbs_ref: str
    switch_tx_id: uuid.UUID | None
    idempotency_key: str
    stan: str
    rrn: str
    tx_type: str
    bank_code: str
    account_number: str
    counterparty_bank: str
    counterparty_account: str
    amount_centavos: int
    currency: str
    status: str
    auth_id: str | None
    response_code: str | None
    signature_verified: bool
    created_at: datetime


class BankChaosConfig(BaseModel):
    preset: str = Field(default="normal")
    latency_ms: int = Field(default=60, ge=0, le=60_000)
    drop_0210: bool = Field(default=False)
    commit_then_drop_0210: bool = Field(default=False)
    force_de39: str | None = Field(default=None, pattern=r"^[A-Z0-9]{2}$")
    fail_0430: bool = Field(default=False)
    offline: bool = Field(default=False)


class ReconFaultInjectRequest(BaseModel):
    fault_type: str = Field(
        ...,
        pattern=r"^(ORPHAN_CBS_POSTING|DUPLICATE_CBS_POSTING|STALE_ACTIVE_HOLD)$",
    )
    account_number: str
    amount_centavos: int = Field(default=150_000, gt=0)
    switch_tx_id: uuid.UUID | None = None
    stan: str | None = None
    rrn: str | None = None
