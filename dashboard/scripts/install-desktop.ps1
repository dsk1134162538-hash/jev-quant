param(
    [switch]$NoBuild
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DistIndex = Join-Path $ProjectRoot 'dist\index.html'
$Launcher = Join-Path $ProjectRoot 'start-dashboard.bat'

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
if (-not (Test-Path $Launcher)) {
    throw "Dashboard launcher not found: $Launcher"
}

$Hengce = ([char[]](0x8861, 0x7B56)) -join ''
$ResearchLab = ([char[]](0x91CF, 0x5316, 0x7814, 0x7A76, 0x5BA4)) -join ''
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop "$Hengce AI $ResearchLab.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Launcher
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.IconLocation = Join-Path $ProjectRoot 'desktop\icon.ico'
$Shortcut.Description = "$Hengce AI $ResearchLab - local readonly dashboard"
$Shortcut.Save()

Write-Output $ShortcutPath
