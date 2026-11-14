$TaskName = "Moodle Session Auto Refresher"
& schtasks.exe /delete /tn $TaskName /f
if ($LASTEXITCODE -eq 0) {
    Write-Host "Scheduled Task '$TaskName' removed successfully."
} else {
    Write-Host "Scheduled Task '$TaskName' was not found or already removed."
}
