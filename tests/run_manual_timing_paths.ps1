$ErrorActionPreference='Stop'
$root=Join-Path $PSScriptRoot ('.timing-output\paths-'+[Guid]::NewGuid().ToString('N').Substring(0,8))
[IO.Directory]::CreateDirectory($root) | Out-Null
$checks=New-Object Collections.Generic.List[object]
$runError=$null
function Check([bool]$Value,[string]$Name){$checks.Add(@{name=$Name;passed=$Value});if(-not $Value){throw ('FAIL: '+$Name)};Write-Output ('PASS: '+$Name)}
function Workspace([string]$Name){
    $folder=Join-Path $root $Name
    [IO.Directory]::CreateDirectory((Join-Path $folder 'scripts')) | Out-Null
    foreach($name in @('manual_timing_clock.cs','manual_timing_click.cs','manual_timing_guard.cs','start_manual_timing_clock.ps1')){[IO.File]::Copy((Join-Path $PSScriptRoot ('..\scripts\'+$name)),(Join-Path $folder ('scripts\'+$name)))}
    return $folder
}
function Build([string]$Workspace){
    $id=[Guid]::NewGuid().ToString('N');$stdout=Join-Path $root ($id+'.stdout');$stderr=Join-Path $root ($id+'.stderr')
    $builder=Join-Path $Workspace 'scripts\start_manual_timing_clock.ps1'
    $p=Start-Process powershell.exe -ArgumentList @('-NoProfile','-File',('"'+$builder+'"'),'-NoLaunch') -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $null=$p.Handle
    if(-not $p.WaitForExit(30000)){try{$p.Kill()}catch{};throw 'Owned path-test build timed out.'}
    return @{code=$p.ExitCode;output=[IO.File]::ReadAllText($stdout).Trim();error=[IO.File]::ReadAllText($stderr)}
}
function Junction([string]$Path,[string]$Target){
    [IO.Directory]::CreateDirectory((Split-Path $Path -Parent)) | Out-Null
    [IO.Directory]::CreateDirectory($Target) | Out-Null
    New-Item -ItemType Junction -Path $Path -Target $Target | Out-Null
}
try{
    $launcher=Join-Path $PSScriptRoot '..\scripts\start_manual_timing_clock.ps1'
    $parseTokens=$null;$parseErrors=$null;$ast=[Management.Automation.Language.Parser]::ParseFile($launcher,[ref]$parseTokens,[ref]$parseErrors)
    if($parseErrors.Count -ne 0){throw 'Launcher parse failed.'}
    $definition=@($ast.FindAll({param($node)$node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Safe-Path'},$true))
    if($definition.Count -ne 1){throw 'Expected one Safe-Path implementation.'}
    $drive=[IO.Path]::GetPathRoot([IO.Path]::GetFullPath($root));$probe=Join-Path $drive ('TimingClock-readonly-'+[Guid]::NewGuid().ToString('N'))
    $resolved=& {param($workRoot,$candidate,$body). $body;Safe-Path $candidate} $drive $probe ([scriptblock]::Create($definition[0].Extent.Text))
    Check ($resolved -eq [IO.Path]::GetFullPath($probe)) 'PowerShell drive-root containment stays rooted'
    Check (-not [IO.File]::Exists($probe) -and -not [IO.Directory]::Exists($probe)) 'drive-root check creates no files outside its fixture'
    foreach($part in @('runtime','private')){
        $workspace=Workspace $part;$outside=Join-Path $root ('outside-'+$part)
        Junction (Join-Path $workspace ('data\'+$part+'\manual-clock')) $outside
        $result=Build $workspace
        Check ($result.code -ne 0 -and $result.error -like '*Unsafe reparse point*') ($part+' root junction is rejected')
        Check (@(Get-ChildItem -LiteralPath $outside -Recurse -Force).Count -eq 0) ($part+' junction receives no generated files')
    }
    $workspace=Workspace 'artifact';$first=Build $workspace
    Check ($first.code -eq 0 -and [IO.File]::Exists($first.output)) 'path fixture baseline builds normally'
    $folder=Split-Path $first.output -Parent;$sourceFolder=Join-Path $folder 'sources'
    # Both absolute rename targets are resolved and verified inside this owned fixture.
    $original=$sourceFolder+'.original'
    foreach($path in @($sourceFolder,$original)){if(-not ([IO.Path]::GetFullPath($path)).StartsWith(([IO.Path]::GetFullPath($workspace)+'\'),[StringComparison]::OrdinalIgnoreCase)){throw 'Fixture rename escaped workspace.'}}
    [IO.Directory]::Move($sourceFolder,$original)
    $outside=Join-Path $root 'outside-artifact';Junction $sourceFolder $outside
    $result=Build $workspace
    Check ($result.code -ne 0 -and $result.error -like '*Unsafe reparse point*') 'cached artifact intermediate junction is rejected'
    Check (@(Get-ChildItem -LiteralPath $outside -Recurse -Force).Count -eq 0) 'artifact junction receives no writes'
    $sources=@('manual_timing_clock.cs','manual_timing_click.cs','manual_timing_guard.cs') | ForEach-Object {Join-Path $PSScriptRoot ('..\scripts\'+$_)}
    Add-Type -Path $sources -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll')
    $workspace=Workspace 'journal';$outside=Join-Path $root 'outside-journal';Junction (Join-Path $workspace 'data\private') $outside
    $rejected=$false;try{$journal=New-Object BrsManualClock.TimingJournal($workspace);$journal.Dispose()}catch{$rejected=$_.Exception.ToString() -like '*Reparse points*'}
    Check $rejected 'direct C# journal rejects an intermediate private junction'
    Check (@(Get-ChildItem -LiteralPath $outside -Recurse -Force).Count -eq 0) 'direct journal creates no files through the junction'
    $rejected=$false;try{[BrsManualClock.TimingSafety]::RequireSafePath((Join-Path $root 'journal-sibling\outside.json'),$workspace) | Out-Null}catch{$rejected=$true}
    Check $rejected 'lexically similar sibling path is rejected'
}catch{$runError=$_.Exception.GetType().Name;throw}finally{
    $failed=@($checks|Where-Object{-not $_.passed}).Count
    $version=[regex]::Match([IO.File]::ReadAllText((Join-Path $PSScriptRoot '..\scripts\manual_timing_guard.cs')),'const string Version\s*=\s*"([^"]+)"').Groups[1].Value
    @{scope='owned disposable junction fixtures; builds and journal only; no desktop input';version=$version;total=$checks.Count;failed=$failed;passed=($null -eq $runError -and $failed -eq 0);run_error=$runError;checks=$checks;recorded_at_tehran=[TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,[TimeZoneInfo]::FindSystemTimeZoneById('Iran Standard Time')).ToString('o');sources=@(@(@('manual_timing_clock.cs','manual_timing_click.cs','manual_timing_guard.cs','start_manual_timing_clock.ps1')|ForEach-Object{Join-Path $PSScriptRoot ('..\scripts\'+$_)})+$PSCommandPath|ForEach-Object{@{name=[IO.Path]::GetFileName($_);sha256=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()}})} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'report.json') -Encoding UTF8
    Write-Output ('Path report: '+$root)
}
