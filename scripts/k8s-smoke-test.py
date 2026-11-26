#!/usr/bin/env python3
"""End-to-end Kubernetes smoke test for the METIX / SIMOrede switch stack.

Uses only the Python standard library so it runs identically in GitHub Actions
(`kind` cluster) and locally against Docker Desktop Kubernetes.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

API_BASE = os.environ.get("METIX_API_URL", "http://127.0.0.1:8000").rstrip("/")
UI_BASE = os.environ.get("METIX_UI_URL", "http://127.0.0.1:3000").rstrip("/")


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any]:
    req_headers = dict(headers or {})
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, json.loads(raw) if raw else None


def wait_for_ready(url: str, label: str, attempts: int = 30, delay: float = 2.0) -> None:
    last_err: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5.0) as resp:
                if resp.status == 200:
                    print(f"[OK] {label} reachable ({url})")
                    return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(delay)
    raise SystemExit(f"[FAIL] {label} did not become reachable at {url}: {last_err}")


def main() -> None:
    # 1. Switch API /health and Next.js dashboard root
    wait_for_ready(f"{API_BASE}/health", "switch-api /health")
    status, health = http_json(f"{API_BASE}/health")
    assert status == 200, f"Unexpected /health status: {status}"
    assert health["status"] == "ok", health
    assert health["postgres"] == "connected", health
    assert health["redis"] == "connected", health
    print(f"[OK] Switch health: {health}")

    wait_for_ready(f"{UI_BASE}/", "frontend dashboard")

    # 2. OAuth 2.0 login as Clearing Maker
    status, token_body = http_json(
        f"{API_BASE}/oauth/token",
        method="POST",
        payload={
            "grant_type": "password",
            "username": "ops.maker",
            "password": "maker-mz-2026",
        },
    )
    assert status == 200 and token_body.get("access_token"), token_body
    auth_headers = {"Authorization": f"Bearer {token_body['access_token']}"}
    print(f"[OK] OAuth 2.0 token acquired for role={token_body.get('role')}")

    # 3. Verify all 4 participant adapters are HEALTHY over mTLS
    status, banks = http_json(f"{API_BASE}/api/v1/banks/connectivity", headers=auth_headers)
    assert status == 200, status
    by_code = {b["bank_code"]: b for b in banks}
    for code in ("BCI_MZ", "STDB_MZ", "MPESA_MZ", "EMOLA_MZ"):
        assert code in by_code, f"Missing bank adapter {code}"
        bank = by_code[code]
        assert bank["status"] == "HEALTHY", f"{code} unhealthy: {bank}"
        assert bank["mtls_enabled"] is True, f"{code} mTLS not enabled: {bank}"
    print("[OK] All 4 mTLS participant adapters HEALTHY (BCI_MZ, STDB_MZ, MPESA_MZ, EMOLA_MZ)")

    # 4. Verify Terraform-issued X.509 PKI chain and SHA-256 audit chain
    status, sec = http_json(f"{API_BASE}/api/v1/security/status", headers=auth_headers)
    assert status == 200, status
    assert sec["mtls_enabled"] is True, sec
    assert sec["audit_chain"]["valid"] is True, sec["audit_chain"]
    certs = {c["service_name"]: c for c in sec["certificates"]}
    for svc in ("ca", "switch-api", "bci-cbs", "stdb-cbs", "wallet-sim"):
        assert svc in certs, f"Missing certificate inventory entry: {svc}"
        assert certs[svc]["is_valid_now"] is True, certs[svc]
        assert certs[svc]["ca_chain_verified"] is True, certs[svc]
    print("[OK] Terraform-provisioned X.509 mTLS PKI & SHA-256 audit chain verified")

    # 5. Execute live BCI_MZ -> STDB_MZ interbank transfer (2,500.00 MT = 250,000 centavos)
    idem_key = f"k8s-smoke-{uuid.uuid4().hex}"
    status, transfer = http_json(
        f"{API_BASE}/api/v1/transfers",
        method="POST",
        headers=auth_headers,
        payload={
            "idempotency_key": idem_key,
            "originator_account_number": "00010012345678",
            "beneficiary_account_number": "00030055443322",
            "amount_centavos": 250_000,
        },
    )
    assert status == 200, status
    tx = transfer["transaction"]
    assert tx["status"] == "COMPLETED", tx
    assert tx["response_code"] == "00", tx
    assert tx["currency"] == "943", tx
    assert tx["originator_code"] == "BCI_MZ", tx
    assert tx["beneficiary_code"] == "STDB_MZ", tx
    print(
        f"[OK] Live interbank transfer COMPLETED: id={tx['id']} "
        f"stan={tx['stan']} rrn={tx['rrn']} auth_id={tx['auth_id']}"
    )

    # 6. Verify balanced double-entry switch ledger (SUM(DEBIT) == SUM(CREDIT))
    status, ledger = http_json(f"{API_BASE}/api/v1/ledger", headers=auth_headers)
    assert status == 200, status
    assert ledger["is_balanced"] is True, ledger
    assert ledger["total_debits_centavos"] == ledger["total_credits_centavos"] > 0, ledger
    assert ledger["suspense_balance_centavos"] == 0, ledger
    print(
        f"[OK] Double-entry ledger balanced: DR={ledger['total_debits_centavos']} "
        f"CR={ledger['total_credits_centavos']} suspense=0"
    )
    print("\nALL KUBERNETES E2E SMOKE CHECKS PASSED.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"[FAIL] HTTP {exc.code} on {exc.url}: {body}", file=sys.stderr)
        sys.exit(1)
