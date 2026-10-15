$ErrorActionPreference = 'Stop'
function Get-ArchiveSha256([string] $ArchivePath) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($ArchivePath)
    try { return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}
try {
    $projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    $runtimeRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'data\runtime\farabi'))
    if (-not $runtimeRoot.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Runtime directory is outside the project'
    }
    $runtimeLock = Get-Content -LiteralPath (Join-Path $projectRoot 'capture\runtime-lock.json') -Raw | ConvertFrom-Json
    if ($runtimeLock.node_version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid pinned Node.js version' }
    $processor = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $architecture = switch ($processor) { 'AMD64' { 'x64' } 'ARM64' { 'arm64' } default { throw '64-bit Windows is required' } }
    $nodeDirectory = Join-Path $runtimeRoot "node-v$($runtimeLock.node_version)-win-$architecture"
    $nodeExe = Join-Path $nodeDirectory 'node.exe'
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    if (-not (Test-Path -LiteralPath $nodeExe)) {
        $archiveName = "node-v$($runtimeLock.node_version)-win-$architecture.zip"
        $archivePath = Join-Path $runtimeRoot $archiveName
        $expectedHash = $runtimeLock.node_archive_sha256.$architecture
        if (-not (Test-Path -LiteralPath $archivePath) -or (Get-ArchiveSha256 $archivePath) -ne $expectedHash) {
            Write-Host "Downloading official Node.js $($runtimeLock.node_version) ($architecture)..."
            Invoke-WebRequest -UseBasicParsing -Uri "https://nodejs.org/dist/v$($runtimeLock.node_version)/$archiveName" -OutFile $archivePath -TimeoutSec 120
        }
        if ((Get-ArchiveSha256 $archivePath) -ne $expectedHash) {
            throw 'Node.js archive SHA256 mismatch; installation stopped'
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead($archivePath)
        try {
            foreach ($entry in $archive.Entries) {
                $destination = [System.IO.Path]::GetFullPath((Join-Path $runtimeRoot $entry.FullName))
                if (-not $destination.StartsWith($runtimeRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Archive path is outside the local runtime directory'
                }
                if (-not $entry.Name) { [System.IO.Directory]::CreateDirectory($destination) | Out-Null; continue }
                [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($destination)) | Out-Null
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination, $true)
            }
        } finally { $archive.Dispose() }
    }
    $actualVersion = & $nodeExe --version
    if ($LASTEXITCODE -ne 0 -or $actualVersion -ne "v$($runtimeLock.node_version)") { throw 'Local Node.js version check failed' }
    foreach ($manifest in @('package.json', 'package-lock.json')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot "capture\$manifest") -Destination (Join-Path $runtimeRoot $manifest) -Force
    }
    # Local prefix only; no administrator privileges, global packages or persistent PATH edits.
    $env:PATH = $nodeDirectory + [System.IO.Path]::PathSeparator + $env:PATH
    & $nodeExe (Join-Path $nodeDirectory 'node_modules\npm\bin\npm-cli.js') ci --prefix $runtimeRoot --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Locked Playwright installation failed' }
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $runtimeRoot 'browsers'
    & $nodeExe (Join-Path $runtimeRoot 'node_modules\playwright\cli.js') install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Chromium installation failed; check network routing and retry' }
    & $nodeExe (Join-Path $projectRoot 'scripts\check_farabi_runtime.mjs') (Join-Path $runtimeRoot 'node_modules\playwright\index.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Local Chromium smoke check failed' }
    Write-Host 'Farabi capture setup passed. Run CAPTURE_FARABI.bat and log in yourself.'
    exit 0
} catch {
    Write-Host "Farabi capture setup failed: $($_.Exception.Message)"
    exit 1
}
