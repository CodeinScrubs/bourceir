param([string]$BaselineRoot='')
$ErrorActionPreference='Stop'
$workspace=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$production=if($BaselineRoot){[IO.Path]::GetFullPath($BaselineRoot)}else{$workspace}
$directory=Join-Path $PSScriptRoot ('.timing-output\rereviews-'+[Guid]::NewGuid().ToString('N').Substring(0,8))
[IO.Directory]::CreateDirectory($directory)|Out-Null
$resolver=Join-Path $PSScriptRoot 'manual_timing_sources.ps1';. $resolver
$sources=@(Resolve-TimingSources $production $directory)
$helpers=@((Join-Path $PSScriptRoot 'manual_timing_rereviews.cs'))
Add-Type -Path @($sources+$helpers) -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll')
$report=[BrsManualClock.Tests.RereviewChecks]::RunAll($directory)
$report|Set-Content -LiteralPath (Join-Path $directory 'report.json') -Encoding UTF8
@{scope='no desktop input; identical rereview harness on selected source';version=[BrsManualClock.TimingSafety]::Version;recorded_at_tehran=[BrsManualClock.TimingSafety]::Iso([DateTimeOffset]::UtcNow);sources=@(@($sources+$helpers+$PSCommandPath+$resolver)|ForEach-Object{@{name=[IO.Path]::GetFileName($_);sha256=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()}})}|ConvertTo-Json -Depth 5|Set-Content -LiteralPath (Join-Path $directory 'manifest.json') -Encoding UTF8
$result=$report|ConvertFrom-Json
$result.cases|ForEach-Object{if($_.passed){'PASS: '+$_.name}else{'FAIL: '+$_.name+' - '+$_.error}}
Write-Output ('Report: '+$directory)
if(-not $result.passed){throw ('Rereview suite failed: '+$result.failed)}
