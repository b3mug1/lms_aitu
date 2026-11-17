# Installs Windows Scheduled Task using native PowerShell cmdlets to keep MoodleSession alive silently
$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path "$PSScriptRoot\..").Path
$VenvPythonw = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $VenvPythonw)) {
    $VenvPythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}

if (-not $VenvPythonw -or -not (Test-Path $VenvPythonw)) {
    Write-Error "pythonw.exe not found in $ProjectDir\.venv\Scripts or PATH"
    exit 1
}

$TaskName = "Moodle Session Auto Refresher"

# Run directly via pythonw with WorkingDirectory set (avoids cmd.exe console popups completely)
$Action = New-ScheduledTaskAction -Execute $VenvPythonw -Argument "-m app.moodle_sso_refresher --now" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force

Write-Host "Scheduled Task '$TaskName' successfully updated/registered!"
Write-Host "It will run directly via pythonw in the background without any cmd.exe popups."
