# Register Windows Task Scheduler job for KGI City-Hall Tracker
# Run from an elevated PowerShell:  PowerShell -ExecutionPolicy Bypass -File install_schedule.ps1
$ErrorActionPreference = "Stop"

$TaskName = "KgiCityhallTracker_Daily"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bat  = Join-Path $Root "run_daily.bat"

if (-not (Test-Path $Bat)) {
    throw "run_daily.bat not found at $Bat"
}

# Remove old task if present
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action  = New-ScheduledTaskAction -Execute $Bat -Argument "--no-browser"
$trigger = New-ScheduledTaskTrigger -Daily -At 3:30pm
$trigger.DaysOfWeek = @()  # Daily; we filter trading days at run time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Fetch TWSE top-100 + BSR, filter 凱基市府, update SQLite + dashboard"

Write-Host ""
Write-Host "Registered task '$TaskName'. Test with:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove with:"
Write-Host "  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
