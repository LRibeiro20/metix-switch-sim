from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, AccountTier, Institution, InstitutionRole

METIX_SUSPENSE_ACCOUNT_NUMBER = "METIX-SUSPENSE-943"

SEED_INSTITUTIONS = [
    {
        "code": "METIX_SWITCH",
        "name": "METIX / SIMOrede Central Switch",
        "role": InstitutionRole.SWITCH.value,
        "bic_code": "SIMOMZMA",
    },
    {
        "code": "BCI_MZ",
        "name": "Banco Comercial e de Investimentos",
        "role": InstitutionRole.ORIGINATOR.value,
        "bic_code": "CGDIMZMA",
    },
    {
        "code": "STDB_MZ",
        "name": "Standard Bank Moçambique",
        "role": InstitutionRole.ORIGINATOR.value,
        "bic_code": "SBICMZMX",
    },
    {
        "code": "MPESA_MZ",
        "name": "Vodacom M-Pesa Moçambique",
        "role": InstitutionRole.BENEFICIARY.value,
        "bic_code": "MPESMZMA",
    },
    {
        "code": "EMOLA_MZ",
        "name": "Movitel e-Mola Moçambique",
        "role": InstitutionRole.BENEFICIARY.value,
        "bic_code": "EMOLMZMA",
    },
]

SEED_ACCOUNTS = [
    {
        "institution_code": "METIX_SWITCH",
        "account_number": METIX_SUSPENSE_ACCOUNT_NUMBER,
        "holder_name": "METIX Clearing Suspense (MZN)",
        "account_tier": AccountTier.SUSPENSE.value,
        "balance_centavos": 0,
    },
    {
        "institution_code": "BCI_MZ",
        "account_number": "00010012345678",
        "holder_name": "Amélia Machel (Individual)",
        "account_tier": AccountTier.INDIVIDUAL.value,
        "balance_centavos": 50_000_000,  # 500,000.00 MT
    },
    {
        "institution_code": "BCI_MZ",
        "account_number": "00010087654321",
        "holder_name": "Maputo Logistics Lda (Business)",
        "account_tier": AccountTier.BUSINESS.value,
        "balance_centavos": 150_000_000,  # 1,500,000.00 MT
    },
    {
        "institution_code": "STDB_MZ",
        "account_number": "00030055443322",
        "holder_name": "Carlos Mondlane (Individual)",
        "account_tier": AccountTier.INDIVIDUAL.value,
        "balance_centavos": 40_000_000,  # 400,000.00 MT
    },
    {
        "institution_code": "STDB_MZ",
        "account_number": "00030099887766",
        "holder_name": "Beira Port Trading SA (Business)",
        "account_tier": AccountTier.BUSINESS.value,
        "balance_centavos": 200_000_000,  # 2,000,000.00 MT
    },
    {
        "institution_code": "MPESA_MZ",
        "account_number": "258841234567",
        "holder_name": "M-Pesa Wallet — Rosa Sitoe",
        "account_tier": AccountTier.INDIVIDUAL.value,
        "balance_centavos": 5_000_000,  # 50,000.00 MT
    },
    {
        "institution_code": "EMOLA_MZ",
        "account_number": "258869876543",
        "holder_name": "e-Mola Wallet — João Mabunda",
        "account_tier": AccountTier.INDIVIDUAL.value,
        "balance_centavos": 3_500_000,  # 35,000.00 MT
    },
]


async def seed_database(session: AsyncSession) -> dict[str, int]:
    """Idempotently seed Mozambican institutions and accounts."""
    inst_by_code: dict[str, Institution] = {}
    created_institutions = 0
    created_accounts = 0

    for inst_data in SEED_INSTITUTIONS:
        result = await session.execute(
            select(Institution).where(Institution.code == inst_data["code"])
        )
        inst = result.scalar_one_or_none()
        if inst is None:
            inst = Institution(**inst_data)
            session.add(inst)
            await session.flush()
            created_institutions += 1
        inst_by_code[inst.code] = inst

    for acct_data in SEED_ACCOUNTS:
        result = await session.execute(
            select(Account).where(Account.account_number == acct_data["account_number"])
        )
        acct = result.scalar_one_or_none()
        if acct is None:
            inst = inst_by_code[acct_data["institution_code"]]
            acct = Account(
                institution_id=inst.id,
                account_number=acct_data["account_number"],
                holder_name=acct_data["holder_name"],
                account_tier=acct_data["account_tier"],
                currency="943",
                balance_centavos=acct_data["balance_centavos"],
                version=1,
            )
            session.add(acct)
            created_accounts += 1

    await session.commit()
    return {
        "created_institutions": created_institutions,
        "created_accounts": created_accounts,
    }
