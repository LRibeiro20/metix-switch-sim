from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import jwt
import redis.asyncio as redis
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

DEFAULT_JWT_SECRET = "metix-simorede-jwt-signing-secret-2026-bm-notice-1"
DEFAULT_ENCRYPTION_KEY = "metix-simorede-aes256-master-key-2026"
DEFAULT_SIGNING_MASTER_SEED = "metix-simorede-ed25519-key-registry-2026"
SIGNATURE_SKEW_SECONDS = 300


class Role(StrEnum):
    OPS_VIEWER = "OPS_VIEWER"
    MAKER = "MAKER"
    CHECKER = "CHECKER"
    FRAUD_ANALYST = "FRAUD_ANALYST"
    ADMIN = "ADMIN"
    SERVICE_ADAPTER = "SERVICE_ADAPTER"


ROLE_SCOPES: dict[Role, tuple[str, ...]] = {
    Role.OPS_VIEWER: (
        "transfers:read",
        "accounts:read",
        "settlement:read",
        "recon:read",
        "fraud:read",
        "security:read",
    ),
    Role.MAKER: (
        "transfers:read",
        "transfers:write",
        "accounts:read",
        "accounts:restrict:propose",
        "settlement:read",
        "settlement:run",
        "recon:read",
        "recon:run",
        "recon:adjust:propose",
        "fraud:read",
        "security:read",
    ),
    Role.CHECKER: (
        "transfers:read",
        "accounts:read",
        "accounts:restrict:approve",
        "settlement:read",
        "settlement:approve",
        "recon:read",
        "recon:adjust:approve",
        "fraud:read",
        "fraud:review",
        "security:read",
    ),
    Role.FRAUD_ANALYST: (
        "transfers:read",
        "accounts:read",
        "fraud:read",
        "fraud:review",
        "security:read",
    ),
    Role.ADMIN: (
        "transfers:read",
        "transfers:write",
        "accounts:read",
        "accounts:restrict:propose",
        "accounts:restrict:approve",
        "settlement:read",
        "settlement:run",
        "settlement:approve",
        "recon:read",
        "recon:run",
        "recon:adjust:propose",
        "recon:adjust:approve",
        "fraud:read",
        "fraud:review",
        "security:read",
        "security:admin",
        "chaos:write",
    ),
    Role.SERVICE_ADAPTER: (
        "cbs:debit",
        "cbs:credit",
        "cbs:read",
        "cbs:recon",
        "cbs:chaos",
        "switch:callback",
    ),
}


@dataclass(frozen=True)
class Principal:
    subject: str
    display_name: str
    role: Role
    scopes: tuple[str, ...]
    client_id: str | None = None
    token_id: str | None = None


@dataclass(frozen=True)
class SeededCredential:
    username: str
    secret: str
    display_name: str
    role: Role
    grant_type: str  # "password" or "client_credentials"


# Built-in operator personas and inter-service OAuth 2.0 clients for SIMOrede operations
SEEDED_CREDENTIALS: dict[str, SeededCredential] = {
    "ops.viewer": SeededCredential(
        username="ops.viewer",
        secret="viewer-mz-2026",
        display_name="Inês Nhantumbo (Ops Viewer)",
        role=Role.OPS_VIEWER,
        grant_type="password",
    ),
    "ops.maker": SeededCredential(
        username="ops.maker",
        secret="maker-mz-2026",
        display_name="Mateus Machava (Clearing Maker)",
        role=Role.MAKER,
        grant_type="password",
    ),
    "ops.checker": SeededCredential(
        username="ops.checker",
        secret="checker-mz-2026",
        display_name="Graça Machel-Cossa (Clearing Checker)",
        role=Role.CHECKER,
        grant_type="password",
    ),
    "fraud.analyst": SeededCredential(
        username="fraud.analyst",
        secret="fraud-mz-2026",
        display_name="Sérgio Cumbane (Fraud Analyst)",
        role=Role.FRAUD_ANALYST,
        grant_type="password",
    ),
    "sec.admin": SeededCredential(
        username="sec.admin",
        secret="admin-mz-2026",
        display_name="Lúcia Tembe (Switch Admin)",
        role=Role.ADMIN,
        grant_type="password",
    ),
    # OAuth 2.0 client_credentials service clients
    "switch-api": SeededCredential(
        username="switch-api",
        secret="switch-adapter-secret-2026",
        display_name="METIX Central Switch Engine",
        role=Role.SERVICE_ADAPTER,
        grant_type="client_credentials",
    ),
    "bci-cbs": SeededCredential(
        username="bci-cbs",
        secret="bci-cbs-secret-2026",
        display_name="BCI Core Banking Simulator",
        role=Role.SERVICE_ADAPTER,
        grant_type="client_credentials",
    ),
    "stdb-cbs": SeededCredential(
        username="stdb-cbs",
        secret="stdb-cbs-secret-2026",
        display_name="Standard Bank Core Banking Simulator",
        role=Role.SERVICE_ADAPTER,
        grant_type="client_credentials",
    ),
    "wallet-sim": SeededCredential(
        username="wallet-sim",
        secret="wallet-sim-secret-2026",
        display_name="Mobile Wallet Adapter Simulator",
        role=Role.SERVICE_ADAPTER,
        grant_type="client_credentials",
    ),
}


def authenticate_credential(
    *,
    grant_type: str,
    username: str | None = None,
    password: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> Principal | None:
    if grant_type == "client_credentials":
        if not client_id or not client_secret:
            return None
        cred = SEEDED_CREDENTIALS.get(client_id)
        if cred is None or cred.grant_type != "client_credentials":
            return None
        if not hmac.compare_digest(cred.secret, client_secret):
            return None
        return Principal(
            subject=cred.username,
            display_name=cred.display_name,
            role=cred.role,
            scopes=ROLE_SCOPES[cred.role],
            client_id=client_id,
        )

    if grant_type == "password":
        if not username or not password:
            return None
        cred = SEEDED_CREDENTIALS.get(username)
        if cred is None or cred.grant_type != "password":
            return None
        if not hmac.compare_digest(cred.secret, password):
            return None
        return Principal(
            subject=cred.username,
            display_name=cred.display_name,
            role=cred.role,
            scopes=ROLE_SCOPES[cred.role],
            client_id=client_id or "metix-ops-dashboard",
        )
    return None


def issue_access_token(
    principal: Principal,
    *,
    jwt_secret: str = DEFAULT_JWT_SECRET,
    expires_seconds: int = 3600,
) -> tuple[str, int]:
    now = datetime.now(UTC)
    jti = uuid.uuid4().hex
    payload = {
        "sub": principal.subject,
        "name": principal.display_name,
        "role": principal.role.value,
        "scope": " ".join(principal.scopes),
        "cid": principal.client_id or principal.subject,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_seconds)).timestamp()),
        "iss": "metix-simorede-oauth2",
    }
    token = jwt.encode(payload, jwt_secret, algorithm="HS256")
    return token, expires_seconds


def decode_access_token(
    token: str,
    *,
    jwt_secret: str = DEFAULT_JWT_SECRET,
) -> Principal:
    try:
        claims = jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"],
            issuer="metix-simorede-oauth2",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired OAuth 2.0 access token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        role = Role(claims["role"])
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed role claim in OAuth 2.0 token",
        ) from exc

    raw_scope = str(claims.get("scope", ""))
    scopes = tuple(s for s in raw_scope.split(" ") if s)
    return Principal(
        subject=str(claims["sub"]),
        display_name=str(claims.get("name", claims["sub"])),
        role=role,
        scopes=scopes,
        client_id=str(claims.get("cid", "")),
        token_id=str(claims.get("jti", "")),
    )


_bearer_scheme = HTTPBearer(auto_error=False)


def _extract_jwt_secret(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return getattr(settings, "jwt_secret", DEFAULT_JWT_SECRET)


async def get_current_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> Principal:
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing OAuth 2.0 Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decode_access_token(creds.credentials, jwt_secret=_extract_jwt_secret(request))


async def get_optional_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> Principal | None:
    if creds is None or not creds.credentials:
        return None
    return decode_access_token(creds.credentials, jwt_secret=_extract_jwt_secret(request))


def require_roles(*allowed_roles: Role):
    allowed = set(allowed_roles)

    async def _dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"RBAC denied: role '{principal.role.value}' is not authorized "
                    f"(required one of: {', '.join(sorted(r.value for r in allowed))})"
                ),
            )
        return principal

    return _dependency


def require_scopes(*required_scopes: str):
    needed = set(required_scopes)

    async def _dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not needed.issubset(set(principal.scopes)):
            missing = sorted(needed - set(principal.scopes))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required OAuth 2.0 scope(s): {', '.join(missing)}",
            )
        return principal

    return _dependency


# ---------------------------------------------------------------------------
# Ed25519 Digital Signatures (Inter-service message & settlement report signing)
# ---------------------------------------------------------------------------


def derive_ed25519_keypair(
    service_id: str,
    *,
    master_seed: str = DEFAULT_SIGNING_MASTER_SEED,
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Deterministically derive a per-service Ed25519 keypair from master seed."""
    seed = hashlib.sha256(f"{master_seed}:{service_id}".encode()).digest()
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    return priv, priv.public_key()


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _signing_input(method: str, path: str, timestamp: str, nonce: str, body_bytes: bytes) -> bytes:
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode()


def sign_http_payload(
    *,
    method: str,
    path: str,
    body_bytes: bytes,
    service_id: str,
    master_seed: str = DEFAULT_SIGNING_MASTER_SEED,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Sign an HTTP request or response using Ed25519 over canonical method/path/time/nonce/body."""
    ts = timestamp or str(int(time.time()))
    nc = nonce or secrets.token_hex(16)
    priv, _ = derive_ed25519_keypair(service_id, master_seed=master_seed)
    sig_bytes = priv.sign(_signing_input(method, path, ts, nc, body_bytes))
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode("ascii")
    return {
        "X-Signature-Alg": "Ed25519",
        "X-Key-Id": service_id,
        "X-Timestamp": ts,
        "X-Nonce": nc,
        "X-Payload-Signature": sig_b64,
    }


def sign_document(
    document: dict[str, Any],
    *,
    service_id: str = "switch-api",
    master_seed: str = DEFAULT_SIGNING_MASTER_SEED,
) -> str:
    """Create a detached Ed25519 signature over canonical JSON (e.g., settlement reports)."""
    priv, _ = derive_ed25519_keypair(service_id, master_seed=master_seed)
    sig = priv.sign(canonical_json_bytes(document))
    return base64.urlsafe_b64encode(sig).decode("ascii")


def verify_document_signature(
    document: dict[str, Any],
    signature_b64: str,
    *,
    service_id: str = "switch-api",
    master_seed: str = DEFAULT_SIGNING_MASTER_SEED,
) -> bool:
    _, pub = derive_ed25519_keypair(service_id, master_seed=master_seed)
    try:
        sig = base64.urlsafe_b64decode(signature_b64.encode("ascii"))
        pub.verify(sig, canonical_json_bytes(document))
        return True
    except Exception:
        return False


async def verify_signed_http_request(
    request: Request,
    *,
    redis_client: redis.Redis | None = None,
    allowed_key_ids: set[str] | None = None,
    master_seed: str = DEFAULT_SIGNING_MASTER_SEED,
) -> dict[str, str]:
    """Verify Ed25519 signature headers, timestamp window, and Redis replay nonce."""
    alg = request.headers.get("x-signature-alg")
    key_id = request.headers.get("x-key-id")
    ts_str = request.headers.get("x-timestamp")
    nonce = request.headers.get("x-nonce")
    sig_b64 = request.headers.get("x-payload-signature")

    if not all([alg, key_id, ts_str, nonce, sig_b64]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Ed25519 digital signature headers",
        )
    assert alg and key_id and ts_str and nonce and sig_b64

    if alg != "Ed25519":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unsupported signature algorithm: {alg}",
        )
    if allowed_key_ids is not None and key_id not in allowed_key_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Untrusted signing key_id: {key_id}",
        )

    try:
        ts_val = int(ts_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid X-Timestamp header"
        ) from exc

    now_ts = int(time.time())
    if abs(now_ts - ts_val) > SIGNATURE_SKEW_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Signed request timestamp outside acceptable replay window",
        )

    body_bytes = await request.body()
    _, pub = derive_ed25519_keypair(key_id, master_seed=master_seed)
    try:
        sig_bytes = base64.urlsafe_b64decode(sig_b64.encode("ascii"))
        pub.verify(
            sig_bytes,
            _signing_input(request.method, request.url.path, ts_str, nonce, body_bytes),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ed25519 digital signature verification failed (payload tampered or wrong key)",
        ) from exc

    if redis_client is not None:
        nonce_key = f"metix:sig:nonce:{key_id}:{nonce}"
        fresh = await redis_client.set(nonce_key, "1", nx=True, ex=SIGNATURE_SKEW_SECONDS * 2)
        if not fresh:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Replay attack detected: X-Nonce already consumed",
            )

    return {"key_id": key_id, "algorithm": alg, "nonce": nonce, "timestamp": ts_str}


# ---------------------------------------------------------------------------
# AES-256-GCM Authenticated Encryption at Rest (CBS PII & Sensitive Metadata)
# ---------------------------------------------------------------------------


class FieldEncryptor:
    """AES-256-GCM authenticated field encryptor with HMAC-SHA256 blind indexing."""

    def __init__(self, master_secret: str = DEFAULT_ENCRYPTION_KEY) -> None:
        self._aes_key = hashlib.sha256(f"aes:{master_secret}".encode()).digest()
        self._hmac_key = hashlib.sha256(f"hmac:{master_secret}".encode()).digest()
        self._aesgcm = AESGCM(self._aes_key)

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(12)
        ct = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        n_b64 = base64.urlsafe_b64encode(nonce).decode("ascii")
        c_b64 = base64.urlsafe_b64encode(ct).decode("ascii")
        return f"enc:v1:{n_b64}:{c_b64}"

    def decrypt(self, token: str) -> str:
        if not token.startswith("enc:v1:"):
            raise ValueError("Invalid encrypted field envelope")
        _, _, n_b64, c_b64 = token.split(":", 3)
        nonce = base64.urlsafe_b64decode(n_b64.encode("ascii"))
        ct = base64.urlsafe_b64decode(c_b64.encode("ascii"))
        pt = self._aesgcm.decrypt(nonce, ct, None)
        return pt.decode("utf-8")

    def blind_index(self, plaintext: str) -> str:
        return hmac.new(
            self._hmac_key, plaintext.strip().lower().encode("utf-8"), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def mask_holder_name(name: str) -> str:
        parts = name.split()
        masked: list[str] = []
        for part in parts:
            if len(part) <= 2 or part.startswith("("):
                masked.append(part)
            else:
                masked.append(part[:2] + "*" * min(4, len(part) - 2))
        return " ".join(masked)
