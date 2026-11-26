# METIX / SIMOrede mTLS PKI — mirrors backend/shared/pki.py:
#   * RSA-2048 Root CA "METIX-SIMOREDE-ROOT-CA" (C=MZ) with SKI/AKI
#   * One serverAuth+clientAuth certificate per service with the same SANs
#     as SERVICE_IDENTITIES (+ 127.0.0.1), signed by the Root CA (SKI/AKI set)
# so the certificates pass Python 3.14 VERIFY_X509_STRICT mutual verification.

locals {
  service_identities = {
    "switch-api" = ["switch-api", "metix-switch", "localhost"]
    "bci-cbs"    = ["bci-cbs", "bci.simorede.mz", "localhost"]
    "stdb-cbs"   = ["stdb-cbs", "stdb.simorede.mz", "localhost"]
    "wallet-sim" = ["wallet-sim", "wallet.simorede.mz", "localhost"]
  }
}

resource "tls_private_key" "ca" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "ca" {
  private_key_pem = tls_private_key.ca.private_key_pem

  subject {
    country      = "MZ"
    organization = "Banco de Mocambique METIX PKI"
    common_name  = "METIX-SIMOREDE-ROOT-CA"
  }

  is_ca_certificate     = true
  set_subject_key_id    = true
  set_authority_key_id  = true
  validity_period_hours = var.ca_validity_hours
  early_renewal_hours   = 720

  allowed_uses = [
    "cert_signing",
    "crl_signing",
    "digital_signature",
  ]
}

resource "tls_private_key" "service" {
  for_each = local.service_identities

  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "service" {
  for_each = local.service_identities

  private_key_pem = tls_private_key.service[each.key].private_key_pem
  dns_names       = each.value
  ip_addresses    = ["127.0.0.1"]

  subject {
    country      = "MZ"
    organization = "METIX SIMOrede Participant"
    common_name  = each.key
  }
}

resource "tls_locally_signed_cert" "service" {
  for_each = local.service_identities

  cert_request_pem   = tls_cert_request.service[each.key].cert_request_pem
  ca_private_key_pem = tls_private_key.ca.private_key_pem
  ca_cert_pem        = tls_self_signed_cert.ca.cert_pem

  set_subject_key_id    = true
  validity_period_hours = var.certs_validity_hours
  early_renewal_hours   = 720

  allowed_uses = [
    "digital_signature",
    "key_encipherment",
    "server_auth",
    "client_auth",
  ]
}
