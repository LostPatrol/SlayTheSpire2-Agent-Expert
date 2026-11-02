# Start the local dashboard with the project's Python environment.
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 18766,
    [string]$Config = 'configs/smoke.json',
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
# Resolve the project from this script so launch works from any directory.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
# All dashboard probes bypass the system proxy and use the same loopback URL as the CLI.
$DashboardUrl = "http://127.0.0.1:$Port/"

function Test-ExistingDashboard {
    # Recognize this project before reusing a listener, with compatibility for older dashboards.
    try {
        $Health = Invoke-RestMethod -Uri ($DashboardUrl + 'api/health') -NoProxy -TimeoutSec 2
    }
    catch {
        $Health = $null
    }
    if ($Health.app -eq 'sts2-agent-dashboard') {
        if (-not $Health.project_root -or [IO.Path]::GetFullPath($Health.project_root) -ne $ProjectRoot) {
            throw "Port $Port belongs to a dashboard for another project. Choose another -Port."
        }
        return $true
    }

    # A previously deployed dashboard has no health endpoint but exposes its title and snapshot.
    try {
        $Schema = Invoke-RestMethod -Uri ($DashboardUrl + 'openapi.json') -NoProxy -TimeoutSec 2
        if ($Schema.info.title -eq 'STS2 Agent Dashboard') {
            $Snapshot = Invoke-RestMethod -Uri ($DashboardUrl + 'api/snapshot') -NoProxy -TimeoutSec 2
            if ($Snapshot.run_id -and $Snapshot.status) { return $true }
        }
    }
    catch { }

    # An unrelated listener must not be mistaken for a failed dashboard startup.
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        throw "Port $Port is occupied by another service. Choose another -Port."
    }
    return $false
}

if (Test-ExistingDashboard) {
    Write-Host "Dashboard is already running: $DashboardUrl"
    if (-not $NoBrowser) { Start-Process $DashboardUrl }
    exit 0
}

$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Project Python environment is missing. Run scripts/setup.ps1 first.'
}

# The CLI starts one paused session and never launches the game.
$DashboardArguments = @('-m', 'sts2_agent.cli', '--config', $Config, 'dashboard', '--port', $Port)
if ($NoBrowser) {
    $DashboardArguments += '--no-browser'
}
Push-Location -LiteralPath $ProjectRoot
try {
    & $PythonPath @DashboardArguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
