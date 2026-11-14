param([ValidateRange(1,20)][int]$GuardTrials=3)
$ErrorActionPreference = 'Stop'
$logicOutput=@(& powershell.exe -NoProfile -File (Join-Path $PSScriptRoot 'manual_timing_clock.tests.ps1'))
if ($LASTEXITCODE -ne 0) { throw 'Clock logic tests failed.' }
$logicOutput | Write-Output
$countLine=@($logicOutput | Where-Object { $_ -match '^CLOCK_TEST_COUNT=\d+$' })
if($countLine.Count -ne 1){throw 'Clock check count missing.'}
$logicCount=[int]($countLine[0].Split('=')[1])
$runName = 'manual-clock-click-' + [Guid]::NewGuid().ToString('N')
$outputDir = Join-Path $PSScriptRoot ('.timing-output\' + $runName)
[IO.Directory]::CreateDirectory($outputDir) | Out-Null
$sourcePaths = @((Join-Path $PSScriptRoot '..\scripts\manual_timing_clock.cs'),(Join-Path $PSScriptRoot '..\scripts\manual_timing_click.cs'),(Join-Path $PSScriptRoot '..\scripts\manual_timing_guard.cs'),(Join-Path $PSScriptRoot 'manual_timing_click_smoke.cs'),$PSCommandPath)
@{ recorded_at_tehran=[TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,[TimeZoneInfo]::FindSystemTimeZoneById('Iran Standard Time')).ToString('o'); sources=@($sourcePaths | ForEach-Object { @{name=[IO.Path]::GetFileName($_);sha256=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()} }); scope='only local fixture; no broker target'; logic_tests_passed=$logicCount } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $outputDir 'manifest.json') -Encoding UTF8
$exePath = Join-Path $outputDir 'LocalClickFixture.exe'
Add-Type -Path @((Join-Path $PSScriptRoot '..\scripts\manual_timing_click.cs'), (Join-Path $PSScriptRoot '..\scripts\manual_timing_guard.cs'), (Join-Path $PSScriptRoot 'manual_timing_click_smoke.cs')) -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll') -OutputAssembly $exePath -OutputType WindowsApplication
foreach ($scenario in @('success','cancel','target_changed','target_moved','target_resized','target_disabled','recent_input')) {
    $resultPath = Join-Path $outputDir ($scenario + '.jsonl')
    $trials = if ($scenario -eq 'success') { 3 } else { $GuardTrials }
    $fixtureProcess = Start-Process -FilePath $exePath -ArgumentList @(('"' + $resultPath + '"'),$scenario,$trials) -WindowStyle Normal -PassThru
    $null=$fixtureProcess.Handle
    if (-not $fixtureProcess.WaitForExit(20000)) {
        try { $fixtureProcess.Kill() } catch { }
        throw ('Local click fixture timed out: ' + $scenario)
    }
    if (-not (Test-Path -LiteralPath $resultPath)) { throw ('No local test receipt: ' + $scenario) }
    $rows = @(Get-Content -LiteralPath $resultPath -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json })
    $rows | ConvertTo-Json -Compress
    if ($fixtureProcess.ExitCode -ne 0 -or $rows.Count -ne $trials -or @($rows | Where-Object { $_.passed -ne $true }).Count -ne 0) { throw ('Local click fixture failed: ' + $scenario) }
}
Write-Output ('Test receipts: ' + $outputDir)
