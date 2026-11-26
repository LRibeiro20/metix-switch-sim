locals {
  db_hosts = {
    switch = "postgres"
    bci    = "bci-postgres"
    stdb   = "stdb-postgres"
  }

  # Flattened keys consumed by infra/k8s/base (e.g. switch-user, bci-url).
  db_secret_data = merge([
    for name, db in var.databases : {
      "${name}-user"     = db.user
      "${name}-password" = db.password
      "${name}-db"       = db.db
      "${name}-url"      = "postgresql+asyncpg://${db.user}:${urlencode(db.password)}@${local.db_hosts[name]}:5432/${db.db}"
    }
  ]...)

  app_secret_data = {
    JWT_SECRET          = random_password.jwt_secret.result
    ENCRYPTION_KEY      = random_password.encryption_key.result
    SIGNING_MASTER_SEED = random_password.signing_master_seed.result
  }
}

resource "kubernetes_namespace_v1" "metix" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of"    = "metix-switch"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# Shared across every service: OAuth 2.0 JWTs issued by switch-api are verified by
# the CBS/wallet simulators, and Ed25519 keys are derived from the same master seed.
resource "random_password" "jwt_secret" {
  length  = 64
  special = false
}

resource "random_password" "encryption_key" {
  length  = 64
  special = false
}

resource "random_password" "signing_master_seed" {
  length  = 64
  special = false
}

resource "kubernetes_secret_v1" "pki_certs" {
  metadata {
    name      = "metix-pki-certs"
    namespace = kubernetes_namespace_v1.metix.metadata[0].name
    labels = {
      "app.kubernetes.io/part-of"    = "metix-switch"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
  type = "Opaque"
  data = var.pki_bundle
}

resource "kubernetes_secret_v1" "db_credentials" {
  metadata {
    name      = "metix-db-credentials"
    namespace = kubernetes_namespace_v1.metix.metadata[0].name
    labels = {
      "app.kubernetes.io/part-of"    = "metix-switch"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
  type = "Opaque"
  data = local.db_secret_data
}

resource "kubernetes_secret_v1" "app_secrets" {
  metadata {
    name      = "metix-app-secrets"
    namespace = kubernetes_namespace_v1.metix.metadata[0].name
    labels = {
      "app.kubernetes.io/part-of"    = "metix-switch"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
  type = "Opaque"
  data = local.app_secret_data
}
