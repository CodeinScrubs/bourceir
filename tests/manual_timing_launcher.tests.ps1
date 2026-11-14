$ErrorActionPreference='Stop'
$directory=Join-Path $PSScriptRoot ('.timing-output\launcher-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
$fixture=Join-Path $directory 'path with spaces !'
[IO.Directory]::CreateDirectory((Join-Path $fixture 'scripts')) | Out-Null
$batch=Join-Path $fixture 'MANUAL_TIMING_CLOCK.bat'
[IO.File]::Copy((Join-Path $PSScriptRoot '..\MANUAL_TIMING_CLOCK.bat'),$batch)
$script=Join-Path $fixture 'scripts\start_manual_timing_clock.ps1'
$checks=New-Object Collections.Generic.List[object]
function Run-Fixture {
 $info=New-Object Diagnostics.ProcessStartInfo
 $info.FileName=Join-Path $env:SystemRoot 'System32\cmd.exe'
 $info.Arguments='/d /c ""'+$batch+'""'
 $info.WorkingDirectory=$fixture
 $info.UseShellExecute=$false;$info.CreateNoWindow=$true
 $info.RedirectStandardInput=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
 $child=New-Object Diagnostics.Process
 $child.StartInfo=$info
 if(-not $child.Start()){throw 'Owned launcher fixture did not start.'}
 $null=$child.Handle
 $child.StandardInput.WriteLine('');$child.StandardInput.Close()
 $stdout=$child.StandardOutput.ReadToEnd();$stderr=$child.StandardError.ReadToEnd()
 if(-not $child.WaitForExit(15000)){try{$child.Kill()}catch{};throw 'Owned launcher fixture timed out.'}
 $result=@{exit_code=$child.ExitCode;stdout=$stdout;stderr=$stderr}
 $child.Dispose();return $result
}
function Check([bool]$Value,[string]$Name){$checks.Add(@{name=$Name;passed=$Value});if(-not $Value){throw ('FAIL: '+$Name)};Write-Output ('PASS: '+$Name)}
try {
 [IO.File]::WriteAllText($script,'exit 0')
 $success=Run-Fixture
 Check ($success.exit_code -eq 0 -and $success.stdout -notmatch 'did not start successfully') 'successful child returns zero from a path with spaces and exclamation'
 [IO.File]::WriteAllText($script,'exit 7')
 $failure=Run-Fixture
 Check ($failure.exit_code -eq 7 -and $failure.stdout -match 'did not start successfully') 'child failure is visible and its exit code is preserved'
 [IO.File]::WriteAllText($script,'(')
 $parse=Run-Fixture
 Check ($parse.exit_code -ne 0 -and $parse.stdout -match 'did not start successfully') 'error before script execution is visible and returns failure'
 # Only this explicitly owned fixture file is removed, never recursively.
 [IO.File]::Delete($script)
 $missing=Run-Fixture
 Check ($missing.exit_code -eq 1 -and $missing.stdout -match 'launcher is missing') 'missing script is visible and returns failure'
} finally {
 $passed=@($checks | Where-Object {-not $_.passed}).Count -eq 0 -and $checks.Count -eq 4
 $version=[regex]::Match([IO.File]::ReadAllText((Join-Path $PSScriptRoot '..\scripts\manual_timing_guard.cs')),'const string Version\s*=\s*"([^"]+)"').Groups[1].Value
 @{scope='batch entry-point fixtures only; fake child scripts; no application or mouse input';version=$version;recorded_at_tehran=[TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,[TimeZoneInfo]::FindSystemTimeZoneById('Iran Standard Time')).ToString('o');total=$checks.Count;failed=@($checks|Where-Object{-not $_.passed}).Count;passed=$passed;checks=$checks;batch_sha256=(Get-FileHash -LiteralPath $batch -Algorithm SHA256).Hash.ToLowerInvariant();sources=@(@($batch,$PSCommandPath)|ForEach-Object{@{name=[IO.Path]::GetFileName($_);sha256=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()}})} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $directory 'report.json') -Encoding UTF8
 Write-Output ('Launcher report: '+$directory)
}
