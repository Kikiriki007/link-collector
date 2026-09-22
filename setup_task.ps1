# One-time setup: registers a daily Windows Task Scheduler entry that runs run_collect.bat.
# Own, separately-named task ("link-collector-daily") - does not touch or replace any other
# scheduled task on this machine.
#
# Usage (from a normal PowerShell prompt, in this folder):
#   .\setup_task.ps1
#   .\setup_task.ps1 -Time "22:30"      # pick a different daily run time (default 05:00)

param(
    [string]$Time = "05:00"
)

$TaskName = "link-collector-daily"
$ScriptDir = $PSScriptRoot
$BatPath = Join-Path $ScriptDir "run_collect.bat"

if (-not (Test-Path $BatPath)) {
    Write-Error "run_collect.bat not found next to this script ($BatPath)."
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Task '$TaskName' already exists - unregistering it first so this run replaces it cleanly."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $ScriptDir
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily Telegram link -> download -> transcribe collector (link-collector project)."

Write-Host "Registered '$TaskName', daily at $Time."
Write-Host "Test it right now with:  Start-ScheduledTask -TaskName '$TaskName'"
