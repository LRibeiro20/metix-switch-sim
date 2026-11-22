from __future__ import annotations

from app.iso8583.models import MTI, Iso8583Message, build_de90, format_de7

# Field metadata for wire encoding/decoding
# Fixed fields specify exact char length; LLVAR fields use 2-digit decimal length prefix.
_FIELD_ORDER: list[tuple[int, str, str, int | None]] = [
    (2, "de2", "LLVAR", None),
    (3, "de3", "FIXED", 6),
    (4, "de4", "FIXED_INT", 12),
    (7, "de7", "FIXED", 10),
    (11, "de11", "FIXED", 6),
    (37, "de37", "FIXED", 12),
    (38, "de38", "FIXED", 6),
    (39, "de39", "FIXED", 2),
    (49, "de49", "FIXED", 3),
    (90, "de90", "FIXED", 42),
    (102, "de102", "LLVAR", None),
    (103, "de103", "LLVAR", None),
]


def _compute_hex_bitmap(present_bits: set[int]) -> str:
    """Compute 128-bit primary+secondary ISO 8583 hexadecimal bitmap (32 hex chars)."""
    bits = set(present_bits)
    if any(b > 64 for b in bits):
        bits.add(1)  # Bit 1 indicates presence of secondary bitmap (DE 65..128)
    bitmap_int = 0
    for bit in bits:
        if not 1 <= bit <= 128:
            raise ValueError(f"ISO 8583 bit out of range: {bit}")
        bitmap_int |= 1 << (128 - bit)
    return f"{bitmap_int:032X}"


def _parse_hex_bitmap(bitmap_hex: str) -> set[int]:
    """Parse 32-char hex bitmap into set of active data element numbers."""
    bitmap_int = int(bitmap_hex, 16)
    active: set[int] = set()
    for bit in range(1, 129):
        if bitmap_int & (1 << (128 - bit)):
            active.add(bit)
    return active


def encode_raw_iso(msg: Iso8583Message) -> str:
    """Encode an Iso8583Message into a canonical ASCII ISO 8583 wire frame."""
    present_bits: set[int] = set()
    body_parts: list[str] = []

    for bit, attr, kind, fixed_len in _FIELD_ORDER:
        val = getattr(msg, attr)
        if val is None:
            continue
        present_bits.add(bit)
        if kind == "FIXED_INT":
            assert fixed_len is not None
            body_parts.append(f"{int(val):0{fixed_len}d}")
        elif kind == "FIXED":
            assert fixed_len is not None
            str_val = str(val)
            if len(str_val) != fixed_len:
                raise ValueError(f"DE {bit} must be length {fixed_len}, got {len(str_val)}")
            body_parts.append(str_val)
        elif kind == "LLVAR":
            str_val = str(val)
            if len(str_val) > 99:
                raise ValueError(f"DE {bit} exceeds LLVAR max length 99")
            body_parts.append(f"{len(str_val):02d}{str_val}")

    bitmap = _compute_hex_bitmap(present_bits)
    return f"{msg.mti.value}{bitmap}{''.join(body_parts)}"


def decode_raw_iso(raw: str) -> Iso8583Message:
    """Decode an ASCII ISO 8583 wire frame back into an Iso8583Message."""
    if len(raw) < 36:
        raise ValueError("Raw ISO 8583 message too short to contain MTI and 128-bit bitmap")
    mti = MTI(raw[:4])
    bitmap_hex = raw[4:36]
    active_bits = _parse_hex_bitmap(bitmap_hex)

    cursor = 36
    parsed: dict[str, object] = {"mti": mti}

    for bit, attr, kind, fixed_len in _FIELD_ORDER:
        if bit not in active_bits:
            continue
        if kind == "FIXED_INT":
            assert fixed_len is not None
            chunk = raw[cursor : cursor + fixed_len]
            cursor += fixed_len
            parsed[attr] = int(chunk)
        elif kind == "FIXED":
            assert fixed_len is not None
            chunk = raw[cursor : cursor + fixed_len]
            cursor += fixed_len
            parsed[attr] = chunk
        elif kind == "LLVAR":
            length = int(raw[cursor : cursor + 2])
            cursor += 2
            chunk = raw[cursor : cursor + length]
            cursor += length
            parsed[attr] = chunk

    return Iso8583Message.model_validate(parsed)


def build_0200(
    *,
    originator_account: str,
    originator_code: str,
    beneficiary_account: str,
    beneficiary_code: str,
    amount_centavos: int,
    stan: str,
    rrn: str,
    de7: str | None = None,
) -> Iso8583Message:
    return Iso8583Message(
        mti=MTI.FINANCIAL_REQUEST,
        de2=originator_account,
        de3="280000",
        de4=amount_centavos,
        de7=de7 or format_de7(),
        de11=stan,
        de37=rrn,
        de49="943",
        de102=f"{originator_code}:{originator_account}",
        de103=f"{beneficiary_code}:{beneficiary_account}",
    )


def build_0210(
    req: Iso8583Message,
    *,
    response_code: str,
    auth_id: str | None = None,
) -> Iso8583Message:
    return Iso8583Message(
        mti=MTI.FINANCIAL_RESPONSE,
        de2=req.de2,
        de3=req.de3,
        de4=req.de4,
        de7=format_de7(),
        de11=req.de11,
        de37=req.de37,
        de38=auth_id,
        de39=response_code,
        de49="943",
        de102=req.de102,
        de103=req.de103,
    )


def build_0420(
    *,
    originator_account: str,
    originator_code: str,
    beneficiary_account: str,
    beneficiary_code: str,
    amount_centavos: int,
    orig_stan: str,
    orig_rrn: str,
    orig_de7: str,
    response_code: str = "68",
) -> Iso8583Message:
    return Iso8583Message(
        mti=MTI.REVERSAL_ADVICE,
        de2=originator_account,
        de3="280020",
        de4=amount_centavos,
        de7=format_de7(),
        de11=orig_stan,
        de37=orig_rrn,
        de39=response_code,
        de49="943",
        de90=build_de90(
            orig_mti=MTI.FINANCIAL_REQUEST.value,
            orig_stan=orig_stan,
            orig_de7=orig_de7,
        ),
        de102=f"{originator_code}:{originator_account}",
        de103=f"{beneficiary_code}:{beneficiary_account}",
    )


def build_0430(rev: Iso8583Message, *, response_code: str = "00") -> Iso8583Message:
    return Iso8583Message(
        mti=MTI.REVERSAL_RESPONSE,
        de2=rev.de2,
        de3=rev.de3,
        de4=rev.de4,
        de7=format_de7(),
        de11=rev.de11,
        de37=rev.de37,
        de39=response_code,
        de49="943",
        de90=rev.de90,
        de102=rev.de102,
        de103=rev.de103,
    )
