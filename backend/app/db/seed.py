from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Account,
    AccountTier,
    Institution,
    InstitutionRole,
    ParticipantLiquidityProfile,
)

METIX_SUSPENSE_ACCOUNT_NUMBER = "METIX-SUSPENSE-943"
BM_RTGS_TREASURY_ACCOUNT_NUMBER = "BM-RTGS-TREASURY-943"

SETTLEMENT_ACCOUNT_BY_INSTITUTION: dict[str, str] = {
    "BCI_MZ": "SETTLE-BCI-943",
    "STDB_MZ": "SETTLE-STDB-943",
    "MPESA_MZ": "SETTLE-MPESA-943",
    "EMOLA_MZ": "SETTLE-EMOLA-943",
}

SEED_INSTITUTIONS = [
    {
        "code": "METIX_SWITCH",
        "name": "METIX / SIMOrede Central Switch (Banco de Moçambique)",
        "role": InstitutionRole.SWITCH.value,
        "bic_code": "SIMOMZMA",
    },
    {
        "code": "BCI_MZ",
        "name": "Banco Comercial e de Investimentos",
        "role": InstitutionRole.PARTICIPANT.value,
        "bic_code": "CGDIMZMA",
    },
    {
        "code": "STDB_MZ",
        "name": "Standard Bank Moçambique",
        "role": InstitutionRole.PARTICIPANT.value,
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

# Switch DB holds clearing suspense, central bank treasury, & participant settlement accounts.
# Customer accounts live exclusively inside bci_cbs, stdb_cbs, and wallet-sim.
SEED_ACCOUNTS = [
    {
        "institution_code": "METIX_SWITCH",
        "account_number": METIX_SUSPENSE_ACCOUNT_NUMBER,
        "holder_name": "METIX Clearing Suspense (MZN)",
        "account_tier": AccountTier.SUSPENSE.value,
        "balance_centavos": 0,
    },
    {
        "institution_code": "METIX_SWITCH",
        "account_number": BM_RTGS_TREASURY_ACCOUNT_NUMBER,
        "holder_name": "Banco de Moçambique RTGS Liquidity Pool",
        "account_tier": AccountTier.SUSPENSE.value,
        "balance_centavos": 0,
    },
    {
        "institution_code": "BCI_MZ",
        "account_number": "SETTLE-BCI-943",
        "holder_name": "BCI Participant Settlement Account (SPIM)",
        "account_tier": AccountTier.SETTLEMENT.value,
        "balance_centavos": 500_000_000,  # 5,000,000.00 MT prefunded liquidity
    },
    {
        "institution_code": "STDB_MZ",
        "account_number": "SETTLE-STDB-943",
        "holder_name": "Standard Bank Participant Settlement Account (SPIM)",
        "account_tier": AccountTier.SETTLEMENT.value,
        "balance_centavos": 500_000_000,  # 5,000,000.00 MT prefunded liquidity
    },
    {
        "institution_code": "MPESA_MZ",
        "account_number": "SETTLE-MPESA-943",
        "holder_name": "Vodacom M-Pesa Clearing Settlement Account",
        "account_tier": AccountTier.SETTLEMENT.value,
        "balance_centavos": 100_000_000,  # 1,000,000.00 MT
    },
    {
        "institution_code": "EMOLA_MZ",
        "account_number": "SETTLE-EMOLA-943",
        "holder_name": "Movitel e-Mola Clearing Settlement Account",
        "account_tier": AccountTier.SETTLEMENT.value,
        "balance_centavos": 100_000_000,  # 1,000,000.00 MT
    },
]

SEED_LIQUIDITY_PROFILES = [
    {
        "institution_code": "BCI_MZ",
        "prefunded_limit_centavos": 500_000_000,
        "minimum_reserve_centavos": 50_000_000,  # 500,000.00 MT floor
        "collateral_centavos": 250_000_000,
        "net_debit_cap_centavos": 300_000_000,
    },
    {
        "institution_code": "STDB_MZ",
        "prefunded_limit_centavos": 500_000_000,
        "minimum_reserve_centavos": 50_000_000,
        "collateral_centavos": 250_000_000,
        "net_debit_cap_centavos": 300_000_000,
    },
    {
        "institution_code": "MPESA_MZ",
        "prefunded_limit_centavos": 100_000_000,
        "minimum_reserve_centavos": 10_000_000,
        "collateral_centavos": 50_000_000,
        "net_debit_cap_centavos": 75_000_000,
    },
    {
        "institution_code": "EMOLA_MZ",
        "prefunded_limit_centavos": 100_000_000,
        "minimum_reserve_centavos": 10_000_000,
        "collateral_centavos": 50_000_000,
        "net_debit_cap_centavos": 75_000_000,
    },
]


async def seed_database(session: AsyncSession) -> dict[str, int]:
    """Idempotently seed institutions, settlement/suspense accounts, and liquidity profiles."""
    inst_by_code: dict[str, Institution] = {}
    acct_by_number: dict[str, Account] = {}
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
            await session.flush()
            created_accounts += 1
        acct_by_number[acct.account_number] = acct

    for lp in SEED_LIQUIDITY_PROFILES:
        code = str(lp["institution_code"])
        existing_lp = (
            await session.execute(
                select(ParticipantLiquidityProfile).where(
                    ParticipantLiquidityProfile.institution_code == code
                )
            )
        ).scalar_one_or_none()
        if existing_lp is None:
            settle_acct = acct_by_number[SETTLEMENT_ACCOUNT_BY_INSTITUTION[code]]
            session.add(
                ParticipantLiquidityProfile(
                    institution_code=code,
                    settlement_account_id=settle_acct.id,
                    prefunded_limit_centavos=int(lp["prefunded_limit_centavos"]),
                    minimum_reserve_centavos=int(lp["minimum_reserve_centavos"]),
                    collateral_centavos=int(lp["collateral_centavos"]),
                    net_debit_cap_centavos=int(lp["net_debit_cap_centavos"]),
                    simulate_shortfall=False,
                )
            )

    await session.commit()
    return {
        "created_institutions": created_institutions,
        "created_accounts": created_accounts,
    }
