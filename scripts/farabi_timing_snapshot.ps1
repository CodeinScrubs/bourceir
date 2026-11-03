[CmdletBinding()]
param(
    [ValidateSet('preflight','postflight','rehearsal')][string]$Phase = 'preflight',
    [string]$SessionDir = ''
)
# Read-only workstation snapshot. No network probe, resync, routing edit, or broker request.
$ErrorActionPreference = 'Stop'
$timingWorkspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$timingRoot = Join-Path $timingWorkspace 'data\private\farabi-timing'
$timingZone = [TimeZoneInfo]::FindSystemTimeZoneById('Iran Standard Time')
$timingStartedUtc = [DateTimeOffset]::UtcNow
$timingWatch = [Diagnostics.Stopwatch]::StartNew()
$timingNow = [TimeZoneInfo]::ConvertTime($timingStartedUtc, $timingZone)
if ([string]::IsNullOrWhiteSpace($SessionDir)) {
    $SessionDir = Join-Path $timingRoot ('system-' + $timingNow.ToString('yyyyMMdd-HHmmss-fff'))
}
$timingDestination = [IO.Path]::GetFullPath($SessionDir)
if (-not $timingDestination.StartsWith($timingRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Snapshot must be inside data/private/farabi-timing.'
}
[void](New-Item -ItemType Directory -Path $timingDestination -Force)
$timingErrors = [Collections.Generic.List[string]]::new()
function Read-TimingCommand {
    param([string[]]$ArgumentList)
    try {
        $timingOutput = & w32tm.exe @ArgumentList 2>&1 | Out-String -Width 4096
        $timingExit = $LASTEXITCODE
        if ($timingExit -ne 0) { $timingErrors.Add('w32tm ' + ($ArgumentList -join ' ') + ' exited ' + $timingExit) }
        return [ordered]@{arguments=$ArgumentList;exit_code=$timingExit;output=$timingOutput.Trim()}
    } catch {
        $timingErrors.Add('w32tm query failed')
        return [ordered]@{arguments=$ArgumentList;exit_code=$null;error='query_failed'}
    }
}
$timingStatus = Read-TimingCommand @('/query','/status','/verbose')
$timingPeers = Read-TimingCommand @('/query','/peers')
$timingConfig = Read-TimingCommand @('/query','/configuration')
$timingRegistry = [ordered]@{}
foreach ($timingSection in @('Parameters','Config','TimeProviders\NtpClient')) {
    $timingValues = [ordered]@{}
    try {
        $timingKey = Get-ItemProperty -LiteralPath ('HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\' + $timingSection) -ErrorAction Stop
        foreach ($timingName in @('Type','NtpServer','MinPollInterval','MaxPollInterval','UpdateInterval','MaxAllowedPhaseOffset','Enabled','SpecialPollInterval')) {
            if ($null -ne $timingKey.PSObject.Properties[$timingName]) { $timingValues[$timingName] = $timingKey.$timingName }
        }
        $timingRegistry[$timingSection] = $timingValues
    } catch {
        $timingErrors.Add('Windows Time registry query unavailable: ' + $timingSection)
        $timingRegistry[$timingSection] = [ordered]@{query_error='unavailable'}
    }
}
$timingProxifier = [ordered]@{active_profile=$null;profile_sha256=$null;configured_farabi_rules=@();actual_connection_rule_observed=$false}
try {
    $timingActive = (Get-ItemProperty -LiteralPath 'HKCU:\Software\Initex\Proxifier\Settings').ActiveProfile4
    $timingProfilePath = Join-Path $env:APPDATA ('Proxifier4\Profiles\' + $timingActive + '.ppx')
    if (-not (Test-Path -LiteralPath $timingProfilePath)) { $timingProfilePath = [string]$timingActive }
    if (Test-Path -LiteralPath $timingProfilePath) {
        $timingProxifier.active_profile = [IO.Path]::GetFileNameWithoutExtension($timingProfilePath)
        $timingProxifier.profile_sha256 = (Get-FileHash -LiteralPath $timingProfilePath -Algorithm SHA256).Hash
        [xml]$timingXml = Get-Content -LiteralPath $timingProfilePath -Raw
        $timingIndex = 0
        $timingRules = foreach ($timingRule in $timingXml.ProxifierProfile.RuleList.Rule) {
            $timingIndex++
            if ([string]$timingRule.Targets -match 'farabi|farabixo') {
                [ordered]@{position=$timingIndex;enabled=[string]$timingRule.enabled;name=[string]$timingRule.Name;
                    applications=[string]$timingRule.Applications;targets=[string]$timingRule.Targets;ports=[string]$timingRule.Ports;
                    action_type=[string]$timingRule.Action.type}
            }
        }
        $timingProxifier.configured_farabi_rules = @($timingRules)
    }
} catch { $timingErrors.Add('Proxifier profile snapshot unavailable') }
$timingEvents = @()
try {
    $timingEvents = @(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Time-Service';
        StartTime=[DateTime]::Now.AddHours(-2);EndTime=[DateTime]::Now} -MaxEvents 30 -ErrorAction Stop |
        Select-Object @{n='time_utc';e={$_.TimeCreated.ToUniversalTime().ToString('o')}},Id,LevelDisplayName)
} catch {
    if ($_.FullyQualifiedErrorId -notmatch 'NoMatchingEventsFound') { $timingErrors.Add('Time service event query unavailable') }
}
$timingAdapters = @()
$timingRoutes = @()
try { $timingAdapters = @(Get-NetAdapter | Select-Object Name,Status,InterfaceDescription,LinkSpeed,ifIndex) }
catch { $timingErrors.Add('Adapter query unavailable') }
try { $timingRoutes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
    Select-Object InterfaceAlias,NextHop,RouteMetric,InterfaceMetric,State) }
catch { $timingErrors.Add('Route query unavailable') }
$timingOs = $null
try { $timingOs = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop }
catch { $timingErrors.Add('Operating system query unavailable') }
$timingServiceStatus = 'UNKNOWN'
try { $timingServiceStatus = [string](Get-Service W32Time -ErrorAction Stop).Status }
catch { $timingErrors.Add('Windows Time service state unavailable') }
$timingChrome = $null
try { $timingChrome = (Get-ItemProperty -LiteralPath 'HKCU:\Software\Google\Chrome\BLBeacon').version } catch {}
$timingFinishedUtc = [DateTimeOffset]::UtcNow
$timingWatch.Stop()
$timingResult = [ordered]@{
    schema_version='1.0.0';phase=$Phase;recorded_at_tehran=$timingNow.ToString('o');recorded_at_utc=$timingStartedUtc.ToString('o');
    finished_at_utc=$timingFinishedUtc.ToString('o');collection_elapsed_ms=$timingWatch.Elapsed.TotalMilliseconds;
    timezone=[TimeZoneInfo]::Local.Id;os=[ordered]@{caption=$timingOs.Caption;version=$timingOs.Version;build=$timingOs.BuildNumber};
    installed_chrome_registry_version=$timingChrome;active_browser_version=$null;
    active_browser_version_semantics='not_observable_from_workstation_snapshot; inspect selected browser UI';
    time_service_status=$timingServiceStatus;status=$timingStatus;peers=$timingPeers;configuration=$timingConfig;
    windows_time_registry=$timingRegistry;proxifier=$timingProxifier;recent_bounded_time_events=$timingEvents;
    network_adapters=$timingAdapters;default_routes=$timingRoutes;errors=@($timingErrors);
    settings_changed=$false;network_probes_sent=0;broker_requests_sent=0;
    note='Configured rules and route table do not establish the matched rule of a live connection.'
}
$timingFile = Join-Path $timingDestination ($Phase + '-' + $timingNow.ToString('yyyyMMdd-HHmmss-fff') + '.json')
$timingResult | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $timingFile -Encoding UTF8
$timingHash = (Get-FileHash -LiteralPath $timingFile -Algorithm SHA256).Hash
Set-Content -LiteralPath ($timingFile + '.sha256.txt') -Value ($timingHash + '  ' + [IO.Path]::GetFileName($timingFile)) -Encoding ASCII
[pscustomobject]@{saved=$timingFile;w32tm_status_exit_code=$timingStatus.exit_code;diagnostic_errors=$timingErrors.Count;settings_changed=$false} | ConvertTo-Json -Compress
