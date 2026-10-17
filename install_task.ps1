# Registers a Windows scheduled task (current user, no admin needed) that runs
# the job bot every 30 minutes while this PC is on. Run once in PowerShell.
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$action = New-ScheduledTaskAction -Execute "$dir\run.bat" -WorkingDirectory $dir
$every  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30)
$set    = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "PavaniJobBot" -Action $action -Trigger $every -Settings $set -Force | Out-Null
"Task 'PavaniJobBot' installed: runs every 30 min. Remove with: Unregister-ScheduledTask PavaniJobBot"
