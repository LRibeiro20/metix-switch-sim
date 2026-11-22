from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.schemas import ChaosPresetRequest
from app.iso8583.codec import build_0210, build_0430
from app.iso8583.models import MTI, Iso8583Message
from app.services.chaos import ChaosConfig, get_chaos_config, set_chaos_config
from app.services.switch_engine import generate_auth_id

router = APIRouter(prefix="/sim/beneficiary", tags=["beneficiary-simulator"])

CHAOS_PRESETS: dict[str, ChaosConfig] = {
    "normal": ChaosConfig(preset="normal", latency_ms=200, drop_0210=False, force_de39=None),
    "slow_15s": ChaosConfig(preset="slow_15s", latency_ms=15_000, drop_0210=False, force_de39=None),
    "drop_0210": ChaosConfig(preset="drop_0210", latency_ms=200, drop_0210=True, force_de39=None),
    "inoperative_91": ChaosConfig(
        preset="inoperative_91", latency_ms=250, drop_0210=False, force_de39="91"
    ),
    "recon_hold": ChaosConfig(
        preset="recon_hold", latency_ms=200, drop_0210=True, force_de39=None, fail_0430=True
    ),
}


@router.get("/chaos", response_model=ChaosConfig)
async def read_chaos(request: Request) -> ChaosConfig:
    return await get_chaos_config(request.app.state.redis)


@router.post("/chaos", response_model=ChaosConfig)
async def update_chaos(payload: ChaosPresetRequest, request: Request) -> ChaosConfig:
    if payload.preset and payload.preset in CHAOS_PRESETS:
        base = CHAOS_PRESETS[payload.preset].model_copy(deep=True)
    else:
        base = await get_chaos_config(request.app.state.redis)
        if payload.preset is not None:
            base.preset = payload.preset

    updates = payload.model_dump(exclude_unset=True, exclude={"preset"})
    merged = base.model_copy(update=updates)
    return await set_chaos_config(request.app.state.redis, merged)


@router.post("/iso8583")
async def receive_iso8583(msg: Iso8583Message, request: Request) -> JSONResponse:
    """Beneficiary PSP / Mobile Wallet Simulator (MPESA_MZ / EMOLA_MZ)."""
    chaos = await get_chaos_config(request.app.state.redis)
    beneficiary_code = msg.de103.split(":", 1)[0]
    chaos_applies = chaos.target_beneficiary in (None, "", beneficiary_code)

    if msg.mti == MTI.FINANCIAL_REQUEST:
        if chaos_applies and chaos.drop_0210:
            # Simulate packet dropped after 0200 receipt (no 0210 ACK returned on wire)
            await asyncio.sleep(min(chaos.latency_ms, 500) / 1000.0)
            return JSONResponse(
                status_code=202,
                content={
                    "dropped": True,
                    "beneficiary": beneficiary_code,
                    "reason": "Chaos drop_0210 active — 0210 ACK dropped on wire",
                },
            )

        delay_ms = chaos.latency_ms if chaos_applies else 200
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

        de39 = chaos.force_de39 if chaos_applies and chaos.force_de39 else "00"
        auth_id = generate_auth_id() if de39 == "00" else None
        resp_0210 = build_0210(msg, response_code=de39, auth_id=auth_id)
        return JSONResponse(
            status_code=200,
            content={"dropped": False, "iso_message": resp_0210.model_dump(mode="json")},
        )

    if msg.mti == MTI.REVERSAL_ADVICE:
        if chaos_applies and chaos.fail_0430:
            await asyncio.sleep(0.15)
            return JSONResponse(
                status_code=202,
                content={
                    "dropped": True,
                    "beneficiary": beneficiary_code,
                    "reason": "Chaos fail_0430 active — 0430 reversal response dropped",
                },
            )

        await asyncio.sleep(0.1)
        resp_0430 = build_0430(msg, response_code="00")
        return JSONResponse(
            status_code=200,
            content={"dropped": False, "iso_message": resp_0430.model_dump(mode="json")},
        )

    return JSONResponse(
        status_code=400,
        content={"detail": f"Unsupported inbound beneficiary MTI: {msg.mti.value}"},
    )
