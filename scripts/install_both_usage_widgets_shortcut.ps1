[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CodexRepository,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ClaudeRepository
)

$ErrorActionPreference = 'Stop'
$codexRoot = (Resolve-Path -LiteralPath $CodexRepository -ErrorAction Stop).Path
$claudeRoot = (Resolve-Path -LiteralPath $ClaudeRepository -ErrorAction Stop).Path
$scriptPath = Join-Path $PSScriptRoot 'start_both_usage_rings.ps1'
$iconPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\usage-widgets.ico'

foreach ($requiredPath in @($scriptPath, $iconPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required paired-launcher file is missing: $requiredPath"
    }
}

foreach ($widget in @(
    [pscustomobject]@{ Name = 'Codex'; Root = $codexRoot }
    [pscustomobject]@{ Name = 'Claude Code'; Root = $claudeRoot }
)) {
    $pythonw = Join-Path $widget.Root '.venv\Scripts\pythonw.exe'
    $entryPoint = Join-Path $widget.Root 'scripts\start_with_update.py'
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
        throw "Missing $($widget.Name) Python environment: $pythonw. Install the widget first."
    }
    if (-not (Test-Path -LiteralPath $entryPoint -PathType Leaf)) {
        throw "Missing $($widget.Name) startup script: $entryPoint. Check the repository path."
    }
}

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Start Codex and Claude Usage Widgets.lnk'
$powershellPath = Join-Path $PSHOME 'powershell.exe'
$arguments = @(
    '-NoProfile'
    '-WindowStyle Hidden'
    '-File'
    ('"{0}"' -f $scriptPath)
    '-CodexRepository'
    ('"{0}"' -f $codexRoot)
    '-ClaudeRepository'
    ('"{0}"' -f $claudeRoot)
) -join ' '

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershellPath
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = Split-Path -Parent $PSScriptRoot
$shortcut.IconLocation = "$iconPath,0"
$shortcut.Description = 'Start Codex and Claude usage widgets that are not already running.'
$shortcut.Save()

Write-Output "Created desktop shortcut: $shortcutPath"
