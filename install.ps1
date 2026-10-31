# AK820 Studio installer: finds or installs Python, sets up the libraries, makes a desktop shortcut.
# Run through install.bat. AK820_NO_SHORTCUT=1 / AK820_NO_LAUNCH=1 skip the last two steps (for testing).
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root
$Host.UI.RawUI.WindowTitle = "Установка AK820 Studio"

function Step($text) { Write-Host "`n>> $text" -ForegroundColor Cyan }
function Fail($text) {
    Write-Host "`nОшибка: $text" -ForegroundColor Red
    Write-Host "Если не получается, напишите о проблеме на странице проекта (вкладка Issues)."
    exit 1
}

function Find-Python {
    # Python 3.9–3.13: the libraries the program uses have ready-made packages for these versions
    $check = "import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 13) else 1)"
    $candidates = "py -3.13", "py -3.12", "py -3.11", "py -3.10", "py -3.9", "python",
                  "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    foreach ($c in $candidates) {
        if ($c -match '^py (-3\.\d+)$') { $exe = "py"; $args0 = @($Matches[1]) } else { $exe = $c; $args0 = @() }
        try {
            & $exe @args0 -c $check 2>$null
            if ($LASTEXITCODE -eq 0) {
                return (& $exe @args0 -c "import sys; print(sys.executable)").Trim()
            }
        } catch {}
    }
    return $null
}

Write-Host "=== Установка AK820 Studio ===" -ForegroundColor Magenta

Step "Ищу Python"
$python = Find-Python
if (-not $python) {
    Write-Host "Python не найден, устанавливаю Python 3.11 через winget (только для вашего пользователя)..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "нет winget. Установите Python 3.11 с python.org и запустите install.bat ещё раз."
    }
    winget install -e --id Python.Python.3.11 --scope user --silent --accept-package-agreements --accept-source-agreements
    $python = Find-Python
    if (-not $python) { Fail "Python не установился. Установите Python 3.11 с python.org и запустите install.bat ещё раз." }
}
Write-Host "Python: $python"

Step "Создаю окружение программы (.venv)"
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "не удалось создать .venv" }
}

Step "Устанавливаю библиотеки (пара минут при первой установке)"
& $venvPy -m pip install --disable-pip-version-check -q --upgrade pip
& $venvPy -m pip install --disable-pip-version-check -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "не удалось установить библиотеки. Проверьте интернет и запустите install.bat ещё раз." }

Step "Проверяю, что всё работает"
& $venvPy -c "import ak820.gui, ak820.service, ak820.render; ak820.render.quote_card(0, clock=True); print('ok')"
if ($LASTEXITCODE -ne 0) { Fail "программа не запускается после установки" }

if (-not $env:AK820_NO_SHORTCUT) {
    Step "Создаю ярлык на рабочем столе"
    $desk = [Environment]::GetFolderPath("Desktop")
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut("$desk\AK820 Studio.lnk")
    $s.TargetPath = Join-Path $root ".venv\Scripts\pythonw.exe"
    $s.Arguments = "-m ak820 gui"
    $s.WorkingDirectory = $root
    $s.IconLocation = Join-Path $root "ak820\ui\icon.ico"
    $s.Description = "AK820 Studio"
    $s.Save()
}

Write-Host "`nГотово! Подключите клавиатуру USB-кабелем и запускайте ярлык «AK820 Studio» на рабочем столе." -ForegroundColor Green
if (-not $env:AK820_NO_LAUNCH) {
    Start-Process -FilePath (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList "-m", "ak820", "gui" -WorkingDirectory $root
}
