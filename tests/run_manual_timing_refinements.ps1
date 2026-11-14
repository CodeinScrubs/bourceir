$ErrorActionPreference='Stop'
$sources=@('..\scripts\manual_timing_clock.cs','..\scripts\manual_timing_click.cs','..\scripts\manual_timing_guard.cs','manual_timing_faults.cs','manual_timing_refinements.cs') | ForEach-Object {Join-Path $PSScriptRoot $_}
Add-Type -Path $sources -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll')
$directory=Join-Path $PSScriptRoot ('.timing-output\refinements-'+[Guid]::NewGuid().ToString('N').Substring(0,8))
[IO.Directory]::CreateDirectory($directory) | Out-Null
$report=[BrsManualClock.Tests.TimingRefinements]::RunAll()
$report | Set-Content -LiteralPath (Join-Path $directory 'report.json') -Encoding UTF8
@{scope='Refinement regressions; no desktop input';version=[BrsManualClock.TimingSafety]::Version;recorded_at_tehran=[BrsManualClock.TimingSafety]::Iso([DateTimeOffset]::UtcNow);sources=@(@($sources+$PSCommandPath) | ForEach-Object {@{name=[IO.Path]::GetFileName($_);sha256=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()}})} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $directory 'manifest.json') -Encoding UTF8
$result=$report | ConvertFrom-Json
$result.cases | ForEach-Object {if($_.passed){'PASS: '+$_.name}else{'FAIL: '+$_.name+' - '+$_.error}}
Write-Output ('Report: '+$directory)
if(-not $result.passed){throw ('Refinement suite failed: '+$result.failed)}
