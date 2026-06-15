# setup_scheduler.ps1
# --------------------
# Registers a Windows Task Scheduler job that runs the automation
# once a month on the day and time configured in config.yaml.
#
# Run this script ONCE as Administrator:
#   Right-click PowerShell → "Run as Administrator"
#   cd C:\path\to\tableau_automation
#   .\setup_scheduler.ps1

$ProjectDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe   = "$ProjectDir\venv\Scripts\python.exe"
$ScriptPath  = "$ProjectDir\main.py"
$TaskName    = "CockpitToSharePoint_Monthly"

$ConfigPath  = "$ProjectDir\config.yaml"
$ConfigRaw   = Get-Content $ConfigPath -Raw

$RunDay  = [regex]::Match($ConfigRaw, 'run_day:\s*(\d+)').Groups[1].Value
$RunTime = [regex]::Match($ConfigRaw, "run_time:\s*[`"']?(\d{2}:\d{2})[`"']?").Groups[1].Value

if (-not $RunDay)  { $RunDay  = "1"     }
if (-not $RunTime) { $RunTime = "06:00" }

Write-Host ""
Write-Host "==================================================="
Write-Host "  Cockpit → SharePoint Monthly Task Scheduler"
Write-Host "==================================================="
Write-Host "  Project dir : $ProjectDir"
Write-Host "  Python      : $PythonExe"
Write-Host "  Script      : $ScriptPath"
Write-Host "  Schedule    : Day $RunDay of each month at $RunTime"
Write-Host "==================================================="
Write-Host ""

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python executable not found at: $PythonExe`nRun 'python -m venv venv' and 'venv\Scripts\pip install -r requirements.txt' first."
    exit 1
}

# Build a proper monthly trigger using the Schedule.Service COM object
$TaskService = New-Object -ComObject "Schedule.Service"
$TaskService.Connect()
$RootFolder  = $TaskService.GetFolder("\")

$TaskDef     = $TaskService.NewTask(0)
$TaskDef.Settings.Enabled                   = $true
$TaskDef.Settings.RunOnlyIfNetworkAvailable = $true
$TaskDef.Settings.StartWhenAvailable        = $true   # run ASAP if machine was off

# Monthly trigger
$Triggers    = $TaskDef.Triggers
$MonthTrig   = $Triggers.Create(4)   # 4 = TASK_TRIGGER_MONTHLY
$MonthTrig.DaysOfMonth   = [Math]::Pow(2, [int]$RunDay - 1)   # bitmask
$MonthTrig.MonthsOfYear  = 4095       # all 12 months
$TimeParts   = $RunTime -split ":"
$MonthTrig.StartBoundary = (Get-Date -Hour $TimeParts[0] -Minute $TimeParts[1] -Second 0).ToString("yyyy-MM-ddTHH:mm:ss")
$MonthTrig.Enabled       = $true

# Action
$Action      = $TaskDef.Actions.Create(0)   # 0 = TASK_ACTION_EXEC
$Action.Path = $PythonExe
$Action.Arguments        = "`"$ScriptPath`""
$Action.WorkingDirectory = $ProjectDir

# Run elevated
$TaskDef.Principal.RunLevel = 1   # TASK_RUNLEVEL_HIGHEST

try {
    $RootFolder.RegisterTaskDefinition(
        $TaskName,
        $TaskDef,
        6,      # TASK_CREATE_OR_UPDATE
        $null,  # current user
        $null,
        3       # TASK_LOGON_INTERACTIVE_TOKEN
    ) | Out-Null

    Write-Host "Task '$TaskName' registered successfully!" -ForegroundColor Green
    Write-Host ""
    Write-Host "View/edit it in: Task Scheduler Library → $TaskName"
} catch {
    Write-Error "Failed to register task: $_"
    exit 1
}

Write-Host ""
Write-Host "To run the task immediately for testing:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""