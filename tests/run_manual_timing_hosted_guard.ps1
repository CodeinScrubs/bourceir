$ErrorActionPreference='Stop'
$directory=Join-Path $PSScriptRoot ('.timing-output\host-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
[IO.Directory]::CreateDirectory($directory) | Out-Null
$exe=Join-Path $directory 'LocalHostedFixture.exe'
$sources=@('..\scripts\manual_timing_click.cs','..\scripts\manual_timing_guard.cs','manual_timing_hosted_guard.cs') | ForEach-Object {Join-Path $PSScriptRoot $_}
Add-Type -Path $sources -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll') -OutputAssembly $exe -OutputType WindowsApplication
@{scope='two owned processes; no desktop input';recorded_at_tehran=[TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,[TimeZoneInfo]::FindSystemTimeZoneById('Iran Standard Time')).ToString('o');sources=@(@($sources+$PSCommandPath)|ForEach-Object{@{name=[IO.Path]::GetFileName($_);sha256=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()}})}|ConvertTo-Json -Depth 4|Set-Content -LiteralPath (Join-Path $directory 'manifest.json') -Encoding UTF8
$process=Start-Process -FilePath $exe -ArgumentList ('"'+$directory+'"') -WindowStyle Normal -PassThru
$null=$process.Handle
if(-not $process.WaitForExit(15000)){try{$process.Kill()}catch{};throw 'Owned hosted-control fixture timed out.'}
$report=Get-Content -LiteralPath (Join-Path $directory 'report.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$report | ConvertTo-Json -Compress
Write-Output ('Hosted-control report: '+$directory)
if($process.ExitCode -ne 0 -or -not $report.passed){throw 'Hosted-control guard verification failed.'}
