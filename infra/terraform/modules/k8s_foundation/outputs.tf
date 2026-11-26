output "namespace" {
  description = "Created namespace name."
  value       = kubernetes_namespace_v1.metix.metadata[0].name
}

output "secret_names" {
  description = "Names of the Secrets referenced by infra/k8s/base."
  value = [
    kubernetes_secret_v1.pki_certs.metadata[0].name,
    kubernetes_secret_v1.db_credentials.metadata[0].name,
    kubernetes_secret_v1.app_secrets.metadata[0].name,
  ]
}
