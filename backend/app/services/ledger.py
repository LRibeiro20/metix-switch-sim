from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Account,
    AccountTier,
    JournalEntry,
    JournalEntryType,
    JournalPosting,
    PostingDirection,
)


class LedgerError(Exception):
    """Base exception for double-entry ledger invariant violations."""


class InsufficientFundsError(LedgerError):
    """Raised when a non-suspense debit account lacks sufficient centavos."""


class OptimisticLockError(LedgerError):
    """Raised when concurrent balance mutations exhaust optimistic lock retries."""


async def post_balanced_journal(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID | None,
    entry_type: JournalEntryType,
    description: str,
    debit_account_id: uuid.UUID,
    credit_account_id: uuid.UUID,
    amount_centavos: int,
    max_retries: int = 8,
) -> JournalEntry:
    """Atomically mutate two account balances via a balanced double-entry journal posting.

    Invariants enforced:
    - Minor units (int centavos) strictly > 0.
    - Optimistic locking via `accounts.version` checked and incremented on every balance write.
    - Exactly balanced DEBIT and CREDIT postings where SUM(DEBIT) == SUM(CREDIT).
    - Deterministic UUID lock ordering prevents circular deadlocks.
    """
    if not isinstance(amount_centavos, int) or amount_centavos <= 0:
        raise LedgerError("amount_centavos must be a strictly positive integer")
    if debit_account_id == credit_account_id:
        raise LedgerError("Debit and credit accounts must be distinct")

    for _attempt in range(max_retries):
        async with session.begin_nested() as nested:
            result = await session.execute(
                select(Account)
                .where(Account.id.in_([debit_account_id, credit_account_id]))
                .execution_options(populate_existing=True)
            )
            accounts_by_id = {acct.id: acct for acct in result.scalars().all()}
            if debit_account_id not in accounts_by_id or credit_account_id not in accounts_by_id:
                raise LedgerError("One or both ledger accounts do not exist")

            debit_acct = accounts_by_id[debit_account_id]
            credit_acct = accounts_by_id[credit_account_id]

            if (
                debit_acct.account_tier != AccountTier.SUSPENSE.value
                and debit_acct.balance_centavos < amount_centavos
            ):
                raise InsufficientFundsError(
                    f"Account {debit_acct.account_number} has {debit_acct.balance_centavos} "
                    f"centavos, required {amount_centavos}"
                )

            updates: list[tuple[uuid.UUID, int, int]] = sorted(
                [
                    (debit_acct.id, debit_acct.version, -amount_centavos),
                    (credit_acct.id, credit_acct.version, +amount_centavos),
                ],
                key=lambda item: item[0],
            )

            conflict = False
            for acct_id, expected_version, delta in updates:
                stmt = (
                    update(Account)
                    .where(Account.id == acct_id, Account.version == expected_version)
                    .values(
                        balance_centavos=Account.balance_centavos + delta,
                        version=Account.version + 1,
                        updated_at=func.now(),
                    )
                )
                res = await session.execute(stmt)
                if res.rowcount != 1:
                    conflict = True
                    break

            if conflict:
                await nested.rollback()
                continue

            entry = JournalEntry(
                transaction_id=transaction_id,
                entry_type=entry_type.value,
                description=description,
            )
            session.add(entry)
            await session.flush()

            debit_posting = JournalPosting(
                journal_entry_id=entry.id,
                account_id=debit_account_id,
                direction=PostingDirection.DEBIT.value,
                amount_centavos=amount_centavos,
            )
            credit_posting = JournalPosting(
                journal_entry_id=entry.id,
                account_id=credit_account_id,
                direction=PostingDirection.CREDIT.value,
                amount_centavos=amount_centavos,
            )
            session.add_all([debit_posting, credit_posting])
            await session.flush()

            session.expire(debit_acct)
            session.expire(credit_acct)
            return entry

    raise OptimisticLockError(
        f"Failed to post journal entry for tx {transaction_id} after {max_retries} optimistic lock "
        "retries"
    )
