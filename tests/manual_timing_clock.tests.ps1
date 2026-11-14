$ErrorActionPreference = 'Stop'
Add-Type -Path @((Join-Path $PSScriptRoot '..\scripts\manual_timing_clock.cs'), (Join-Path $PSScriptRoot '..\scripts\manual_timing_click.cs'), (Join-Path $PSScriptRoot '..\scripts\manual_timing_guard.cs')) -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll')
$script:checkCount=0
function Assert-True([bool]$Condition, [string]$Name) {
    if (-not $Condition) { throw "FAIL: $Name" }
    Write-Output "PASS: $Name"
    $script:checkCount++
}
$before = [DateTimeOffset]::Parse('2026-10-24T08:44:59+03:30')
$target = [BrsManualClock.ClockLogic]::NextTarget($before)
Assert-True ($target.ToString('o') -eq '2026-10-24T08:45:00.0000000+03:30') 'same day before deadline'
$after = [BrsManualClock.ClockLogic]::NextTarget($target)
Assert-True ($after.Day -eq 25 -and $after.Hour -eq 8 -and $after.Minute -eq 45) 'past target rolls to next day'
$utc = [DateTimeOffset]::Parse('2026-10-24T05:14:59Z')
Assert-True ([BrsManualClock.ClockLogic]::NextTarget($utc) -eq $target) 'input UTC maps to Tehran'
$yearEnd = [BrsManualClock.ClockLogic]::NextTarget([DateTimeOffset]::Parse('2026-12-31T09:00:00+03:30'))
Assert-True ($yearEnd.Year -eq 2027 -and $yearEnd.Month -eq 1 -and $yearEnd.Day -eq 1) 'calendar year rollover'
Assert-True ([BrsManualClock.ClockLogic]::Decide($before,$target,$true,16,16) -eq 'wait') 'never cue before deadline'
Assert-True ([BrsManualClock.ClockLogic]::Decide($target.AddMilliseconds(8),$target,$true,16,16) -eq 'cue') 'cue on first observed deadline crossing'
Assert-True ([BrsManualClock.ClockLogic]::Decide($target,$target,$false,16,16) -eq 'idle') 'no armed target means no cue'
Assert-True ([BrsManualClock.ClockLogic]::Decide($target,$target,$true,250,16) -eq 'clock_change') 'clock jump cancels'
Assert-True ([BrsManualClock.ClockLogic]::Decide($target,$target,$true,9000,9000) -eq 'scheduler_gap') 'suspend or long stall cancels'
Assert-True ([BrsManualClock.ClockLogic]::Decide($before,$target,$true,-500,16) -eq 'clock_change') 'backward clock change cancels'
$fractional = [TimeSpan]::ParseExact('08:45:00.125','hh\:mm\:ss\.fff',[Globalization.CultureInfo]::InvariantCulture)
$fractionalTarget = [BrsManualClock.ClockLogic]::NextTarget($before,$fractional)
Assert-True ($fractionalTarget.Millisecond -eq 125) 'custom target preserves millisecond'
$parsedTime = [TimeSpan]::Zero
Assert-True ([BrsManualClock.ClockLogic]::TryTime('23:59:59.999',[ref]$parsedTime) -and $parsedTime.Milliseconds -eq 999) 'last millisecond of day is valid'
Assert-True (-not [BrsManualClock.ClockLogic]::TryTime('24:00:00.000',[ref]$parsedTime)) 'hour 24 rejected'
Assert-True (-not [BrsManualClock.ClockLogic]::TryTime('08:60:00.000',[ref]$parsedTime)) 'minute 60 rejected'
Assert-True (-not [BrsManualClock.ClockLogic]::TryTime('08:45:60.000',[ref]$parsedTime)) 'second 60 rejected'
Assert-True (-not [BrsManualClock.ClockLogic]::TryTime('08:45:00',[ref]$parsedTime)) 'missing millisecond rejected'
Assert-True ([BrsManualClock.DesktopInput]::NormalizeAbsolute(-1920,-1920,3840) -eq 8) 'negative monitor first pixel maps to its center'
Assert-True ([BrsManualClock.DesktopInput]::NormalizeAbsolute(1919,-1920,3840) -eq 65527) 'last desktop pixel maps to its center'
$outOfBoundsRejected = $false
try { [BrsManualClock.DesktopInput]::NormalizeAbsolute(1920,-1920,3840) | Out-Null } catch { $outOfBoundsRejected = $true }
Assert-True $outOfBoundsRejected 'pixel beyond desktop rejected'
Assert-True ([BrsManualClock.ClickPlan]::ValidateTime($before,$target,100,16,16) -eq 'wait') 'click cannot occur early'
Assert-True ([BrsManualClock.ClickPlan]::ValidateTime($target.AddMilliseconds(100),$target,100,16,16) -eq 'due') 'lateness exactly at configured boundary accepted'
Assert-True ([BrsManualClock.ClickPlan]::ValidateTime($target.AddTicks(1001000),$target,100,16,16) -eq 'too_late') 'late click rejected'
Assert-True ([BrsManualClock.ClickPlan]::ValidateTime($target,$target,100,-500,16) -eq 'clock_change') 'click cancelled on clock rollback'
Assert-True ([BrsManualClock.ClickPlan]::ValidateTime($target,$target,100,4000,4000) -eq 'scheduler_gap') 'click cancelled after sleep'
Assert-True ([BrsManualClock.DesktopInput]::PreciseUtcNow().Offset -eq [TimeSpan]::Zero) 'native clock result has UTC offset; accuracy is not measured'
Assert-True ([BrsManualClock.ClockLogic]::NormalizeDigits(([string][char]0x06F1)+[char]0x06F2) -eq '12') 'Persian digits normalize before parsing'
function Local-Digits([string]$Text,[int]$Zero) { return -join @($Text.ToCharArray() | ForEach-Object { if ($_ -ge '0' -and $_ -le '9') { [char]($Zero + [int]$_ - 48) } else { $_ } }) }
$parsedTarget=[DateTimeOffset]::MinValue
foreach($zero in @(0x06F0,0x0660)) {
    Assert-True ([BrsManualClock.ClockLogic]::TryTarget((Local-Digits '1405/08/02' $zero),(Local-Digits '08:45:00.125' $zero),[ref]$parsedTarget) -and $parsedTarget.ToString('o') -eq '2026-10-24T08:45:00.1250000+03:30') ('full Jalali target parses digit set '+$zero)
}
$reason=[BrsManualClock.ClockWindow].GetMethod('Reason',([Reflection.BindingFlags]'NonPublic,Static')).Invoke($null,@('target_not_reached'))
Assert-True ($reason.StartsWith(([string][char]0x0632)+[char]0x0645+[char]0x0627+[char]0x0646)) 'BOM-less production C# preserves Persian text through this compiler'
Write-Output ('CLOCK_TEST_COUNT='+$script:checkCount)
