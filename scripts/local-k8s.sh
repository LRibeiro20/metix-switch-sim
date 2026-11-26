#!/usr/bin/env bash
# Local Kubernetes + Terraform lifecycle helper for metix-switch-sim.
# Usage: ./scripts/local-k8s.sh <build|up|redeploy|status|smoke|down> [context] [overlay]
set -euo pipefail

ACTION="${1:-}"
CONTEXT="${2:-docker-desktop}"
OVERLAY="${3:-local}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${REPO_ROOT}/infra/terraform"
NAMESPACE="metix-switch"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 1; }
}

check_context() {
  require_cmd kubectl
  if ! kubectl config get-contexts -o name | grep -qx "${CONTEXT}"; then
    echo "kubectl context '${CONTEXT}' not found. Enable Kubernetes in Docker Desktop or pass a valid context." >&2
    exit 1
  fi
  if ! kubectl --context "${CONTEXT}" get --raw=/readyz --request-timeout=5s >/dev/null 2>&1; then
    echo "Kubernetes API server for context '${CONTEXT}' is unreachable." >&2
    echo "Enable Kubernetes in Docker Desktop -> Settings -> Kubernetes, wait until it is green, verify 'kubectl --context ${CONTEXT} get nodes', then re-run." >&2
    exit 1
  fi
}

case "${ACTION}" in
  build)
    require_cmd docker
    echo "==> Building metix-backend:local..."
    docker build -t metix-backend:local "${REPO_ROOT}/backend"
    echo "==> Building metix-frontend:local..."
    docker build -t metix-frontend:local \
      --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 \
      "${REPO_ROOT}/frontend"
    ;;
  up)
    require_cmd terraform
    check_context
    terraform -chdir="${TF_DIR}" init -input=false
    terraform -chdir="${TF_DIR}" apply -auto-approve \
      -var="kube_context=${CONTEXT}" \
      -var="kustomize_overlay=${OVERLAY}"
    echo ""
    echo "METIX Switch is running on Kubernetes (${CONTEXT} / ${NAMESPACE}):"
    echo "  Dashboard : http://localhost:3000"
    echo "  Switch API: http://localhost:8000/docs"
    ;;
  redeploy)
    check_context
    kubectl --context "${CONTEXT}" -n "${NAMESPACE}" rollout restart deployment
    kubectl --context "${CONTEXT}" -n "${NAMESPACE}" wait --for=condition=Available deployment --all --timeout=600s
    ;;
  status)
    check_context
    kubectl --context "${CONTEXT}" -n "${NAMESPACE}" get pods,svc,pvc,networkpolicies -o wide
    ;;
  smoke)
    python3 "${REPO_ROOT}/scripts/k8s-smoke-test.py"
    ;;
  down)
    require_cmd terraform
    check_context
    terraform -chdir="${TF_DIR}" destroy -auto-approve \
      -var="kube_context=${CONTEXT}" \
      -var="kustomize_overlay=${OVERLAY}"
    kubectl --context "${CONTEXT}" -n "${NAMESPACE}" delete pvc --all --ignore-not-found >/dev/null 2>&1 || true
    ;;
  *)
    echo "Usage: $0 <build|up|redeploy|status|smoke|down> [context] [overlay]" >&2
    exit 1
    ;;
esac
