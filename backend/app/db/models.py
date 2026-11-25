from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class InstitutionRole(StrEnum):
    PARTICIPANT = "PARTICIPANT"
    ORIGINATOR = "ORIGINATOR"
    BENEFICIARY = "BENEFICIARY"
    SWITCH = "SWITCH"


class AccountTier(StrEnum):
    INDIVIDUAL = "INDIVIDUAL"
    BUSINESS = "BUSINESS"
    SUSPENSE = "SUSPENSE"
    SETTLEMENT = "SETTLEMENT"


class TransactionStatus(StrEnum):
    INITIATED = "INITIATED"
    ROUTED = "ROUTED"
    COMPLETED = "COMPLETED"
    DECLINED = "DECLINED"
    TIMED_OUT = "TIMED_OUT"
    REVERSAL_SENT = "REVERSAL_SENT"
    REVERSED = "REVERSED"
    RECON_HOLD = "RECON_HOLD"


class JournalEntryType(StrEnum):
    CLEARING_HOLD = "CLEARING_HOLD"
    SETTLEMENT = "SETTLEMENT"
    REVERSAL = "REVERSAL"
    RECON_ADJUSTMENT = "RECON_ADJUSTMENT"
    LIQUIDITY_TOPUP = "LIQUIDITY_TOPUP"


class PostingDirection(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class MessageDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


TERMINAL_STATUSES: frozenset[TransactionStatus] = frozenset(
    {
        TransactionStatus.COMPLETED,
        TransactionStatus.DECLINED,
        TransactionStatus.REVERSED,
        TransactionStatus.RECON_HOLD,
    }
)

ALLOWED_TRANSITIONS: dict[TransactionStatus, frozenset[TransactionStatus]] = {
    TransactionStatus.INITIATED: frozenset({TransactionStatus.ROUTED, TransactionStatus.DECLINED}),
    TransactionStatus.ROUTED: frozenset(
        {
            TransactionStatus.COMPLETED,
            TransactionStatus.DECLINED,
            TransactionStatus.TIMED_OUT,
        }
    ),
    TransactionStatus.TIMED_OUT: frozenset({TransactionStatus.REVERSAL_SENT}),
    TransactionStatus.REVERSAL_SENT: frozenset(
        {TransactionStatus.REVERSED, TransactionStatus.RECON_HOLD}
    ),
    TransactionStatus.COMPLETED: frozenset(),
    TransactionStatus.DECLINED: frozenset(),
    TransactionStatus.REVERSED: frozenset(),
    TransactionStatus.RECON_HOLD: frozenset(),
}


class InvalidStateTransitionError(Exception):
    """Raised when a switch transaction attempts an illegal state machine transition."""


def validate_transition(current: TransactionStatus, target: TransactionStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransitionError(
            f"Illegal switch transaction transition: {current.value} -> {target.value}"
        )


class Institution(Base):
    __tablename__ = "institutions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    bic_code: Mapped[str] = mapped_column(String(16), nullable=False, default="METXMZMA")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    accounts: Mapped[list[Account]] = relationship(back_populates="institution")


class Account(Base):
    """Switch-owned clearing suspense & participant settlement accounts (no customer accounts)."""

    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("currency = '943'", name="ck_accounts_currency_mzn"),
        CheckConstraint("version >= 1", name="ck_accounts_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("institutions.id", ondelete="RESTRICT"), nullable=False
    )
    account_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    holder_name: Mapped[str] = mapped_column(String(128), nullable=False)
    account_tier: Mapped[str] = mapped_column(String(24), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="943")
    balance_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    institution: Mapped[Institution] = relationship(back_populates="accounts")


class SettlementCycle(Base):
    __tablename__ = "settlement_cycles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="OPEN")
    total_gross_volume_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_net_settled_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    initiated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    report_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    positions: Mapped[list[SettlementPosition]] = relationship(
        back_populates="cycle", order_by="SettlementPosition.institution_code"
    )


class SettlementPosition(Base):
    __tablename__ = "settlement_positions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("settlement_cycles.id", ondelete="RESTRICT"), nullable=False
    )
    institution_code: Mapped[str] = mapped_column(String(32), nullable=False)
    gross_debits_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gross_credits_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    net_position_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opening_balance_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    closing_balance_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    available_liquidity_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    liquidity_utilization_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False)

    cycle: Mapped[SettlementCycle] = relationship(back_populates="positions")


class ParticipantLiquidityProfile(Base):
    __tablename__ = "participant_liquidity_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    institution_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    settlement_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    prefunded_limit_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    minimum_reserve_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    collateral_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_debit_cap_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    simulate_shortfall: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SwitchTransaction(Base):
    __tablename__ = "switch_transactions"
    __table_args__ = (
        CheckConstraint("amount_centavos > 0", name="ck_switch_tx_amount_positive"),
        CheckConstraint("currency = '943'", name="ck_switch_tx_currency_mzn"),
        Index("ix_switch_tx_status_sla", "status", "sla_deadline_at"),
        Index("ix_switch_tx_stan_rrn", "stan", "rrn"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    stan: Mapped[str] = mapped_column(String(6), nullable=False)
    rrn: Mapped[str] = mapped_column(String(12), nullable=False)
    transmission_datetime: Mapped[str] = mapped_column(String(10), nullable=False)
    originator_code: Mapped[str] = mapped_column(String(32), nullable=False)
    beneficiary_code: Mapped[str] = mapped_column(String(32), nullable=False)
    originator_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    beneficiary_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    originator_account_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    beneficiary_account_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    originator_hold_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="943")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=TransactionStatus.INITIATED.value
    )
    response_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    auth_id: Mapped[str | None] = mapped_column(String(6), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    fraud_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fraud_status: Mapped[str] = mapped_column(String(24), nullable=False, default="CLEAR")
    processing_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    settlement_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("settlement_cycles.id", ondelete="SET NULL"), nullable=True
    )
    sla_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    journal_entries: Mapped[list[JournalEntry]] = relationship(
        back_populates="transaction", order_by="JournalEntry.created_at"
    )
    iso_messages: Mapped[list[Iso8583MessageLog]] = relationship(
        back_populates="transaction", order_by="Iso8583MessageLog.created_at"
    )


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("switch_transactions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    entry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    transaction: Mapped[SwitchTransaction | None] = relationship(back_populates="journal_entries")
    postings: Mapped[list[JournalPosting]] = relationship(
        back_populates="journal_entry", order_by="JournalPosting.direction"
    )


class JournalPosting(Base):
    __tablename__ = "journal_postings"
    __table_args__ = (
        CheckConstraint("amount_centavos > 0", name="ck_journal_postings_amount_positive"),
        CheckConstraint("direction IN ('DEBIT', 'CREDIT')", name="ck_journal_postings_direction"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    journal_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    direction: Mapped[str] = mapped_column(String(6), nullable=False)
    amount_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    journal_entry: Mapped[JournalEntry] = relationship(back_populates="postings")
    account: Mapped[Account] = relationship()


class Iso8583MessageLog(Base):
    __tablename__ = "iso8583_message_log"
    __table_args__ = (Index("ix_iso_msg_tx_created", "transaction_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("switch_transactions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    mti: Mapped[str] = mapped_column(String(4), nullable=False)
    direction: Mapped[str] = mapped_column(String(12), nullable=False)
    stan: Mapped[str] = mapped_column(String(6), nullable=False)
    rrn: Mapped[str] = mapped_column(String(12), nullable=False)
    response_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_iso_snippet: Mapped[str] = mapped_column(Text, nullable=False)
    signature_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    transaction: Mapped[SwitchTransaction] = relationship(back_populates="iso_messages")


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    triggered_by: Mapped[str] = mapped_column(String(64), nullable=False)
    switch_tx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bci_tx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stdb_tx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exception_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suspense_balance_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="COMPLETED")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReconciliationException(Base):
    __tablename__ = "reconciliation_exceptions"
    __table_args__ = (Index("ix_recon_exc_status", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    exception_type: Mapped[str] = mapped_column(String(36), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="HIGH")
    bank_code: Mapped[str] = mapped_column(String(32), nullable=False)
    switch_tx_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    cbs_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stan: Mapped[str | None] = mapped_column(String(6), nullable=True)
    rrn: Mapped[str | None] = mapped_column(String(12), nullable=True)
    amount_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="OPEN")
    resolution_notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MakerCheckerRequest(Base):
    __tablename__ = "maker_checker_requests"
    __table_args__ = (
        CheckConstraint(
            "checker_id IS NULL OR checker_id <> maker_id",
            name="ck_maker_checker_four_eyes",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    maker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    maker_role: Mapped[str] = mapped_column(String(32), nullable=False)
    checker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checker_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FraudCase(Base):
    __tablename__ = "fraud_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("switch_transactions.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    originator_code: Mapped[str] = mapped_column(String(32), nullable=False)
    originator_account_number: Mapped[str] = mapped_column(String(32), nullable=False)
    beneficiary_code: Mapped[str] = mapped_column(String(32), nullable=False)
    beneficiary_account_number: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False)
    triggered_rules: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING_REVIEW")
    assigned_to: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    events: Mapped[list[FraudInvestigationEvent]] = relationship(
        back_populates="fraud_case", order_by="FraudInvestigationEvent.created_at"
    )


class FraudInvestigationEvent(Base):
    __tablename__ = "fraud_investigation_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fraud_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fraud_cases.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    notes: Mapped[str] = mapped_column(String(512), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    fraud_case: Mapped[FraudCase] = relationship(back_populates="events")


class AuditLog(Base):
    """Append-only SHA-256 hash-chained audit trail."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sequence_no: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SystemAlert(Base):
    __tablename__ = "system_alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    source_service: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
