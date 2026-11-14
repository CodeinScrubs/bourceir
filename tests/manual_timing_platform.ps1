$ErrorActionPreference='Stop'
$sources=@('..\scripts\manual_timing_clock.cs','..\scripts\manual_timing_click.cs','..\scripts\manual_timing_guard.cs') | ForEach-Object { Join-Path $PSScriptRoot $_ }
Add-Type -Path $sources -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll')
$desktop=[BrsManualClock.TargetGuard]::ActiveDesktop()
$precise=[BrsManualClock.DesktopInput]::PreciseUtcNow()
$inputSize=[BrsManualClock.DesktopInput]::NativeInputSize
$expectedSize=if([IntPtr]::Size -eq 8){40}else{28}
$checks=@(
    @{name='interactive desktop name available';passed=(-not [string]::IsNullOrWhiteSpace($desktop))},
    @{name='precise clock agrees with UTC within diagnostic bound';passed=([Math]::Abs(($precise-[DateTimeOffset]::UtcNow).TotalSeconds) -lt 5)},
    @{name='native INPUT layout matches process architecture';passed=($inputSize -eq $expectedSize)}
)
$failed=@($checks|Where-Object{-not $_.passed}).Count
$value=@{scope='read-only platform capability assertions, not input delivery evidence';version=[BrsManualClock.TimingSafety]::Version;recorded_at_tehran=[BrsManualClock.TimingSafety]::Iso($precise);desktop=$desktop;is_64_bit=[Environment]::Is64BitProcess;framework=[Environment]::Version.ToString();input_size=$inputSize;total=$checks.Count;failed=$failed;passed=($failed -eq 0);checks=$checks;input_sent=$false}
$value|ConvertTo-Json -Depth 4
if($failed -ne 0){throw 'Platform capability assertion failed.'}
