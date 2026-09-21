[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CodexRepository,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ClaudeRepository
)

$ErrorActionPreference = 'Stop'

function Resolve-WidgetRepository {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Mutex
    )

    $root = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
    $entryPoint = Join-Path $root 'scripts\start_with_update.py'
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
        throw "Missing $Name Python environment: $pythonw. Install the widget first."
    }
    if (-not (Test-Path -LiteralPath $entryPoint -PathType Leaf)) {
        throw "Missing $Name startup script: $entryPoint. Check the repository path."
    }

    [pscustomobject]@{
        Name = $Name
        Root = $root
        Pythonw = $pythonw
        Mutex = $Mutex
    }
}

function Test-WidgetSupervisorRunning {
    param([Parameter(Mandatory = $true)][string]$Name)

    $handle = $null
    try {
        $handle = [System.Threading.Mutex]::OpenExisting($Name)
        return $true
    }
    catch [System.Threading.WaitHandleCannotBeOpenedException] {
        return $false
    }
    finally {
        if ($null -ne $handle) {
            $handle.Dispose()
        }
    }
}

$widgets = @(
    (Resolve-WidgetRepository -Path $CodexRepository -Name 'Codex Usage Rings' -Mutex 'Local\CodexUsageRingsSupervisor')
    (Resolve-WidgetRepository -Path $ClaudeRepository -Name 'Claude Code Usage Rings' -Mutex 'Local\ClaudeCodeUsageRingsSupervisor')
)

foreach ($widget in $widgets) {
    if (Test-WidgetSupervisorRunning -Name $widget.Mutex) {
        Write-Output "$($widget.Name) is already running; leaving it alone."
        continue
    }

    if ($PSCmdlet.ShouldProcess($widget.Name, 'Start usage widget')) {
        Start-Process `
            -FilePath $widget.Pythonw `
            -ArgumentList 'scripts\start_with_update.py' `
            -WorkingDirectory $widget.Root `
            -WindowStyle Hidden
        Write-Output "Started $($widget.Name)."
    }
}
