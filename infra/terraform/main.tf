locals {
  # Must match `namespace:` in infra/k8s/base/kustomization.yaml.
  namespace = "metix-switch"

  k8s_root     = abspath("${path.module}/../k8s")
  overlay_path = "${local.k8s_root}/overlays/${var.kustomize_overlay}"
  kubeconfig   = pathexpand(var.kubeconfig_path)

  # Re-apply whenever any base or overlay manifest changes.
  manifests_hash = sha1(join("", [
    for f in sort(fileset(local.k8s_root, "**/*.yaml")) : filesha1("${local.k8s_root}/${f}")
  ]))
}

# 1. X.509 mTLS PKI (Root CA + per-service certificates).
module "pki" {
  source = "./modules/pki"

  ca_validity_hours    = var.ca_validity_hours
  certs_validity_hours = var.certs_validity_hours
}

# 2. Namespace + Secrets (PKI bundle, DB credentials, app crypto secrets).
module "k8s_foundation" {
  source = "./modules/k8s_foundation"

  namespace  = local.namespace
  pki_bundle = module.pki.bundle
  databases  = var.databases
}

# 3a. Teardown hook — stable inputs so it only runs `delete -k` on destroy.
# Uses working_dir and environment instead of quoted paths so Windows cmd.exe
# and Linux /bin/sh both handle paths with spaces without escaping issues.
resource "terraform_data" "kustomize_teardown" {
  input = {
    kubeconfig   = local.kubeconfig
    context      = var.kube_context
    overlay_path = local.overlay_path
  }

  provisioner "local-exec" {
    when        = destroy
    working_dir = self.input.overlay_path
    environment = {
      KUBECONFIG = self.input.kubeconfig
    }
    command = "kubectl --context ${self.input.context} delete -k . --ignore-not-found --wait=true"
  }

  depends_on = [module.k8s_foundation]
}

# 3b. Apply the Kustomize overlay and wait for every workload to become ready.
# Replaced (re-applied in place via `kubectl apply`) whenever manifests change.
# Secret rotation does not restart pods by itself: run `scripts/local-k8s redeploy`.
resource "terraform_data" "kustomize_apply" {
  triggers_replace = {
    manifests_hash = local.manifests_hash
    overlay        = var.kustomize_overlay
    context        = var.kube_context
  }

  provisioner "local-exec" {
    working_dir = local.overlay_path
    environment = {
      KUBECONFIG = local.kubeconfig
    }
    command = "kubectl --context ${var.kube_context} apply -k ."
  }

  provisioner "local-exec" {
    environment = {
      KUBECONFIG = local.kubeconfig
    }
    command = "kubectl --context ${var.kube_context} -n ${local.namespace} wait --for=condition=Available deployment --all --timeout=${var.rollout_timeout}"
  }

  depends_on = [terraform_data.kustomize_teardown]
}
