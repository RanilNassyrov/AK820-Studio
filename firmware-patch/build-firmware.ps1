# Builds our firmware in the official QMK Docker container. Touches nothing on the keyboard.
# cmd /c for the docker checks: Windows PowerShell treats anything on a program's stderr as an error.
$fw = $PSScriptRoot

function Docker-Ready {
    cmd /c "docker info >nul 2>&1"
    return $LASTEXITCODE -eq 0
}

if (-not (Docker-Ready)) {
    Write-Host "Starting Docker Desktop, this can take a few minutes..." -ForegroundColor Cyan
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    $deadline = (Get-Date).AddMinutes(6)
    while (-not (Docker-Ready) -and (Get-Date) -lt $deadline) { Start-Sleep 5 }
    if (-not (Docker-Ready)) {
        Write-Host "Docker did not start. Open Docker Desktop, accept its window if it shows one, and run this again." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Docker is ready. Building, the first time takes 10-20 minutes..." -ForegroundColor Cyan
Remove-Item "$fw\ak820pro-hostcolors-fpb.bin" -ErrorAction SilentlyContinue
cmd /c "docker run --rm -v ${fw}:/out ghcr.io/qmk/qmk_cli bash /out/build-in-docker.sh 2>&1"
if (Test-Path "$fw\ak820pro-hostcolors-fpb.bin") {
    Write-Host "`nDone. Tell Claude the firmware build finished." -ForegroundColor Green
} else {
    Write-Host "`nThe build failed. Tell Claude, it can read this window." -ForegroundColor Red
    exit 1
}
