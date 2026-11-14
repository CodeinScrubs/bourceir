param([switch]$NoLaunch)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -ne 'Desktop' -or $PSVersionTable.PSVersion.Major -lt 5) { throw 'Run this launcher with Windows PowerShell 5.1 (powershell.exe), not pwsh.exe.' }
$workRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$runtimeRoot = Join-Path $workRoot 'data\runtime\manual-clock'
$privateRoot = Join-Path $workRoot 'data\private\manual-clock'
$sourceNames = @('manual_timing_clock.cs','manual_timing_click.cs','manual_timing_guard.cs')
$utf8 = New-Object Text.UTF8Encoding($false)
$buildMutex = $null
$buildOwned = $false
$exeLock = $null

function Hash-Bytes([byte[]]$Bytes) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace('-','').ToLowerInvariant() } finally { $algorithm.Dispose() }
}
function Safe-Path([string]$Path) {
    $full=[IO.Path]::GetFullPath($Path)
    $drive=[IO.Path]::GetPathRoot($workRoot)
    $basePath=if($workRoot.Length -eq $drive.Length){$workRoot}else{$workRoot.TrimEnd([char[]]'\/')}
    $prefix=if($basePath.EndsWith('\')){$basePath}else{$basePath+'\'}
    if ($full -ne $basePath -and -not $full.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'Generated path escapes the workspace.' }
    $current=$full
    while ($true) {
        try {
            $attributes=[IO.File]::GetAttributes($current)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Unsafe reparse point in a workspace path.' }
        } catch [IO.FileNotFoundException] { } catch [IO.DirectoryNotFoundException] { }
        if ($current -eq $basePath) { break }
        $current=[IO.Path]::GetDirectoryName($current)
        if ([string]::IsNullOrEmpty($current)) { throw 'Generated path containment could not be checked.' }
    }
    return $full
}
function New-SafeDirectory([string]$Path) {
    $safe=Safe-Path $Path
    [IO.Directory]::CreateDirectory($safe) | Out-Null
    Safe-Path $safe | Out-Null
}
function Runtime-Child([string]$Path) {
    $full = Safe-Path $Path
    $prefix = [IO.Path]::GetFullPath($runtimeRoot).TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'Build path escapes the intended runtime directory.' }
    return $full
}
function Write-Durable([string]$Path,[string]$Text) {
    $Path=Safe-Path $Path
    $stream = New-Object IO.FileStream($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read,4096,[IO.FileOptions]::WriteThrough)
    try { $bytes=$utf8.GetBytes($Text); $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}
function Assert-SourcesUnchanged($Snapshots) {
    foreach ($source in $Snapshots) {
        if ((Get-FileHash -LiteralPath (Safe-Path (Join-Path $PSScriptRoot $source.name)) -Algorithm SHA256).Hash.ToLowerInvariant() -ne $source.sha256) { throw 'Source changed while building. Nothing was launched; retry after editing finishes.' }
    }
}
function Valid-Artifact([string]$Folder,$Snapshots,[string]$BundleHash) {
    try {
        $folderPath = Runtime-Child $Folder
        $receipt = Get-Content -LiteralPath (Runtime-Child (Join-Path $folderPath 'build.json')) -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($receipt.schema_version -ne 2 -or $receipt.source_sha256 -ne $BundleHash -or @($receipt.sources).Count -ne $Snapshots.Count) { return $false }
        foreach ($source in $Snapshots) {
            $record = @($receipt.sources | Where-Object { $_.name -eq $source.name })
            if ($record.Count -ne 1 -or $record[0].sha256 -ne $source.sha256) { return $false }
            if ((Get-FileHash -LiteralPath (Runtime-Child (Join-Path $folderPath ('sources\' + $source.name))) -Algorithm SHA256).Hash.ToLowerInvariant() -ne $source.sha256) { return $false }
        }
        return (Get-FileHash -LiteralPath (Runtime-Child (Join-Path $folderPath 'BrsManualClock.exe')) -Algorithm SHA256).Hash.ToLowerInvariant() -eq $receipt.exe_sha256
    } catch { if ($_.Exception.Message -like '*Unsafe reparse point*') { throw }; return $false }
}
function Quote-Argument([string]$Value) {
    if ($Value.Contains('"') -or $Value.Contains("`r") -or $Value.Contains("`n")) { throw 'Unsupported character in a startup argument.' }
    $trailing = $Value.Length - $Value.TrimEnd('\').Length
    return '"' + $Value + ('\' * $trailing) + '"'
}
try {
    # Check both trees before creating either one; a failed path check writes no fallback log through the redirect.
    Safe-Path $runtimeRoot | Out-Null
    Safe-Path $privateRoot | Out-Null
    New-SafeDirectory $runtimeRoot
    New-SafeDirectory $privateRoot
    $snapshots = @($sourceNames | ForEach-Object {
        $bytes=[IO.File]::ReadAllBytes((Safe-Path (Join-Path $PSScriptRoot $_)))
        @{ name=$_; bytes=$bytes; sha256=(Hash-Bytes $bytes) }
    })
    $hashText = ($snapshots | ForEach-Object { $_.name + ':' + $_.sha256 }) -join "`n"
    $sourceHash = Hash-Bytes ($utf8.GetBytes($hashText))
    Assert-SourcesUnchanged $snapshots
    $mutexSuffix = Hash-Bytes ($utf8.GetBytes($workRoot.ToLowerInvariant()))
    $buildMutex = New-Object Threading.Mutex($false,('Local\BrsTimingClock-build-' + $mutexSuffix))
    try { $buildOwned=$buildMutex.WaitOne(30000) } catch [Threading.AbandonedMutexException] { $buildOwned=$true }
    if (-not $buildOwned) { throw 'Another build is still active; this launch timed out without starting a second build.' }
    $indexPath = Runtime-Child (Join-Path $runtimeRoot ('cache-' + $sourceHash + '.json'))
    $artifactFolder = $null
    if (Test-Path -LiteralPath $indexPath) {
        try {
            $index=Get-Content -LiteralPath $indexPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($index.folder -match ('^' + $sourceHash + '(-[0-9a-f]{32})?$')) {
                $candidate=Runtime-Child (Join-Path $runtimeRoot $index.folder)
                if (Valid-Artifact $candidate $snapshots $sourceHash) { $artifactFolder=$candidate }
            }
        } catch { if ($_.Exception.Message -like '*Unsafe reparse point*') { throw } }
    }
    if ($null -eq $artifactFolder) {
        $stage = Runtime-Child (Join-Path $runtimeRoot ('.build-' + [Guid]::NewGuid().ToString('N')))
        New-SafeDirectory (Join-Path $stage 'sources')
        foreach ($source in $snapshots) { [IO.File]::WriteAllBytes((Runtime-Child (Join-Path $stage ('sources\' + $source.name))),$source.bytes) }
        $sourcePaths = @($sourceNames | ForEach-Object { Runtime-Child (Join-Path $stage ('sources\' + $_)) })
        $stagedExe=Runtime-Child (Join-Path $stage 'BrsManualClock.exe')
        Add-Type -Path $sourcePaths -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll') -OutputAssembly $stagedExe -OutputType WindowsApplication
        Assert-SourcesUnchanged $snapshots
        $buildInfo=@{ schema_version=2;source_sha256=$sourceHash;sources=@($snapshots | ForEach-Object { @{name=$_.name;sha256=$_.sha256} });exe_sha256=(Get-FileHash -LiteralPath $stagedExe -Algorithm SHA256).Hash.ToLowerInvariant();built_at_tehran=[TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,[TimeZoneInfo]::FindSystemTimeZoneById('Iran Standard Time')).ToString('o') }
        Write-Durable (Join-Path $stage 'build.json') ($buildInfo | ConvertTo-Json -Depth 5)
        $folderName=$sourceHash
        if (Test-Path -LiteralPath (Join-Path $runtimeRoot $folderName)) { $folderName+='-' + [Guid]::NewGuid().ToString('N') }
        $artifactFolder=Runtime-Child (Join-Path $runtimeRoot $folderName)
        # Both absolute directory targets are verified children before this move.
        $stage=Runtime-Child $stage
        [IO.Directory]::Move($stage,$artifactFolder)
        if (-not (Valid-Artifact $artifactFolder $snapshots $sourceHash)) { throw 'New build failed verification. Nothing was launched.' }
        $indexTemp=Runtime-Child (Join-Path $runtimeRoot ('.index-' + [Guid]::NewGuid().ToString('N') + '.json'))
        Write-Durable $indexTemp (@{folder=$folderName;source_sha256=$sourceHash} | ConvertTo-Json)
        if (Test-Path -LiteralPath $indexPath) { [IO.File]::Replace($indexTemp,$indexPath,(Runtime-Child ($indexPath + '.previous-' + [Guid]::NewGuid().ToString('N')))) }
        else { [IO.File]::Move($indexTemp,$indexPath) }
    }
    Assert-SourcesUnchanged $snapshots
    $exePath=Runtime-Child (Join-Path $artifactFolder 'BrsManualClock.exe')
    $build=Get-Content -LiteralPath (Runtime-Child (Join-Path $artifactFolder 'build.json')) -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($NoLaunch) { Write-Output $exePath }
    else {
        # Keep the verified executable open without write/delete sharing through startup.
        $exeLock=New-Object IO.FileStream($exePath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        $hashAlgorithm=[Security.Cryptography.SHA256]::Create()
        try { $lockedHash=([BitConverter]::ToString($hashAlgorithm.ComputeHash($exeLock))).Replace('-','').ToLowerInvariant() } finally { $hashAlgorithm.Dispose() }
        if ($lockedHash -ne $build.exe_sha256) { throw 'Executable changed before launch.' }
        $nonce=[Guid]::NewGuid().ToString('N')
        $launchReceipt=Safe-Path (Join-Path $privateRoot ('launch-' + $nonce + '.json'))
        $arguments=@((Quote-Argument $workRoot),'--launch-receipt',(Quote-Argument $launchReceipt),'--launch-nonce',$nonce) -join ' '
        $launched=Start-Process -FilePath $exePath -ArgumentList $arguments -WindowStyle Normal -PassThru
        $null=$launched.Handle
        $deadline=[Diagnostics.Stopwatch]::StartNew()
        $ready=$false
        while ($deadline.Elapsed.TotalSeconds -lt 12) {
            if (Test-Path -LiteralPath $launchReceipt) {
                try { $ack=Get-Content -LiteralPath $launchReceipt -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $ack=$null }
                if ($ack -and $ack.nonce -eq $nonce) {
                    if ($ack.status -eq 'error') { throw ('Clock could not start: ' + $ack.message) }
                    if ($ack.status -eq 'ready' -and $ack.pid -eq $launched.Id -and $ack.exe_sha256 -eq $lockedHash -and $ack.armed -eq $false) {
                        Write-Durable ($launchReceipt+'.confirm') (@{nonce=$nonce} | ConvertTo-Json -Compress)
                        $ready=$true; break
                    }
                }
            }
            $launched.Refresh()
            if ($launched.HasExited) { throw ('Clock exited before becoming ready; exit code ' + $launched.ExitCode) }
            Start-Sleep -Milliseconds 50
        }
        if (-not $ready) {
            Write-Durable ($launchReceipt+'.abort') '{"startup_aborted":true}'
            throw 'Startup readiness was not confirmed. No click was armed. Check the private launch receipt and close any unresponsive clock window.'
        }
    }
} catch {
    $failure=$_
    try {
        New-SafeDirectory $privateRoot
        $errorPath=Safe-Path (Join-Path $privateRoot ('launcher-error-' + [Guid]::NewGuid().ToString('N') + '.json'))
        [IO.File]::WriteAllText($errorPath,(@{recorded_at_tehran=[TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,[TimeZoneInfo]::FindSystemTimeZoneById('Iran Standard Time')).ToString('o');error=$failure.Exception.Message;launched_ready=$false} | ConvertTo-Json),$utf8)
    } catch { }
    if (-not $NoLaunch) { Add-Type -AssemblyName System.Windows.Forms; [Windows.Forms.MessageBox]::Show($failure.Exception.Message,'Timing Clock could not start',[Windows.Forms.MessageBoxButtons]::OK,[Windows.Forms.MessageBoxIcon]::Error) | Out-Null }
    throw $failure
} finally {
    if ($exeLock) { $exeLock.Dispose() }
    if ($buildOwned) { $buildMutex.ReleaseMutex() }
    if ($buildMutex) { $buildMutex.Dispose() }
}
