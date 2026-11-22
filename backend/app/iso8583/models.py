from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class MTI(StrEnum):
    FINANCIAL_REQUEST = "0200"
    FINANCIAL_RESPONSE = "0210"
    REVERSAL_ADVICE = "0420"
    REVERSAL_RESPONSE = "0430"


ISO_RESPONSE_DESCRIPTIONS: dict[str, str] = {
    "00": "Approved or completed successfully",
    "51": "Insufficient funds",
    "61": "Exceeds withdrawal / Notice 1/GBM/2026 daily limit",
    "68": "Response received too late (25s SPIM SLA timeout)",
    "91": "Issuer or switch inoperative",
    "96": "System malfunction",
}


class Iso8583Message(BaseModel):
    """Canonical ISO 8583 message representation for METIX / SIMOrede."""

    mti: MTI
    de2: str = Field(..., min_length=6, max_length=19, description="Primary Account Number / ID")
    de3: str = Field(
        default="280000", pattern=r"^\d{6}$", description="Processing Code (P2P Instant)"
    )
    de4: int = Field(..., gt=0, description="Transaction amount in minor units (centavos)")
    de7: str = Field(..., pattern=r"^\d{10}$", description="Transmission date & time (MMDDhhmmss)")
    de11: str = Field(..., pattern=r"^\d{6}$", description="Systems Trace Audit Number (STAN)")
    de37: str = Field(
        ..., min_length=12, max_length=12, description="Retrieval Reference Number (RRN)"
    )
    de38: str | None = Field(
        default=None,
        min_length=6,
        max_length=6,
        description="Authorization Identification Response",
    )
    de39: str | None = Field(
        default=None, pattern=r"^[A-Z0-9]{2}$", description="ISO 8583 Response Code"
    )
    de49: Literal["943"] = Field(default="943", description="Currency Code (943 = MZN)")
    de90: str | None = Field(
        default=None, min_length=42, max_length=42, description="Original Data Elements (0420/0430)"
    )
    de102: str = Field(..., min_length=3, max_length=64, description="Originator Account ID")
    de103: str = Field(..., min_length=3, max_length=64, description="Beneficiary Account ID")

    @field_validator("de4")
    @classmethod
    def validate_amount_bounds(cls, v: int) -> int:
        if v > 999_999_999_999:
            raise ValueError("DE 4 exceeds 12-digit ISO 8583 field capacity")
        return v

    @model_validator(mode="after")
    def validate_mti_requirements(self) -> Iso8583Message:
        if self.mti in (MTI.FINANCIAL_RESPONSE, MTI.REVERSAL_RESPONSE) and self.de39 is None:
            raise ValueError(f"MTI {self.mti.value} requires DE 39 (Response Code)")
        if self.mti in (MTI.REVERSAL_ADVICE, MTI.REVERSAL_RESPONSE) and self.de90 is None:
            raise ValueError(f"MTI {self.mti.value} requires DE 90 (Original Data Elements)")
        return self


def format_de7(dt: datetime | None = None) -> str:
    """Return UTC transmission timestamp formatted as MMDDhhmmss."""
    target = (dt or datetime.now(UTC)).astimezone(UTC)
    return target.strftime("%m%d%H%M%S")


def build_de90(
    orig_mti: str,
    orig_stan: str,
    orig_de7: str,
    orig_acquirer_id: str = "METIXSWITCH",
    orig_forwarding_id: str = "SIMOREDE001",
) -> str:
    """Build 42-char ISO 8583 DE 90 Original Data Elements."""
    acq = orig_acquirer_id[:11].ljust(11, "0")
    fwd = orig_forwarding_id[:11].ljust(11, "0")
    return f"{orig_mti:0>4}{orig_stan:0>6}{orig_de7:0>10}{acq}{fwd}"
