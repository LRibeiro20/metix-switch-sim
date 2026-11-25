from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, SystemAlert
from shared.security import canonical_json_bytes

GENESIS_HASH = "0" * 64


def compute_audit_hash(
    *,
    sequence_no: int,
    actor_id: str,
    actor_role: str,
    action: str,
    resource_type: str,
    resource_id: str,
    payload_json: dict[str, Any],
    prev_hash: str,
) -> str:
    body = canonical_json_bytes(payload_json).decode("utf-8")
    raw = (
        f"{sequence_no}|{actor_id}|{actor_role}|{action}|"
        f"{resource_type}|{resource_id}|{body}|{prev_hash}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def record_audit_log(
    session: AsyncSession,
    *,
    actor_id: str,
    actor_role: str,
    action: str,
    resource_type: str,
    resource_id: str,
    payload_json: dict[str, Any] | None = None,
) -> AuditLog:
    """Append a tamper-evident hash-chained entry to audit_logs using PostgreSQL advisory lock."""
    # Advisory transaction lock serializes hash-chain head calculation safely under concurrency.
    await session.execute(text("SELECT pg_advisory_xact_lock(943001)"))

    last = (
        await session.execute(select(AuditLog).order_by(AuditLog.sequence_no.desc()).limit(1))
    ).scalar_one_or_none()
    next_seq = 1 if last is None else int(last.sequence_no) + 1
    prev_hash = GENESIS_HASH if last is None else last.entry_hash
    payload = payload_json or {}
    entry_hash = compute_audit_hash(
        sequence_no=next_seq,
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        payload_json=payload,
        prev_hash=prev_hash,
    )
    row = AuditLog(
        sequence_no=next_seq,
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        payload_json=payload,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    session.add(row)
    await session.flush()
    return row


async def verify_audit_chain(session: AsyncSession) -> dict[str, Any]:
    """Verify the entire audit_logs SHA-256 hash chain from genesis to head."""
    rows = list(
        (await session.execute(select(AuditLog).order_by(AuditLog.sequence_no.asc())))
        .scalars()
        .all()
    )
    expected_prev = GENESIS_HASH
    expected_seq = 1
    for row in rows:
        if int(row.sequence_no) != expected_seq or row.prev_hash != expected_prev:
            return {
                "valid": False,
                "checked_entries": expected_seq - 1,
                "total_entries": len(rows),
                "broken_at_sequence": int(row.sequence_no),
                "reason": "Sequence gap or prev_hash link mismatch",
            }
        recomputed = compute_audit_hash(
            sequence_no=int(row.sequence_no),
            actor_id=row.actor_id,
            actor_role=row.actor_role,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            payload_json=row.payload_json or {},
            prev_hash=row.prev_hash,
        )
        if recomputed != row.entry_hash:
            return {
                "valid": False,
                "checked_entries": expected_seq - 1,
                "total_entries": len(rows),
                "broken_at_sequence": int(row.sequence_no),
                "reason": "Entry payload hash mismatch (tampering detected)",
            }
        expected_prev = row.entry_hash
        expected_seq += 1

    return {
        "valid": True,
        "checked_entries": len(rows),
        "total_entries": len(rows),
        "head_hash": expected_prev,
        "broken_at_sequence": None,
        "reason": None,
    }


def create_system_alert(
    session: AsyncSession,
    *,
    category: str,
    severity: str,
    title: str,
    message: str,
    source_service: str = "switch-api",
    resource_id: str | None = None,
) -> SystemAlert:
    alert = SystemAlert(
        category=category,
        severity=severity,
        title=title,
        message=message,
        source_service=source_service,
        resource_id=resource_id,
    )
    session.add(alert)
    return alert
