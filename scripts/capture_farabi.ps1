param([switch] $Check, [switch] $Watch)
$ErrorActionPreference = 'Stop'
try {
    $projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    Set-Location -LiteralPath $projectRoot
    $customRuntime = [bool]($env:BRS_NODE -or $env:BRS_PLAYWRIGHT_MODULE)
    if ($customRuntime) {
        if (-not $env:BRS_NODE -or -not $env:BRS_PLAYWRIGHT_MODULE) { throw 'Provide both BRS_NODE and BRS_PLAYWRIGHT_MODULE for a custom runtime.' }
        $nodeExe = $env:BRS_NODE
        $modulePath = $env:BRS_PLAYWRIGHT_MODULE
    } else {
        $runtimeLock = Get-Content -LiteralPath (Join-Path $projectRoot 'capture\runtime-lock.json') -Raw | ConvertFrom-Json
        $processor = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        $architecture = switch ($processor) { 'AMD64' { 'x64' } 'ARM64' { 'arm64' } default { throw '64-bit Windows is required.' } }
        $runtimeRoot = Join-Path $projectRoot 'data\runtime\farabi'
        $nodeExe = Join-Path $runtimeRoot "node-v$($runtimeLock.node_version)-win-$architecture\node.exe"
        $modulePath = Join-Path $runtimeRoot 'node_modules\playwright\index.mjs'
        $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $runtimeRoot 'browsers'
    }
    function Test-CaptureRuntime {
        if (-not (Test-Path -LiteralPath $nodeExe) -or -not (Test-Path -LiteralPath $modulePath)) { return $false }
        if (-not $customRuntime) {
            $actualVersion = & $nodeExe --version
            if ($LASTEXITCODE -ne 0 -or $actualVersion -ne "v$($runtimeLock.node_version)") { return $false }
        }
        try {
            & $nodeExe (Join-Path $PSScriptRoot 'check_farabi_runtime.mjs') $modulePath | Out-Host
            return $LASTEXITCODE -eq 0
        } catch { return $false }
    }
    if (-not (Test-CaptureRuntime)) {
        if ($customRuntime) { throw 'Custom capture runtime is unavailable. Check Node.js, Playwright and browser paths.' }
        Write-Host 'Installing or repairing the local read-only capture runtime...'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'setup_farabi.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Capture setup failed; fix the reported connection or installation error and retry.' }
        if (-not (Test-CaptureRuntime)) { throw 'Capture runtime is still unavailable after setup.' }
    }
    if ($Check) { Write-Host 'Capture entry point check passed. No website or account was opened.'; exit 0 }
    $env:BRS_PLAYWRIGHT_MODULE = $modulePath
    $entryScript = if ($Watch) { 'farabi_watch.mjs' } else { 'farabi_capture.mjs' }
    & $nodeExe (Join-Path $PSScriptRoot $entryScript)
    exit $LASTEXITCODE
} catch {
    Write-Host "Farabi capture could not start: $($_.Exception.Message)"
    exit 1
}
