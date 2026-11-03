param([ValidateRange(1,10)][int]$Samples=5)
# Read-only diagnostics: no resync, service restart, registry write or settings change.
$ErrorActionPreference='Stop'
$workspace=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$private=Join-Path $workspace 'data\private\clock-comparison'
$zone=[TimeZoneInfo]::FindSystemTimeZoneById('Iran Standard Time')
function Tehran-Now { return [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,$zone).ToString('o') }
$folder=Join-Path $private ('reference-'+[Guid]::NewGuid().ToString('N'))
function Reject-Redirects([string]$Path){
 $current=[IO.Path]::GetFullPath($Path)
 if(-not $current.StartsWith($workspace+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Clock snapshot escapes workspace.'}
 while($true){
  try{if(([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Reparse point in clock snapshot path.'}}catch [IO.FileNotFoundException]{}catch [IO.DirectoryNotFoundException]{}
  if($current -eq $workspace){break};$current=[IO.Path]::GetDirectoryName($current)
 }
}
Reject-Redirects $folder
[IO.Directory]::CreateDirectory($folder) | Out-Null
function Query-Time([string[]]$Arguments){
 $before=Tehran-Now;$watch=[Diagnostics.Stopwatch]::StartNew()
 try{$output=(& w32tm.exe @Arguments 2>&1 | Out-String -Width 4096);$code=$LASTEXITCODE}
 catch{$output=$_.Exception.GetType().Name;$code=$null}
 $watch.Stop()
 $rows=New-Object Collections.Generic.List[object]
 foreach($line in ($output -split "`r?`n")){
  if($line -match '^\s*\d+,\s*\d+,\s*(\d+),\s*([+-]\d+\.\d+),\s*([+-]\d+\.\d+)\s*$'){
   $receive=[DateTimeOffset]::new([DateTime]::FromFileTimeUtc([long]$Matches[1]))
   $rtt=[double]::Parse($Matches[2],[Globalization.CultureInfo]::InvariantCulture)*1000
   $offset=[double]::Parse($Matches[3],[Globalization.CultureInfo]::InvariantCulture)*1000
   $rows.Add(@{received_at_tehran=[TimeZoneInfo]::ConvertTime($receive,$zone).ToString('o');roundtrip_ms=$rtt;w32tm_ntp_offset_ms=$offset})
  }
 }
 return @{arguments=$Arguments;started_at_tehran=$before;finished_at_tehran=(Tehran-Now);elapsed_ms=$watch.Elapsed.TotalMilliseconds;exit_code=$code;raw_output=$output.Trim();samples=$rows.ToArray();note='Exit code 0 alone does not prove NTP samples succeeded; inspect parsed rows and errors.'}
}
$records=@()
$records+=Query-Time @('/query','/status','/verbose')
$records+=Query-Time @('/query','/peers')
foreach($server in @('162.159.200.1','ntp.time.ir')){$records+=Query-Time @('/stripchart',('/computer:'+$server),('/samples:'+$Samples),'/period:1','/rdtsc')}
$report=@{schema_version=1;recorded_at_tehran=(Tehran-Now);timezone=[TimeZoneInfo]::Local.Id;settings_changed=$false;resync_requested=$false;order_actions=0;exchange_clock_verified=$false;queries=$records;scope='NTP network estimates and service diagnostics, not UTC or exchange accuracy guarantees.'}
$file=Join-Path $folder 'report.json'
$report | ConvertTo-Json -Depth 9 | Set-Content -LiteralPath $file -Encoding UTF8
@{script_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant();report_sha256=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant();file='report.json'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $folder 'manifest.json') -Encoding UTF8
Write-Output ([pscustomobject]@{folder=$folder;ntp_sample_counts=@($records | Where-Object { $_.arguments -contains '/rdtsc' } | ForEach-Object { @{server=$_.arguments[1];valid_rows=$_.samples.Count} });settings_changed=$false} | ConvertTo-Json -Depth 5 -Compress)
