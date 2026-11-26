variable "kubeconfig_path" {
  description = "Path to the kubeconfig file used by both the kubernetes provider and kubectl."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "kubeconfig context to target (docker-desktop locally, kind-metix-ci in CI)."
  type        = string
  default     = "docker-desktop"
}

variable "kustomize_overlay" {
  description = "Kustomize overlay under infra/k8s/overlays to apply."
  type        = string
  default     = "local"

  validation {
    condition     = contains(["local", "ci-kind"], var.kustomize_overlay)
    error_message = "kustomize_overlay must be one of: local, ci-kind."
  }
}

variable "rollout_timeout" {
  description = "How long kubectl waits for every Deployment to become Available after apply."
  type        = string
  default     = "900s"
}

variable "certs_validity_hours" {
  description = "Validity of per-service mTLS certificates (825 days, matching backend/shared/pki.py)."
  type        = number
  default     = 19800
}

variable "ca_validity_hours" {
  description = "Validity of the METIX-SIMOREDE-ROOT-CA certificate (10 years)."
  type        = number
  default     = 87600
}

# Local simulation database credentials. Defaults mirror docker-compose.yml so
# pgAdmin connection docs keep working through `kubectl port-forward`.
variable "databases" {
  description = "Credentials for the three isolated PostgreSQL 16 databases."
  type = map(object({
    user     = string
    password = string
    db       = string
  }))
  sensitive = true
  default = {
    switch = { user = "metix", password = "metix_secret", db = "metix_switch" }
    bci    = { user = "bci_user", password = "bci_secret", db = "bci_cbs" }
    stdb   = { user = "stdb_user", password = "stdb_secret", db = "stdb_cbs" }
  }

  validation {
    condition     = alltrue([for k in ["switch", "bci", "stdb"] : contains(keys(var.databases), k)])
    error_message = "databases must define the keys: switch, bci, stdb."
  }
}
