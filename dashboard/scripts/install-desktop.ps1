param(
    [switch]$NoBuild
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DistIndex = Join-Path $ProjectRoot 'dist\desktop.html'

if (-not $NoBuild) {
    $Npm = Get-Command npm.cmd -ErrorAction Stop
    Push-Location $ProjectRoot
    try {
        if (-not (Test-Path (Join-Path $ProjectRoot 'node_modules'))) {
            & $Npm.Source install --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw 'npm install failed' }
        }
        & $Npm.Source run build
        if ($LASTEXITCODE -ne 0) { throw 'Dashboard build failed' }
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path $DistIndex)) {
    throw "Dashboard file not found: $DistIndex"
}

$EdgeCandidates = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    "$env:LocalAppData\Microsoft\Edge\Application\msedge.exe"
)
$Edge = $EdgeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $Edge) {
    throw 'Microsoft Edge was not found'
}

$Hengce = ([char[]](0x8861, 0x7B56)) -join ''
$ResearchLab = ([char[]](0x91CF, 0x5316, 0x7814, 0x7A76, 0x5BA4)) -join ''
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop "$Hengce AI $ResearchLab.lnk"
$AppUrl = ([System.Uri]$DistIndex).AbsoluteUri
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Edge
$Shortcut.Arguments = "--app=`"$AppUrl`""
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.IconLocation = Join-Path $ProjectRoot 'desktop\icon.ico'
$Shortcut.Description = "$Hengce AI $ResearchLab - local demo dashboard"
$Shortcut.Save()

Write-Output $ShortcutPath
