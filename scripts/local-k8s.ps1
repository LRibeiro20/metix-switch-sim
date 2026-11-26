<#
.SYNOPSIS
    Local Kubernetes + Terraform lifecycle helper for metix-switch-sim (Docker Desktop K8s).
.EXAMPLE
    .\scripts\local-k8s.ps1 build
    .\scripts\local-k8s.ps1 up
    .\scripts\local-k8s.ps1 status
    .\scripts\local-k8s.ps1 smoke
    .\scripts\local-k8s.ps1 down
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("build", "up", "redeploy", "status", "smoke", "down")]
    [string]$Action,

    [string]$Context = "docker-desktop",
    [ValidateSet("local", "ci-kind")]
    [string]$Overlay = "local"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$TfDir = Join-Path $RepoRoot "infra/terraform"
$Namespace = "metix-switch"

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH."
    }
}

function Assert-KubeContext {
    Assert-Command "kubectl"
    $contexts = kubectl config get-contexts -o name 2>$null
    if ($LASTEXITCODE -ne 0 -or ($contexts -notcontains $Context)) {
        throw "kubectl context '$Context' not found. Enable Kubernetes in Docker Desktop Settings -> Kubernetes (or pass -Context <name>)."
    }
    $ready = kubectl --context $Context get --raw=/readyz --request-timeout=5s 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw @"
Kubernetes API server for context '$Context' is unreachable ($ready).
Docker Desktop keeps the 'docker-desktop' kubeconfig entry even when Kubernetes is disabled or stopped.
Fix:
  1. Open Docker Desktop -> Settings -> Kubernetes.
  2. Check 'Enable Kubernetes' and click 'Apply & restart' (wait until the Kubernetes indicator turns green).
  3. Verify with: kubectl --context $Context get nodes
  4. Re-run: .\scripts\local-k8s.ps1 up
"@
    }
}

switch ($Action) {
    "build" {
        Assert-Command "docker"
        Write-Host "==> Building metix-backend:local..." -ForegroundColor Cyan
        docker build -t metix-backend:local (Join-Path $RepoRoot "backend")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

        Write-Host "==> Building metix-frontend:local (NEXT_PUBLIC_API_URL=http://localhost:8000)..." -ForegroundColor Cyan
        docker build -t metix-frontend:local `
            --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 `
            (Join-Path $RepoRoot "frontend")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

        Write-Host "==> Local images built." -ForegroundColor Green
    }

    "up" {
        Assert-Command "terraform"
        Assert-KubeContext

        Write-Host "==> Initializing Terraform..." -ForegroundColor Cyan
        terraform -chdir="$TfDir" init -input=false
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

        Write-Host "==> Applying Terraform IaC + Kustomize ($Overlay -> $Context)..." -ForegroundColor Cyan
        terraform -chdir="$TfDir" apply -auto-approve `
            -var="kube_context=$Context" `
            -var="kustomize_overlay=$Overlay"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

        Write-Host ""
        Write-Host "METIX Switch is running on Kubernetes ($Context / $Namespace):" -ForegroundColor Green
        Write-Host "  Dashboard : http://localhost:3000"
        Write-Host "  Switch API: http://localhost:8000/docs"
    }

    "redeploy" {
        Assert-KubeContext
        Write-Host "==> Restarting Deployments in namespace $Namespace..." -ForegroundColor Cyan
        kubectl --context $Context -n $Namespace rollout restart deployment
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        kubectl --context $Context -n $Namespace wait --for=condition=Available deployment --all --timeout=600s
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    "status" {
        Assert-KubeContext
        kubectl --context $Context -n $Namespace get pods,svc,pvc,networkpolicies -o wide
    }

    "smoke" {
        $python = if (Get-Command uv -ErrorAction SilentlyContinue) { "uv" } else { "python" }
        $smokeScript = Join-Path $PSScriptRoot "k8s-smoke-test.py"
        if ($python -eq "uv") {
            uv --directory (Join-Path $RepoRoot "backend") run --no-dev python $smokeScript
        } else {
            python $smokeScript
        }
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    "down" {
        Assert-Command "terraform"
        Assert-KubeContext
        Write-Host "==> Destroying Terraform-managed Kubernetes stack ($Overlay -> $Context)..." -ForegroundColor Yellow
        terraform -chdir="$TfDir" destroy -auto-approve `
            -var="kube_context=$Context" `
            -var="kustomize_overlay=$Overlay"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
