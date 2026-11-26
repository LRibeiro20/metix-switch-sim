# Local-only: talks to the cluster selected by kube_context in the local
# kubeconfig (Docker Desktop by default, kind-metix-ci in GitHub Actions).
# State stays in the local backend — nothing is deployed to remote servers.
provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}
