# Registers (or updates) the "AIDaily" scheduled task for the current user.
#   .\install-task.ps1                 # every day at 07:30
#   .\install-task.ps1 -At 06:00
#   .\install-task.ps1 -Uninstall
param(
    [string]$At = "07:30",
    [string]$TaskName = "AIDaily",
    [switch]$Uninstall
)

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed scheduled task $TaskName"
    return
}

$runner = Join-Path $PSScriptRoot "run.cmd"
$python = (Get-Command python -ErrorAction Stop).Source

$action = New-ScheduledTaskAction -Execute $runner -Argument "`"$python`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# StartWhenAvailable: if the PC was off or asleep at the scheduled time, run as soon as it's back.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Collects AI/LLM/agent news from many platforms and renders site\index.html" -Force | Out-Null

$next = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
Write-Output "Registered $TaskName (daily at $At, python: $python). Next run: $next"
