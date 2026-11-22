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
    transaction_id: uuid.UUID
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
    amount_centavos: int
    currency: str
    status: str
    response_code: str | None
    response_description: str | None
    auth_id: str | None
    failure_reason: str | None
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


class ChaosPresetRequest(BaseModel):
    preset: str | None = None
    latency_ms: int | None = Field(default=None, ge=0, le=60_000)
    drop_0210: bool | None = None
    force_de39: str | None = Field(default=None, pattern=r"^[A-Z0-9]{2}$")
    fail_0430: bool | None = None
    target_beneficiary: str | None = None
