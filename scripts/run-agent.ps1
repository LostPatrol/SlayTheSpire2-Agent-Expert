# Run the local external policy with UTF-8 console output and a bounded action budget.
param(
    [ValidateSet('run', 'inspect', 'probe')][string]$Command = 'inspect',
    [int]$MaxActions = 20
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8 = '1'
$arguments = @('-m', 'sts2_agent.cli', '--root', $projectRoot, $Command)
if ($Command -eq 'run') { $arguments += @('--max-actions', [string]$MaxActions) }
& (Join-Path $projectRoot '.venv/Scripts/python.exe') @arguments
if ($LASTEXITCODE -ne 0) { throw 'The controller stopped. Read its result.json for the safe error.' }
