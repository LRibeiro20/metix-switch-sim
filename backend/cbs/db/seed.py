from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbs.db.models import CbsAccount, CbsAccountTier
from shared.security import FieldEncryptor

BANK_CLEARING_GL_ACCOUNT: dict[str, str] = {
    "BCI_MZ": "BCI-CLEARING-GL-943",
    "STDB_MZ": "STDB-CLEARING-GL-943",
}

CBS_SEED_ACCOUNTS: dict[str, list[dict[str, object]]] = {
    "BCI_MZ": [
        {
            "account_number": "BCI-CLEARING-GL-943",
            "holder_name": "BCI Interbank METIX Clearing GL",
            "account_tier": CbsAccountTier.CLEARING_GL.value,
            "balance_centavos": 0,
            "per_tx_limit_centavos": 10_000_000_000,
            "daily_limit_centavos": 100_000_000_000,
        },
        {
            "account_number": "00010012345678",
            "holder_name": "Amélia Machel (Individual)",
            "account_tier": CbsAccountTier.INDIVIDUAL.value,
            "balance_centavos": 50_000_000,  # 500,000.00 MT
            "per_tx_limit_centavos": 20_000_000,  # 200,000.00 MT
            "daily_limit_centavos": 20_000_000,
        },
        {
            "account_number": "00010087654321",
            "holder_name": "Maputo Logistics Lda (Business)",
            "account_tier": CbsAccountTier.BUSINESS.value,
            "balance_centavos": 150_000_000,  # 1,500,000.00 MT
            "per_tx_limit_centavos": 50_000_000,  # 500,000.00 MT
            "daily_limit_centavos": 50_000_000,
        },
        {
            "account_number": "00010055001122",
            "holder_name": "Fátima Guebuza (Individual)",
            "account_tier": CbsAccountTier.INDIVIDUAL.value,
            "balance_centavos": 30_000_000,  # 300,000.00 MT
            "per_tx_limit_centavos": 20_000_000,
            "daily_limit_centavos": 20_000_000,
        },
    ],
    "STDB_MZ": [
        {
            "account_number": "STDB-CLEARING-GL-943",
            "holder_name": "Standard Bank Interbank METIX Clearing GL",
            "account_tier": CbsAccountTier.CLEARING_GL.value,
            "balance_centavos": 0,
            "per_tx_limit_centavos": 10_000_000_000,
            "daily_limit_centavos": 100_000_000_000,
        },
        {
            "account_number": "00030055443322",
            "holder_name": "Carlos Mondlane (Individual)",
            "account_tier": CbsAccountTier.INDIVIDUAL.value,
            "balance_centavos": 40_000_000,  # 400,000.00 MT
            "per_tx_limit_centavos": 20_000_000,
            "daily_limit_centavos": 20_000_000,
        },
        {
            "account_number": "00030099887766",
            "holder_name": "Beira Port Trading SA (Business)",
            "account_tier": CbsAccountTier.BUSINESS.value,
            "balance_centavos": 200_000_000,  # 2,000,000.00 MT
            "per_tx_limit_centavos": 50_000_000,
            "daily_limit_centavos": 50_000_000,
        },
        {
            "account_number": "00030077112233",
            "holder_name": "Eduardo Chissano (Individual)",
            "account_tier": CbsAccountTier.INDIVIDUAL.value,
            "balance_centavos": 25_000_000,  # 250,000.00 MT
            "per_tx_limit_centavos": 20_000_000,
            "daily_limit_centavos": 20_000_000,
        },
    ],
}


async def seed_cbs_database(
    session: AsyncSession,
    *,
    bank_code: str,
    encryptor: FieldEncryptor,
) -> int:
    """Idempotently seed bank-specific customer accounts and clearing GL."""
    created = 0
    accounts = CBS_SEED_ACCOUNTS.get(bank_code, [])
    for item in accounts:
        acct_no = str(item["account_number"])
        existing = (
            await session.execute(select(CbsAccount).where(CbsAccount.account_number == acct_no))
        ).scalar_one_or_none()
        if existing is not None:
            continue
        holder_name = str(item["holder_name"])
        bal = int(item["balance_centavos"])
        acct = CbsAccount(
            account_number=acct_no,
            holder_name_enc=encryptor.encrypt(holder_name),
            holder_name_hash=encryptor.blind_index(holder_name),
            holder_name_masked=encryptor.mask_holder_name(holder_name),
            account_tier=str(item["account_tier"]),
            currency="943",
            ledger_balance_centavos=bal,
            available_balance_centavos=bal,
            status="ACTIVE",
            per_tx_limit_centavos=int(item["per_tx_limit_centavos"]),
            daily_limit_centavos=int(item["daily_limit_centavos"]),
            version=1,
        )
        session.add(acct)
        created += 1
    await session.commit()
    return created
