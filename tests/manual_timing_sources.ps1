function Resolve-TimingSources([string]$Production,[string]$Output) {
    $paths=New-Object Collections.Generic.List[string]
    foreach($name in @('manual_timing_clock.cs','manual_timing_click.cs','manual_timing_guard.cs')){
        $plain=Join-Path $Production ('scripts\'+$name)
        if([IO.File]::Exists($plain)){$paths.Add($plain);continue}
        $snapshot=$plain+'.snapshot'
        if(-not [IO.File]::Exists($snapshot)){throw ('Missing selected timing source/snapshot: '+$name)}
        # Only a disposable test-output folder receives compilable snapshots.
        $owned=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '.timing-output'))+'\'
        $stage=[IO.Path]::GetFullPath((Join-Path $Output 'baseline-sources'))
        if(-not $stage.StartsWith($owned,[StringComparison]::OrdinalIgnoreCase)){throw 'Baseline staging escaped owned test output.'}
        [IO.Directory]::CreateDirectory($stage)|Out-Null
        $target=Join-Path $stage $name
        [IO.File]::WriteAllBytes($target,[IO.File]::ReadAllBytes($snapshot));$paths.Add($target)
    }
    return $paths.ToArray()
}
