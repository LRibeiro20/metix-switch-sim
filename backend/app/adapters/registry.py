from __future__ import annotations

import asyncio

from app.adapters.base import AdapterConnectivity, BankIntegrationAdapter
from shared.schemas import CbsAccountOut

# Mozambican NIB/account routing prefixes -> participant institution code
ACCOUNT_ROUTING_PREFIXES: list[tuple[str, str]] = [
    ("0001", "BCI_MZ"),
    ("0003", "STDB_MZ"),
    ("25884", "MPESA_MZ"),
    ("25885", "MPESA_MZ"),
    ("25886", "EMOLA_MZ"),
    ("25887", "EMOLA_MZ"),
]


class AdapterRegistry:
    """Central routing registry mapping institution codes and account numbers to bank adapters."""

    def __init__(self, adapters: dict[str, BankIntegrationAdapter]) -> None:
        self._adapters = dict(adapters)

    @property
    def bank_codes(self) -> list[str]:
        return list(self._adapters.keys())

    def get(self, bank_code: str) -> BankIntegrationAdapter:
        if bank_code not in self._adapters:
            raise ValueError(f"No integration adapter registered for institution: {bank_code}")
        return self._adapters[bank_code]

    def resolve_bank_code_for_account(self, account_number: str) -> str:
        for prefix, code in ACCOUNT_ROUTING_PREFIXES:
            if account_number.startswith(prefix) and code in self._adapters:
                return code
        raise ValueError(f"Unable to resolve participant institution for account: {account_number}")

    async def lookup_account(self, account_number: str) -> tuple[str, CbsAccountOut]:
        bank_code = self.resolve_bank_code_for_account(account_number)
        adapter = self.get(bank_code)
        acct = await adapter.get_balance(account_number)
        if acct is None:
            raise ValueError(f"Unknown account {account_number} at institution {bank_code}")
        return bank_code, acct

    async def list_all_customer_accounts(self) -> list[CbsAccountOut]:
        results = await asyncio.gather(
            *(adapter.list_accounts() for adapter in self._adapters.values()),
            return_exceptions=True,
        )
        combined: list[CbsAccountOut] = []
        for res in results:
            if isinstance(res, list):
                combined.extend(res)
        return combined

    async def check_all_connectivity(self) -> list[AdapterConnectivity]:
        return list(
            await asyncio.gather(
                *(adapter.check_connectivity() for adapter in self._adapters.values())
            )
        )
