from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.cbs_adapters import BciCoreBankingAdapter, StandardBankCoreBankingAdapter
from app.adapters.registry import AdapterRegistry
from app.adapters.wallet_adapter import MobileWalletAdapter
from app.config import Settings, get_settings
from app.db.models import (
    Account,
    Iso8583MessageLog,
    JournalEntryType,
    MessageDirection,
    SwitchTransaction,
    TransactionStatus,
    validate_transition,
)
from app.db.seed import METIX_SUSPENSE_ACCOUNT_NUMBER, seed_database
from app.db.session import dispose_engine, get_session_factory, init_db
from app.services.audit import create_system_alert
from app.services.ledger import post_balanced_journal
from app.services.redis_guard import RedisGuard
from shared.iso8583.codec import build_0420, encode_raw_iso
from shared.iso8583.models import Iso8583Message
from shared.schemas import CbsDebitReleaseRequest

logger = logging.getLogger(__name__)


class SlaSweeper:
    """Enforces the 25-second SPIM SLA by reversing expired ROUTED transactions via MTI 0420."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: redis.Redis,
        adapter_registry: AdapterRegistry,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.redis_client = redis_client
        self.redis_guard = RedisGuard(redis_client, settings)
        self.adapters = adapter_registry
        self.settings = settings or get_settings()

    async def sweep_once(self) -> list[uuid.UUID]:
        """Claim expired ROUTED transactions, send 0420 Reversal Advice, and post reversal."""
        now = datetime.now(UTC)
        claimed: list[tuple[uuid.UUID, str, Iso8583Message]] = []

        # Phase A: Atomically claim expired ROUTED rows -> TIMED_OUT -> REVERSAL_SENT
        async with self.session_factory() as session:
            stmt = (
                select(SwitchTransaction)
                .where(
                    SwitchTransaction.status == TransactionStatus.ROUTED.value,
                    SwitchTransaction.sla_deadline_at <= now,
                )
                .order_by(SwitchTransaction.sla_deadline_at.asc())
                .with_for_update(skip_locked=True)
                .limit(25)
            )
            expired_txs = list((await session.execute(stmt)).scalars().all())
            if not expired_txs:
                return []

            for tx in expired_txs:
                validate_transition(TransactionStatus(tx.status), TransactionStatus.TIMED_OUT)
                tx.status = TransactionStatus.TIMED_OUT.value
                tx.response_code = "68"
                tx.failure_reason = "SPIM 25-second SLA exceeded (DE 39 = 68)"

                iso_0420 = build_0420(
                    originator_account=tx.originator_account_number,
                    originator_code=tx.originator_code,
                    beneficiary_account=tx.beneficiary_account_number,
                    beneficiary_code=tx.beneficiary_code,
                    amount_centavos=tx.amount_centavos,
                    orig_stan=tx.stan,
                    orig_rrn=tx.rrn,
                    orig_de7=tx.transmission_datetime,
                    response_code="68",
                )

                validate_transition(TransactionStatus(tx.status), TransactionStatus.REVERSAL_SENT)
                tx.status = TransactionStatus.REVERSAL_SENT.value
                session.add(
                    Iso8583MessageLog(
                        transaction_id=tx.id,
                        mti=iso_0420.mti.value,
                        direction=MessageDirection.OUTBOUND.value,
                        stan=iso_0420.de11,
                        rrn=iso_0420.de37,
                        response_code=iso_0420.de39,
                        payload_json=iso_0420.model_dump(mode="json"),
                        raw_iso_snippet=encode_raw_iso(iso_0420),
                        signature_key_id=self.settings.service_id,
                        signature_verified=True,
                    )
                )
                claimed.append((tx.id, tx.beneficiary_code, iso_0420))

            await session.commit()

        # Phase B: Dispatch 0420 via Beneficiary Adapter & finalize REVERSED / RECON_HOLD
        processed_ids: list[uuid.UUID] = []
        for tx_id, ben_code, iso_0420 in claimed:
            ben_adapter = self.adapters.get(ben_code)
            result = await ben_adapter.send_iso_message(
                iso_0420, timeout_seconds=10.0, switch_tx_id=tx_id
            )

            release_hold_info: tuple[str, str, str, str] | None = None
            async with self.session_factory() as session:
                tx = (
                    await session.execute(
                        select(SwitchTransaction)
                        .where(SwitchTransaction.id == tx_id)
                        .with_for_update()
                    )
                ).scalar_one()

                if tx.status != TransactionStatus.REVERSAL_SENT.value:
                    continue

                if result.response_message is not None:
                    msg_0430 = result.response_message
                    session.add(
                        Iso8583MessageLog(
                            transaction_id=tx.id,
                            mti=msg_0430.mti.value,
                            direction=MessageDirection.INBOUND.value,
                            stan=msg_0430.de11,
                            rrn=msg_0430.de37,
                            response_code=msg_0430.de39,
                            payload_json=msg_0430.model_dump(mode="json"),
                            raw_iso_snippet=encode_raw_iso(msg_0430),
                            signature_key_id=f"{ben_code.lower()}-cbs",
                            signature_verified=result.signature_verified,
                        )
                    )

                    if msg_0430.de39 == "00":
                        suspense_acct = (
                            await session.execute(
                                select(Account).where(
                                    Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER
                                )
                            )
                        ).scalar_one()
                        await post_balanced_journal(
                            session,
                            transaction_id=tx.id,
                            entry_type=JournalEntryType.REVERSAL,
                            description=(
                                f"SPIM 25s SLA timeout reversal (0420/0430) for STAN {tx.stan}"
                            ),
                            debit_account_id=suspense_acct.id,
                            credit_account_id=tx.originator_account_id,
                            amount_centavos=tx.amount_centavos,
                        )
                        daily_ref = f"{tx.originator_code}:{tx.originator_account_number}"
                        await self.redis_guard.release_daily_limit(daily_ref, tx.amount_centavos)
                        validate_transition(
                            TransactionStatus(tx.status), TransactionStatus.REVERSED
                        )
                        tx.status = TransactionStatus.REVERSED.value
                        if tx.originator_hold_ref:
                            release_hold_info = (
                                tx.originator_code,
                                tx.originator_hold_ref,
                                tx.stan,
                                tx.rrn,
                            )
                        logger.info("Reversed expired tx=%s STAN=%s via 0420/0430", tx.id, tx.stan)
                    else:
                        validate_transition(
                            TransactionStatus(tx.status), TransactionStatus.RECON_HOLD
                        )
                        tx.status = TransactionStatus.RECON_HOLD.value
                        tx.failure_reason = (
                            f"0430 reversal response returned DE 39 = {msg_0430.de39}; "
                            "placed on reconciliation hold"
                        )
                        create_system_alert(
                            session,
                            category="SLA",
                            severity="CRITICAL",
                            title=f"RECON_HOLD Escalation (STAN {tx.stan})",
                            message=tx.failure_reason,
                            source_service="sla-sweeper",
                            resource_id=str(tx.id),
                        )
                else:
                    validate_transition(TransactionStatus(tx.status), TransactionStatus.RECON_HOLD)
                    tx.status = TransactionStatus.RECON_HOLD.value
                    tx.failure_reason = (
                        "0420 reversal advice unacknowledged by beneficiary; "
                        "placed on reconciliation hold"
                    )
                    create_system_alert(
                        session,
                        category="SLA",
                        severity="CRITICAL",
                        title=f"Unacknowledged 0420 -> RECON_HOLD (STAN {tx.stan})",
                        message=tx.failure_reason,
                        source_service="sla-sweeper",
                        resource_id=str(tx.id),
                    )

                await session.commit()
                processed_ids.append(tx_id)

            # Release Originator CBS earmark hold outside DB lock so available_balance is restored
            if release_hold_info is not None:
                orig_code, hold_ref, stan, rrn = release_hold_info
                orig_adapter = self.adapters.get(orig_code)
                if orig_adapter.supports_originator_debit:
                    try:
                        await orig_adapter.release_debit(
                            CbsDebitReleaseRequest(
                                switch_tx_id=tx_id,
                                hold_ref=hold_ref,
                                stan=stan,
                                rrn=rrn,
                                reason="SPIM 25s SLA timeout reversal (0420/0430)",
                            )
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed releasing originator hold %s at %s after 0430: %s",
                            hold_ref,
                            orig_code,
                            exc,
                        )

        return processed_ids

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        logger.info(
            "Starting METIX SPIM SLA sweeper (poll=%ss, sla=%ss)",
            self.settings.sweeper_poll_interval_seconds,
            self.settings.spim_sla_seconds,
        )
        while stop_event is None or not stop_event.is_set():
            try:
                reversed_ids = await self.sweep_once()
                if reversed_ids:
                    logger.info("SLA sweeper processed %d expired tx(s)", len(reversed_ids))
            except Exception:
                logger.exception("Error during SLA sweep cycle")
            await asyncio.sleep(self.settings.sweeper_poll_interval_seconds)


async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = get_settings()
    await init_db()
    session_factory = get_session_factory()
    async with session_factory() as session:
        await seed_database(session)

    redis_client = redis.from_url(settings.redis_url, decode_responses=False)
    registry = AdapterRegistry(
        {
            "BCI_MZ": BciCoreBankingAdapter(settings, redis_client=redis_client),
            "STDB_MZ": StandardBankCoreBankingAdapter(settings, redis_client=redis_client),
            "MPESA_MZ": MobileWalletAdapter(
                bank_code="MPESA_MZ",
                bank_name="Vodacom M-Pesa Moçambique",
                settings=settings,
                redis_client=redis_client,
            ),
            "EMOLA_MZ": MobileWalletAdapter(
                bank_code="EMOLA_MZ",
                bank_name="Movitel e-Mola Moçambique",
                settings=settings,
                redis_client=redis_client,
            ),
        }
    )
    sweeper = SlaSweeper(session_factory, redis_client, registry, settings)
    try:
        await sweeper.run_forever()
    finally:
        await redis_client.aclose()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(_main())
