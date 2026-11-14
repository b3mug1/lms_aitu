# Installs Windows Scheduled Task using schtasks.exe to keep MoodleSession alive and automatically refresh it via Microsoft SSO
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

# Run command: cd to project dir and run pythonw -m app.moodle_sso_refresher --now
$Command = "cmd.exe /c cd /d `"$ProjectDir`" && `"$VenvPythonw`" -m app.moodle_sso_refresher --now"

# Create / replace scheduled task running every 30 minutes
& schtasks.exe /create /tn $TaskName /tr $Command /sc minute /mo 30 /f

if ($LASTEXITCODE -eq 0) {
    try {
        $TaskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Set-ScheduledTask -TaskName $TaskName -Settings $TaskSettings -ErrorAction SilentlyContinue | Out-Null
    } catch {
        # Optional battery flag
    }
    Write-Host "Scheduled Task '$TaskName' successfully registered!"
    Write-Host "It will run every 30 minutes in the background using pythonw (silent, no popup windows)."
    Write-Host "Starting first run now..."
    & schtasks.exe /run /tn $TaskName
} else {
    Write-Error "Failed to create scheduled task. Exit code: $LASTEXITCODE"
}
