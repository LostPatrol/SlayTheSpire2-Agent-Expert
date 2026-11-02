# Allow project Rust builds to use every CPU, reserving two logical processors in Cargo's job count.
$ErrorActionPreference = 'Stop'
# Windows reports all logical processors independently of the current process affinity.
$RustLogicalProcessors = [int](Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
# Leave two logical processors available for the desktop during parallel crate compilation.
$RustReservedProcessors = 2
$env:CARGO_BUILD_JOBS = [string][Math]::Max(1, $RustLogicalProcessors - $RustReservedProcessors)
# Child compiler processes inherit this process's unrestricted processor-group affinity.
$RustBuildProcess = [Diagnostics.Process]::GetCurrentProcess()
try {
    $RustAllProcessors = if ($RustLogicalProcessors -ge 64) { [long]-1 } else {
        [long]::MaxValue -shr (63 - $RustLogicalProcessors)
    }
    $RustBuildProcess.ProcessorAffinity = [IntPtr]$RustAllProcessors
} catch {
    Write-Warning "Could not update build process affinity: $($_.Exception.Message)"
}
Write-Host "Rust build: $env:CARGO_BUILD_JOBS parallel Cargo jobs; $RustLogicalProcessors logical processors available."
