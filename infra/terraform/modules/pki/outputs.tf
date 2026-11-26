# File layout expected by backend/shared/pki.py under CERTS_DIR (/certs).
# ca.key is included so ensure_pki_bundle() finds a complete bundle and never
# attempts to write to the read-only Secret mount.
output "bundle" {
  description = "Map of file name => PEM content for the metix-pki-certs Secret."
  sensitive   = true
  value = merge(
    {
      "ca.crt" = tls_self_signed_cert.ca.cert_pem
      "ca.key" = tls_private_key.ca.private_key_pem
    },
    { for name, cert in tls_locally_signed_cert.service : "${name}.crt" => cert.cert_pem },
    { for name, key in tls_private_key.service : "${name}.key" => key.private_key_pem },
  )
}

data "tls_certificate" "ca" {
  content = tls_self_signed_cert.ca.cert_pem
}

output "ca_sha1_fingerprint" {
  description = "SHA-1 fingerprint of METIX-SIMOREDE-ROOT-CA."
  value       = upper(data.tls_certificate.ca.certificates[0].sha1_fingerprint)
}

output "ca_cert_pem" {
  description = "Public Root CA certificate (safe to share; trust anchor for mTLS clients)."
  value       = tls_self_signed_cert.ca.cert_pem
}

output "service_certificates" {
  description = "SANs and expiry of every issued service certificate."
  value = {
    for name, cert in tls_locally_signed_cert.service : name => {
      dns_names    = local.service_identities[name]
      validity_end = cert.validity_end_time
      issuer       = "METIX-SIMOREDE-ROOT-CA"
    }
  }
}
