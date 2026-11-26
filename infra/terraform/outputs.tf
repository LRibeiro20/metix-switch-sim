output "namespace" {
  description = "Kubernetes namespace hosting the METIX switch stack."
  value       = module.k8s_foundation.namespace
}

output "kube_context" {
  description = "kubeconfig context the stack was applied to."
  value       = var.kube_context
}

output "service_endpoints" {
  description = "How to reach the stack (LoadBalancer on Docker Desktop; port-forward on kind)."
  value = var.kustomize_overlay == "local" ? {
    dashboard  = "http://localhost:3000"
    switch_api = "http://localhost:8000"
    api_docs   = "http://localhost:8000/docs"
    } : {
    dashboard  = "kubectl -n ${local.namespace} port-forward svc/frontend 3000:3000"
    switch_api = "kubectl -n ${local.namespace} port-forward svc/switch-api 8000:8000"
    api_docs   = "http://localhost:8000/docs (after port-forward)"
  }
}

output "pki_ca_sha1_fingerprint" {
  description = "SHA-1 fingerprint of METIX-SIMOREDE-ROOT-CA."
  value       = module.pki.ca_sha1_fingerprint
}

output "issued_service_certificates" {
  description = "Per-service mTLS certificates issued by the Root CA."
  value       = module.pki.service_certificates
}
