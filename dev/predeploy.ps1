[CmdletBinding()]
param(
    [string]$LocalUrl = "http://127.0.0.1:3000",
    [switch]$SkipSmoke,
    [switch]$SkipImages,
    [switch]$SkipTerraform
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    Write-Host "[predeploy] $Label"
    Push-Location $RepoRoot
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
    }
    finally { Pop-Location }
}

if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw "npm.cmd was not found on PATH." }
$RepoPython = Join-Path $RepoRoot "runtime/.venv/Scripts/python.exe"
$PythonExe = if (Test-Path $RepoPython) {
    $RepoPython
}
else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) { throw "python was not found on PATH." }
    $PythonCommand.Source
}

Invoke-CheckedNative -Label "TypeScript" -FilePath "npm.cmd" -Arguments @("run", "typecheck")
Invoke-CheckedNative -Label "unit tests" -FilePath "npm.cmd" -Arguments @("test")
Invoke-CheckedNative -Label "lint" -FilePath "npm.cmd" -Arguments @("run", "lint", "--", "--ignore-pattern", "runtime/.venv")
Invoke-CheckedNative -Label "production build" -FilePath "npm.cmd" -Arguments @("run", "build")
Invoke-CheckedNative -Label "runtime bytecode validation" -FilePath $PythonExe -Arguments @(
    "-m", "py_compile",
    "runtime/main.py", "runtime/engine.py", "runtime/storage.py", "runtime/world.py",
    "runtime/browser_client.py", "runtime/browser_planner.py", "runtime/cognition.py"
)
Invoke-CheckedNative -Label "runtime unit tests" -FilePath $PythonExe -Arguments @("-m", "unittest", "discover", "-s", "runtime", "-p", "test_*.py", "-v")
Invoke-CheckedNative -Label "browser worker bytecode validation" -FilePath $PythonExe -Arguments @(
    "-m", "py_compile", "browser_worker/main.py", "browser_worker/policy.py", "browser_worker/egress.py"
)
Invoke-CheckedNative -Label "browser worker policy tests" -FilePath $PythonExe -Arguments @("-m", "unittest", "discover", "-s", "browser_worker", "-p", "test_*.py", "-v")

if (-not $SkipTerraform) {
    if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) { throw "terraform was not found on PATH." }
    Invoke-CheckedNative -Label "Terraform formatting" -FilePath "terraform" -Arguments @("-chdir=infra", "fmt", "-check", "-recursive")
    if (-not (Test-Path (Join-Path $RepoRoot "infra/.terraform"))) {
        Invoke-CheckedNative -Label "Terraform provider initialization" -FilePath "terraform" -Arguments @("-chdir=infra", "init", "-backend=false", "-input=false")
    }
    Invoke-CheckedNative -Label "Terraform validation" -FilePath "terraform" -Arguments @("-chdir=infra", "validate", "-no-color")
}

if (-not $SkipImages) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "docker was not found on PATH. Use -SkipImages only for source-only checks." }
    Invoke-CheckedNative -Label "runtime image build" -FilePath "docker" -Arguments @(
        "build", "--build-arg", "RUNTIME_BUILD_SHA=local-predeploy", "-f", "runtime/Dockerfile", "-t", "cashcow-runtime:predeploy", "runtime"
    )
    Invoke-CheckedNative -Label "runtime image import smoke" -FilePath "docker" -Arguments @(
        "run", "--rm", "--entrypoint", "python", "cashcow-runtime:predeploy", "-c",
        "import main, engine, storage, world, browser_client, browser_planner, cognition; print('runtime image imports: ok')"
    )
    Invoke-CheckedNative -Label "browser worker image build" -FilePath "docker" -Arguments @(
        "build", "--build-arg", "BROWSER_WORKER_BUILD_SHA=local-predeploy", "-f", "browser_worker/Dockerfile", "-t", "cashcow-browser-worker:predeploy", "browser_worker"
    )
    Invoke-CheckedNative -Label "browser worker image import smoke" -FilePath "docker" -Arguments @(
        "run", "--rm", "--entrypoint", "python", "cashcow-browser-worker:predeploy", "-c",
        "import main, policy, egress; print('browser worker image imports: ok')"
    )
}

if (Get-Command git -ErrorAction SilentlyContinue) {
    Push-Location $RepoRoot
    try {
        $inside = (& git rev-parse --is-inside-work-tree 2>$null)
        if ($inside -eq "true") {
            Invoke-CheckedNative -Label "git diff check" -FilePath "git" -Arguments @("diff", "--check")
        }
    }
    finally { Pop-Location }
}

if (-not $SkipSmoke) {
    Write-Host "[predeploy] local HTTP smoke"
    try {
        $page = Invoke-WebRequest -UseBasicParsing -Uri "$LocalUrl/" -TimeoutSec 15
        $bootstrap = Invoke-WebRequest -UseBasicParsing -Uri "$LocalUrl/api/bootstrap" -TimeoutSec 15
        if ($page.StatusCode -ne 200 -or $bootstrap.StatusCode -ne 200) { throw "Unexpected status code." }
    }
    catch {
        throw "Local dev server is required for smoke checks at $LocalUrl. Use -SkipSmoke for build-only validation. $($_.Exception.Message)"
    }
}

Write-Host "[predeploy] PASS"
