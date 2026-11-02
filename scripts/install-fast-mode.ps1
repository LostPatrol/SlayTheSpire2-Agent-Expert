# Install pinned official SpeedX packages without changing any game save or progress.
param(
    [string]$GameRoot = 'D:\Steam\steamapps\common\Slay the Spire 2',
    [string]$PackageDirectory,
    [switch]$ConfigureOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
# Keep downloads and backups outside the game's recursively scanned mod directory.
if (-not $PackageDirectory) { $PackageDirectory = Join-Path $projectRoot 'local/fast-mode/packages' }
$packageRoot = [IO.Path]::GetFullPath($PackageDirectory)
$gamePath = [IO.Path]::GetFullPath($GameRoot)
if (-not (Test-Path -LiteralPath (Join-Path $gamePath 'SlayTheSpire2.exe'))) { throw 'Game executable was not found.' }
if (Get-Process -Name SlayTheSpire2 -ErrorAction SilentlyContinue) { throw 'Close the game before installing or configuring SpeedX.' }
# Published archive hashes come from the author's pinned release distribution manifest.
$packages = @(
    @{ Id = 'ModConfig'; Version = '0.2.3'; Hash = 'dab047d5cea9163e4fdce73b05aed8fd4790c5210d327bcd6f620415db7b9d06' },
    @{ Id = 'SpeedX'; Version = '0.11.12'; Hash = '08e958e220f7860916f8833e49f281cbe91f57fcc7af8c23b3946c2ebc84e632' }
)
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $projectRoot "local/mod-backups/speedx-$stamp"
New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
if (-not $ConfigureOnly) {
    foreach ($package in $packages) {
        $filename = "$($package.Id)_v$($package.Version).zip"
        $archive = Join-Path $packageRoot $filename
        $url = "https://github.com/xhyrzldf/sts2-pp-mod-release/releases/download/$($package.Id)-v$($package.Version)/$filename"
        if (-not (Test-Path -LiteralPath $archive)) { Invoke-WebRequest -Uri $url -OutFile $archive }
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $package.Hash) {
            throw "Official archive hash mismatch: $filename"
        }
        $staging = Join-Path $packageRoot "$($package.Id)-$($package.Version)"
        Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
        $source = Join-Path $staging $package.Id
        $destination = Join-Path $gamePath "mods/$($package.Id)"
        if (Test-Path -LiteralPath $destination) {
            New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
            Copy-Item -LiteralPath $destination -Destination $backupRoot -Recurse
        }
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        foreach ($extension in @('dll', 'pck', 'json')) {
            $file = "$($package.Id).$extension"
            Copy-Item -LiteralPath (Join-Path $source $file) -Destination (Join-Path $destination $file) -Force
        }
        Write-Output "Installed $($package.Id) $($package.Version) from verified official archive."
    }
}
# Configuration uses the mod's own persisted format, populated after its settings page is inspected.
$preset = Join-Path $projectRoot 'configs/speedx-agent.json'
if (Test-Path -LiteralPath $preset) {
    $configRoot = Join-Path $env:APPDATA 'SlayTheSpire2/ModConfig'
    $target = Join-Path $configRoot 'sts2.piyixiajiuhenfen.speedx.json'
    New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
    if (Test-Path -LiteralPath $target) {
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        Copy-Item -LiteralPath $target -Destination $backupRoot
    }
    Copy-Item -LiteralPath $preset -Destination $target -Force
    Write-Output 'Applied the reviewed SpeedX Agent preset.'
} else {
    Write-Warning 'No reviewed Agent preset exists yet. Keep gameplay paused until SpeedX automation is disabled in F9 settings.'
}
