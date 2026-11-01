# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
# RetroRemakes port tools: launch, online updates, report a problem.
# Usage: PortTools.ps1 -Action Launch|Check|Update|Report|Rollback [-NoUpdateCheck]
param([ValidateSet('Launch','Check','Update','Report','Rollback')][string]$Action='Launch',
      [switch]$NoUpdateCheck)
$ErrorActionPreference = 'Stop'
$Port = Split-Path -Parent $MyInvocation.MyCommand.Path
$InfoPath = Join-Path $Port 'port.json'
$Info = Get-Content $InfoPath -Raw | ConvertFrom-Json
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function VersionOf($v) { [version]($v + '') }
function Save-Info { $Info | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $InfoPath }

function Get-Manifest {
    if (-not $Info.update_url.StartsWith('https://')) { throw 'Update address must be https.' }
    $m = Invoke-RestMethod -Uri $Info.update_url -UseBasicParsing -TimeoutSec 20 -Headers @{'Cache-Control'='no-cache'}
    if ($m.schema -ne 1 -or $m.kind -ne 'retroremakes-port-update' -or $m.port_id -ne $Info.port_id) { throw 'Update information is for a different port.' }
    if (-not $m.url.StartsWith('https://')) { throw 'Update file address must be https.' }
    if ($m.size -lt 1 -or $m.size -gt 67108864) { throw 'Update size is not valid.' }
    return $m
}

function Check-Update([switch]$Quiet) {
    try { $m = Get-Manifest } catch { if (-not $Quiet) { Write-Host "Could not check for updates: $($_.Exception.Message)" }; return $null }
    if ((VersionOf $m.version) -gt (VersionOf $Info.version)) {
        Write-Host "Update available: $($Info.version) -> $($m.version)"; if ($m.notes) { Write-Host $m.notes }
        return $m
    }
    if (-not $Quiet) { Write-Host "This port is up to date ($($Info.version))." }
    return $null
}

$AllowedTop = 'runtime','recipes','patches','launcher','config','docs','update-info.json'
$AllowedExt = '.c','.cpp','.h','.hpp','.py','.json','.txt','.md','.ini','.cmake','.ps1','.bat','.yml','.yaml','.hlsl','.csv'

function Install-Update($m) {
    if ((VersionOf $Info.installer_version) -lt (VersionOf $m.min_installer)) { throw 'This port needs to be reinstalled before this update.' }
    $dl = Join-Path $Port '.port-downloads'; New-Item -ItemType Directory -Force $dl | Out-Null
    $zip = Join-Path $dl ("$($Info.port_id)-$($m.version).zip")
    Invoke-WebRequest -Uri $m.url -OutFile "$zip.part" -UseBasicParsing -TimeoutSec 600
    $len = (Get-Item "$zip.part").Length
    $sha = (Get-FileHash "$zip.part" -Algorithm SHA256).Hash.ToLower()
    if ($len -ne $m.size -or $sha -ne $m.sha256.ToLower()) { Rename-Item "$zip.part" "$zip.bad"; throw 'Download failed its size/checksum check; nothing was changed.' }
    Move-Item -Force "$zip.part" $zip
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($e in $z.Entries) {
            $n = $e.FullName
            if ($n -match '(^/|\\|:|\.\.)') { throw "Unsafe file in update: $n" }
            if ($AllowedTop -notcontains ($n.Split('/')[0])) { throw "Update tried to change $n" }
            if (-not $n.EndsWith('/') -and $AllowedExt -notcontains [IO.Path]::GetExtension($n).ToLower()) { throw "File type not allowed: $n" }
        }
    } finally { $z.Dispose() }
    $backup = Join-Path $Port (".port-backups\$($Info.version)-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Force $backup | Out-Null
    $target = Join-Path $Port 'update'
    if (Test-Path $target) { Copy-Item -Recurse $target (Join-Path $backup 'update') }
    Copy-Item $InfoPath (Join-Path $backup 'port.json')
    try {
        $stage = Join-Path $dl 'staged'; if (Test-Path $stage) { Rename-Item $stage ("staged-old-" + (Get-Date -Format 'HHmmss')) }
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $stage)
        New-Item -ItemType Directory -Force $target | Out-Null
        Copy-Item -Recurse -Force (Join-Path $stage '*') $target
        $Info.version = $m.version
        $Info | Add-Member -Force pending_rebuild ([bool]$m.requires_rebuild)
        Save-Info
    } catch { Restore-Backup $backup; throw }
    Write-Host "Updated to $($m.version)."
    if ($m.requires_rebuild) { Invoke-Rebuild $backup }
}

function Restore-Backup($backup) {
    $target = Join-Path $Port 'update'
    if (Test-Path $target) { Rename-Item $target (".update-failed-" + (Get-Date -Format 'yyyyMMdd-HHmmss')) }
    if (Test-Path (Join-Path $backup 'update')) { Copy-Item -Recurse (Join-Path $backup 'update') $target }
    Copy-Item -Force (Join-Path $backup 'port.json') $InfoPath
    $script:Info = Get-Content $InfoPath -Raw | ConvertFrom-Json
    Write-Host 'The previous version was restored.'
}

function Invoke-Rebuild($backup) {
    if (-not $Info.rebuild_command) { Write-Host 'This update needs the port to be rebuilt from your game files. Run the port installer again.'; return }
    Write-Host 'Rebuilding the port from your own game files...'
    Push-Location $Port
    try { cmd /c $Info.rebuild_command; $code = $LASTEXITCODE } finally { Pop-Location }
    if ($code -ne 0) { Write-Host "Rebuild failed (code $code)."; if ($backup) { Restore-Backup $backup } ; return }
    $Info.pending_rebuild = $false; Save-Info; Write-Host 'Rebuild finished.'
}

function Scrub([string]$text) {
    $text = $text -replace [regex]::Escape($env:USERPROFILE), '%USERPROFILE%'
    $text = $text -replace [regex]::Escape($Port), '<port>'
    $text = $text -replace '(?i)[A-Z]:\\Users\\[^\\\s"]+', '%USERPROFILE%'
    $text = $text -replace [regex]::Escape($env:COMPUTERNAME), '<pc>'
    $text = $text -replace [regex]::Escape($env:USERNAME), '<user>'
    return $text
}

function New-SupportBundle {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $out = Join-Path $Port "support\support-$($Info.port_id)-$stamp"
    New-Item -ItemType Directory -Force $out | Out-Null
    $os = Get-CimInstance Win32_OperatingSystem; $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $gpus = Get-CimInstance Win32_VideoController | ForEach-Object { @{name=$_.Name; driver=$_.DriverVersion; ram_mb=[int]($_.AdapterRAM/1MB); mode="$($_.CurrentHorizontalResolution)x$($_.CurrentVerticalResolution)@$($_.CurrentRefreshRate)"} }
    $system = @{os="$($os.Caption) $($os.Version)"; cpu=$cpu.Name; cores=$cpu.NumberOfLogicalProcessors;
                ram_gb=[math]::Round($os.TotalVisibleMemorySize/1MB,1); gpus=@($gpus)}
    $system | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 (Join-Path $out 'system.json')
    (Scrub (Get-Content $InfoPath -Raw)) | Set-Content -Encoding UTF8 (Join-Path $out 'port.json')
    foreach ($pattern in @($Info.log_files) + @('display.ini','update\update-info.json')) {
        Get-ChildItem -Path (Join-Path $Port $pattern) -File -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTime -Descending | Select-Object -First 3 | ForEach-Object {
            $text = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
            if ($text.Length -gt 4MB) { $text = $text.Substring($text.Length - 4MB) }
            (Scrub $text) | Set-Content -Encoding UTF8 (Join-Path $out ($_.Name))
          }
    }
    "Describe what happened here (what you were doing, what you saw):`r`n" | Set-Content (Join-Path $out 'WHAT-HAPPENED.txt')
    $zip = "$out.zip"; Compress-Archive -Path (Join-Path $out '*') -DestinationPath $zip -Force
    Write-Host "Support file saved: $zip"
    Write-Host 'It contains logs, settings and PC specs only; no game files.'
    Start-Process explorer.exe "/select,`"$zip`""
    $url = $Info.support_url + $(if ($Info.support_url.Contains('?')) {'&'} else {'?'}) + "version=$($Info.version)"
    if ($url.StartsWith('https://')) { Start-Process $url }
}

switch ($Action) {
    'Check'    { $m = Check-Update; if ($m) { $a = Read-Host 'Install it now? (y/n)'; if ($a -eq 'y') { Install-Update $m } } }
    'Update'   { $m = Check-Update; if ($m) { Install-Update $m } }
    'Report'   { New-SupportBundle }
    'Rollback' { $b = Get-ChildItem (Join-Path $Port '.port-backups') -Directory | Sort-Object Name -Descending | Select-Object -First 1
                 if ($b) { Restore-Backup $b.FullName; Invoke-Rebuild $null } else { Write-Host 'No earlier version saved.' } }
    'Launch'   {
        if ($Info.pending_rebuild) { Invoke-Rebuild $null }
        if ($Info.check_updates_on_start -and -not $NoUpdateCheck) {
            $m = Check-Update -Quiet
            if ($m) { $a = Read-Host "Update $($m.version) is available. Install now? (y/n)"; if ($a -eq 'y') { try { Install-Update $m } catch { Write-Host $_.Exception.Message } } }
        }
        $exe = Get-ChildItem $Port -Filter *.exe | Select-Object -First 1
        if (-not $exe) { Write-Host 'Port program not found. Run the installer.'; exit 1 }
        Start-Process -FilePath $exe.FullName -WorkingDirectory $Port
    }
}
