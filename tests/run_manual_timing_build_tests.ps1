$ErrorActionPreference='Stop'
$root=Join-Path $PSScriptRoot ('.timing-output\build-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
$workspace=Join-Path $root 'work space'
$scripts=Join-Path $workspace 'scripts'
[IO.Directory]::CreateDirectory($scripts) | Out-Null
foreach ($name in @('manual_timing_clock.cs','manual_timing_click.cs','manual_timing_guard.cs','start_manual_timing_clock.ps1')) { [IO.File]::Copy((Join-Path $PSScriptRoot ('..\scripts\'+$name)),(Join-Path $scripts $name)) }
$builder=Join-Path $scripts 'start_manual_timing_clock.ps1'
$checks=New-Object Collections.Generic.List[object]
$runError=$null
function Check([bool]$Value,[string]$Name) { $checks.Add(@{name=$Name;passed=$Value}); if (-not $Value) { throw ('FAIL: '+$Name) }; Write-Output ('PASS: '+$Name) }
function Start-Build {
 $id=[Guid]::NewGuid().ToString('N');$stdout=Join-Path $root ($id+'.stdout');$stderr=Join-Path $root ($id+'.stderr')
 $p=Start-Process -FilePath powershell.exe -ArgumentList @('-NoProfile','-File',('"'+$builder+'"'),'-NoLaunch') -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
 $null=$p.Handle
 return @{process=$p;stdout=$stdout;stderr=$stderr}
}
function Finish-Build($Build) {
 if (-not $Build.process.WaitForExit(30000)) { try {$Build.process.Kill()} catch {};throw 'Owned test build timed out.' }
 return @{code=$Build.process.ExitCode;output=([IO.File]::ReadAllText($Build.stdout)).Trim();error=([IO.File]::ReadAllText($Build.stderr))}
}
try {
 $first=Finish-Build (Start-Build)
 Check ($first.code -eq 0 -and (Test-Path -LiteralPath $first.output)) 'build succeeds in workspace with spaces'
 $firstBytes=[IO.File]::ReadAllBytes($first.output)
 $second=Finish-Build (Start-Build)
 Check ($second.code -eq 0 -and $second.output -eq $first.output) 'verified cache is reused'
 $firstFolder=Split-Path $first.output -Parent
 $receipt=Get-Content -LiteralPath (Join-Path $firstFolder 'build.json') -Raw -Encoding UTF8 | ConvertFrom-Json
 Check ($receipt.schema_version -eq 2 -and $receipt.sources.Count -eq 3) 'manifest binds every source snapshot'
 foreach($source in $receipt.sources) { Check ((Get-FileHash -LiteralPath (Join-Path $firstFolder ('sources\'+$source.name)) -Algorithm SHA256).Hash.ToLowerInvariant() -eq $source.sha256) ('immutable snapshot verified: '+$source.name) }
 [IO.File]::WriteAllBytes($first.output,[byte[]](1,2,3,4))
 $recovered=Finish-Build (Start-Build)
 Check ($recovered.code -eq 0 -and $recovered.output -ne $first.output -and (Test-Path -LiteralPath $recovered.output)) 'corrupt executable recovers to a fresh artifact'
 Check ([IO.File]::ReadAllBytes($first.output).Length -eq 4) 'corrupt artifact is preserved rather than deleted'
 $runtime=Join-Path $workspace 'data\runtime\manual-clock'
 $index=@(Get-ChildItem -LiteralPath $runtime -Filter 'cache-*.json')[0]
 [IO.File]::WriteAllText($index.FullName,'{"folder":"..\\..\\..\\outside"}')
 $safe=Finish-Build (Start-Build)
 Check ($safe.code -eq 0 -and $safe.output.StartsWith($runtime+'\',[StringComparison]::OrdinalIgnoreCase)) 'tampered index cannot escape runtime directory'
 $one=Start-Build;$two=Start-Build
 $oneResult=Finish-Build $one;$twoResult=Finish-Build $two
 Check ($oneResult.code -eq 0 -and $twoResult.code -eq 0 -and $oneResult.output -eq $twoResult.output) 'simultaneous builds resolve to one verified artifact'
 $clockSource=Join-Path $scripts 'manual_timing_clock.cs'
 $saved=[IO.File]::ReadAllBytes($clockSource)
 [IO.File]::WriteAllText($clockSource,'THIS IS NOT VALID C SHARP')
 $failed=Finish-Build (Start-Build)
 Check ($failed.code -ne 0 -and [string]::IsNullOrWhiteSpace($failed.output)) 'compiler failure never falls back to an old executable'
 Check (@(Get-ChildItem -LiteralPath (Join-Path $workspace 'data\private\manual-clock') -Filter 'launcher-error-*.json').Count -gt 0) 'headless startup failure leaves a private error receipt'
 [IO.File]::WriteAllBytes($clockSource,$saved)
 $restored=Finish-Build (Start-Build)
 Check ($restored.code -eq 0 -and $restored.output -eq $safe.output) 'valid cache remains usable after a failed build'
} catch {$runError=$_.Exception.GetType().Name;throw} finally {
 $failed=@($checks|Where-Object{-not $_.passed}).Count
 $version=[regex]::Match([IO.File]::ReadAllText((Join-Path $PSScriptRoot '..\scripts\manual_timing_guard.cs')),'const string Version\s*=\s*"([^"]+)"').Groups[1].Value
 @{scope='isolated build sandbox; no application launched';version=$version;recorded_at_tehran=[TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,[TimeZoneInfo]::FindSystemTimeZoneById('Iran Standard Time')).ToString('o');total=$checks.Count;failed=$failed;passed=($null -eq $runError -and $failed -eq 0);run_error=$runError;checks=$checks;workspace=$workspace;sources=@(@(@('manual_timing_clock.cs','manual_timing_click.cs','manual_timing_guard.cs','start_manual_timing_clock.ps1')|ForEach-Object{Join-Path $PSScriptRoot ('..\scripts\'+$_)})+$PSCommandPath|ForEach-Object{@{name=[IO.Path]::GetFileName($_);sha256=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()}})} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'report.json') -Encoding UTF8
 Write-Output ('Build report: '+$root)
}
