from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
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
    ORIGINATOR = "ORIGINATOR"
    BENEFICIARY = "BENEFICIARY"
    SWITCH = "SWITCH"


class AccountTier(StrEnum):
    INDIVIDUAL = "INDIVIDUAL"
    BUSINESS = "BUSINESS"
    SUSPENSE = "SUSPENSE"


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
    amount_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="943")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=TransactionStatus.INITIATED.value
    )
    response_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    auth_id: Mapped[str | None] = mapped_column(String(6), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
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
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("switch_transactions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    entry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    transaction: Mapped[SwitchTransaction] = relationship(back_populates="journal_entries")
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    transaction: Mapped[SwitchTransaction] = relationship(back_populates="iso_messages")
