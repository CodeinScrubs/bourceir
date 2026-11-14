param([switch]$NoInput)
$ErrorActionPreference='Stop'
$output=Join-Path $PSScriptRoot ('.timing-output\ui-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
[IO.Directory]::CreateDirectory($output) | Out-Null
$build=@(& powershell.exe -NoProfile -File (Join-Path $PSScriptRoot '..\scripts\start_manual_timing_clock.ps1') -NoLaunch)
if($LASTEXITCODE -ne 0){throw 'Production build failed.'}
$assembly=Join-Path $output 'BrsManualClock.dll'
$builtExecutables=@($build | Where-Object { $_ -is [string] -and $_.EndsWith('\BrsManualClock.exe',[StringComparison]::OrdinalIgnoreCase) -and [IO.File]::Exists($_) })
if($builtExecutables.Count -ne 1){throw 'Builder did not return exactly one existing executable.'}
[IO.File]::Copy([string]$builtExecutables[0],$assembly)
$sourcePaths=@('..\scripts\manual_timing_clock.cs','..\scripts\manual_timing_click.cs','..\scripts\manual_timing_guard.cs','..\scripts\start_manual_timing_clock.ps1','manual_timing_ui_smoke.cs')|ForEach-Object{Join-Path $PSScriptRoot $_}
@{scope='owned local forms only';no_input_mode=[bool]$NoInput;recorded_at_tehran=[TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,[TimeZoneInfo]::FindSystemTimeZoneById('Iran Standard Time')).ToString('o');reference_assembly_sha256=(Get-FileHash -LiteralPath $assembly -Algorithm SHA256).Hash.ToLowerInvariant();sources=@(@($sourcePaths+$PSCommandPath)|ForEach-Object{@{name=[IO.Path]::GetFileName($_);sha256=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()}})}|ConvertTo-Json -Depth 4|Set-Content -LiteralPath (Join-Path $output 'manifest.json') -Encoding UTF8
$fixture=Join-Path $output 'LocalUiFixture.exe'
Add-Type -Path (Join-Path $PSScriptRoot 'manual_timing_ui_smoke.cs') -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll',$assembly) -OutputAssembly $fixture -OutputType WindowsApplication
$arguments='"'+$output+'"'
if($NoInput){$arguments+=' --no-input'}
$process=Start-Process -FilePath $fixture -ArgumentList $arguments -WindowStyle Normal -PassThru
$null=$process.Handle
if(-not $process.WaitForExit(45000)){try{$process.Kill()}catch{};throw 'Owned UI fixture timed out.'}
$report=Get-Content -LiteralPath (Join-Path $output 'report.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$report.cases | ForEach-Object {if($_.passed){'PASS: '+$_.name}else{'FAIL: '+$_.name+' '+$_.error}}
Write-Output ('UI report: '+$output)
if($process.ExitCode -ne 0 -or -not $report.passed){throw 'UI workflow verification failed.'}
