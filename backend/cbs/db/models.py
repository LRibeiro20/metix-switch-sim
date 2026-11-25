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
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class CbsBase(DeclarativeBase):
    pass


class CbsAccountTier(StrEnum):
    INDIVIDUAL = "INDIVIDUAL"
    BUSINESS = "BUSINESS"
    CLEARING_GL = "CLEARING_GL"


class CbsHoldStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CAPTURED = "CAPTURED"
    RELEASED = "RELEASED"


class CbsTxType(StrEnum):
    OUTBOUND_DEBIT = "OUTBOUND_DEBIT"
    INBOUND_CREDIT = "INBOUND_CREDIT"
    REVERSAL = "REVERSAL"
    RECON_ADJUSTMENT = "RECON_ADJUSTMENT"


class CbsTxStatus(StrEnum):
    HELD = "HELD"
    POSTED = "POSTED"
    REVERSED = "REVERSED"
    DECLINED = "DECLINED"


class CbsAccount(CbsBase):
    __tablename__ = "cbs_accounts"
    __table_args__ = (
        CheckConstraint("currency = '943'", name="ck_cbs_accounts_currency_mzn"),
        CheckConstraint("version >= 1", name="ck_cbs_accounts_version_positive"),
        CheckConstraint(
            "account_tier = 'CLEARING_GL' OR available_balance_centavos >= 0",
            name="ck_cbs_accounts_avail_nonneg",
        ),
        CheckConstraint(
            "account_tier = 'CLEARING_GL' OR ledger_balance_centavos >= 0",
            name="ck_cbs_accounts_ledger_nonneg",
        ),
        CheckConstraint(
            "account_tier = 'CLEARING_GL' OR available_balance_centavos <= ledger_balance_centavos",
            name="ck_cbs_accounts_avail_le_ledger",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    holder_name_enc: Mapped[str] = mapped_column(String(512), nullable=False)
    holder_name_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    holder_name_masked: Mapped[str] = mapped_column(String(128), nullable=False)
    account_tier: Mapped[str] = mapped_column(String(24), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="943")
    ledger_balance_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    available_balance_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    per_tx_limit_centavos: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=20_000_000
    )
    daily_limit_centavos: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=20_000_000
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CbsHold(CbsBase):
    __tablename__ = "cbs_holds"
    __table_args__ = (
        CheckConstraint("amount_centavos > 0", name="ck_cbs_holds_amount_positive"),
        Index("ix_cbs_holds_account_status", "account_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hold_ref: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    switch_tx_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    stan: Mapped[str] = mapped_column(String(6), nullable=False)
    rrn: Mapped[str] = mapped_column(String(12), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cbs_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    amount_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CbsHoldStatus.ACTIVE.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CbsTransaction(CbsBase):
    __tablename__ = "cbs_transactions"
    __table_args__ = (
        CheckConstraint("amount_centavos > 0", name="ck_cbs_tx_amount_positive"),
        CheckConstraint("currency = '943'", name="ck_cbs_tx_currency_mzn"),
        Index("ix_cbs_tx_stan_rrn", "stan", "rrn"),
        Index("ix_cbs_tx_switch_tx", "switch_tx_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cbs_ref: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    switch_tx_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stan: Mapped[str] = mapped_column(String(6), nullable=False)
    rrn: Mapped[str] = mapped_column(String(12), nullable=False)
    tx_type: Mapped[str] = mapped_column(String(24), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cbs_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    counterparty_bank: Mapped[str] = mapped_column(String(32), nullable=False)
    counterparty_account: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="943")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    auth_id: Mapped[str | None] = mapped_column(String(6), nullable=True)
    response_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    account: Mapped[CbsAccount] = relationship()


class CbsJournalEntry(CbsBase):
    __tablename__ = "cbs_journal_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cbs_transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cbs_transactions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    entry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    postings: Mapped[list[CbsJournalPosting]] = relationship(back_populates="entry")


class CbsJournalPosting(CbsBase):
    __tablename__ = "cbs_journal_postings"
    __table_args__ = (
        CheckConstraint("amount_centavos > 0", name="ck_cbs_postings_amount_positive"),
        CheckConstraint("direction IN ('DEBIT', 'CREDIT')", name="ck_cbs_postings_direction"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    journal_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cbs_journal_entries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cbs_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(6), nullable=False)
    amount_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    entry: Mapped[CbsJournalEntry] = relationship(back_populates="postings")
