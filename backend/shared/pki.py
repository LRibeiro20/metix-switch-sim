from __future__ import annotations

import ipaddress
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SERVICE_IDENTITIES: dict[str, list[str]] = {
    "switch-api": ["switch-api", "metix-switch", "localhost"],
    "bci-cbs": ["bci-cbs", "bci.simorede.mz", "localhost"],
    "stdb-cbs": ["stdb-cbs", "stdb.simorede.mz", "localhost"],
    "wallet-sim": ["wallet-sim", "wallet.simorede.mz", "localhost"],
}


@dataclass(frozen=True)
class CertMetadata:
    service_name: str
    subject_cn: str
    issuer_cn: str
    serial_hex: str
    sha256_fingerprint: str
    not_before: datetime
    not_after: datetime
    is_valid_now: bool


def _write_pem(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _generate_ca(certs_dir: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    ca_cert_path = certs_dir / "ca.crt"
    ca_key_path = certs_dir / "ca.key"

    if ca_cert_path.exists() and ca_key_path.exists():
        cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return cert, key

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "MZ"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Banco de Mocambique METIX PKI"),
            x509.NameAttribute(NameOID.COMMON_NAME, "METIX-SIMOREDE-ROOT-CA"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    _write_pem(ca_cert_path, cert.public_bytes(serialization.Encoding.PEM))
    _write_pem(
        ca_key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    return cert, key


def _generate_service_cert(
    certs_dir: Path,
    service_name: str,
    dns_names: list[str],
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
) -> Path:
    cert_path = certs_dir / f"{service_name}.crt"
    key_path = certs_dir / f"{service_name}.key"
    if cert_path.exists() and key_path.exists():
        return cert_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "MZ"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "METIX SIMOrede Participant"),
            x509.NameAttribute(NameOID.COMMON_NAME, service_name),
        ]
    )
    san_entries: list[x509.GeneralName] = [x509.DNSName(name) for name in dns_names]
    san_entries.append(x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")))

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    _write_pem(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    _write_pem(
        key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    return cert_path


def ensure_pki_bundle(certs_dir: str | Path) -> dict[str, Path]:
    """Ensure Root CA and per-service mTLS X.509 certificates exist in certs_dir."""
    root = Path(certs_dir)
    root.mkdir(parents=True, exist_ok=True)
    ca_cert, ca_key = _generate_ca(root)
    paths: dict[str, Path] = {"ca": root / "ca.crt"}
    for svc, sans in SERVICE_IDENTITIES.items():
        paths[svc] = _generate_service_cert(root, svc, sans, ca_cert, ca_key)
    return paths


def inspect_certificate(certs_dir: str | Path, service_name: str) -> CertMetadata | None:
    """Read X.509 metadata for a service certificate or CA."""
    filename = "ca.crt" if service_name == "ca" else f"{service_name}.crt"
    path = Path(certs_dir) / filename
    if not path.exists():
        return None
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    now = datetime.now(UTC)
    subject_cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    issuer_cn = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    fp = cert.fingerprint(hashes.SHA256()).hex().upper()
    formatted_fp = ":".join(fp[i : i + 2] for i in range(0, len(fp), 2))
    return CertMetadata(
        service_name=service_name,
        subject_cn=str(subject_cn),
        issuer_cn=str(issuer_cn),
        serial_hex=format(cert.serial_number, "X"),
        sha256_fingerprint=formatted_fp,
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        is_valid_now=cert.not_valid_before_utc <= now <= cert.not_valid_after_utc,
    )


def verify_cert_signed_by_ca(certs_dir: str | Path, service_name: str) -> bool:
    """Verify cryptographic X.509 chain between service cert and METIX Root CA."""
    root = Path(certs_dir)
    ca_path = root / "ca.crt"
    svc_path = root / f"{service_name}.crt"
    if not ca_path.exists() or not svc_path.exists():
        return False
    ca_cert = x509.load_pem_x509_certificate(ca_path.read_bytes())
    svc_cert = x509.load_pem_x509_certificate(svc_path.read_bytes())
    now = datetime.now(UTC)
    if not (svc_cert.not_valid_before_utc <= now <= svc_cert.not_valid_after_utc):
        return False
    try:
        svc_cert.verify_directly_issued_by(ca_cert)
        return True
    except Exception:
        return False


def build_client_mtls_context(certs_dir: str | Path, client_service: str) -> ssl.SSLContext:
    """Create an httpx-compatible SSLContext enforcing Mutual TLS (mTLS)."""
    root = Path(certs_dir)
    ensure_pki_bundle(root)
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(root / "ca.crt"))
    ctx.load_cert_chain(
        certfile=str(root / f"{client_service}.crt"),
        keyfile=str(root / f"{client_service}.key"),
    )
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def build_server_mtls_uvicorn_kwargs(
    certs_dir: str | Path, server_service: str
) -> dict[str, object]:
    """Return uvicorn SSL keyword arguments requiring verified client certificates (mTLS)."""
    root = Path(certs_dir)
    ensure_pki_bundle(root)
    return {
        "ssl_certfile": str(root / f"{server_service}.crt"),
        "ssl_keyfile": str(root / f"{server_service}.key"),
        "ssl_ca_certs": str(root / "ca.crt"),
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }


def list_pki_inventory(certs_dir: str | Path) -> list[CertMetadata]:
    """Return metadata for the Root CA and every participant mTLS certificate."""
    ensure_pki_bundle(certs_dir)
    items: list[CertMetadata] = []
    for name in ("ca", *SERVICE_IDENTITIES.keys()):
        meta = inspect_certificate(certs_dir, name)
        if meta is not None:
            items.append(meta)
    return items
