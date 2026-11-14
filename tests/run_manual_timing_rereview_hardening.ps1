$ErrorActionPreference='Stop'
$sources=@('..\scripts\manual_timing_clock.cs','..\scripts\manual_timing_click.cs','..\scripts\manual_timing_guard.cs','manual_timing_rereviews.cs','manual_timing_rereview_hardening.cs')|ForEach-Object{Join-Path $PSScriptRoot $_}
Add-Type -Path $sources -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll')
$directory=Join-Path $PSScriptRoot ('.timing-output\rereview-hardening-'+[Guid]::NewGuid().ToString('N').Substring(0,8));[IO.Directory]::CreateDirectory($directory)|Out-Null
$report=[BrsManualClock.Tests.RereviewHardening]::RunAll($directory);$report|Set-Content -LiteralPath (Join-Path $directory 'report.json') -Encoding UTF8
@{scope='no desktop input';version=[BrsManualClock.TimingSafety]::Version;recorded_at_tehran=[BrsManualClock.TimingSafety]::Iso([DateTimeOffset]::UtcNow);sources=@($sources|ForEach-Object{@{name=[IO.Path]::GetFileName($_);sha256=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()}})}|ConvertTo-Json -Depth 4|Set-Content -LiteralPath (Join-Path $directory 'manifest.json') -Encoding UTF8
$result=$report|ConvertFrom-Json;$result.cases|ForEach-Object{if($_.passed){'PASS: '+$_.name}else{'FAIL: '+$_.name+' - '+$_.error}}
Write-Output ('Report: '+$directory);if(-not $result.passed){throw ('Rereview hardening failed: '+$result.failed)}
