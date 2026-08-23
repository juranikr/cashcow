[CmdletBinding()]
param(
    [string]$LocalUrl = "http://127.0.0.1:3000",
    [switch]$SkipSmoke
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

Invoke-CheckedNative -Label "TypeScript" -FilePath "npm.cmd" -Arguments @("run", "typecheck")
Invoke-CheckedNative -Label "unit tests" -FilePath "npm.cmd" -Arguments @("test")
Invoke-CheckedNative -Label "lint" -FilePath "npm.cmd" -Arguments @("run", "lint")
Invoke-CheckedNative -Label "production build" -FilePath "npm.cmd" -Arguments @("run", "build")

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
