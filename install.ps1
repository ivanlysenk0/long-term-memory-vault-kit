# Встановлення скіла довготривалої пам'яті для агента (Windows, PowerShell).
# Запуск:  powershell -ExecutionPolicy Bypass -File .\install.ps1

param(
    [ValidateSet('claude','amp','gemini','all')]
    [string]$Agent
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Src  = Join-Path $Here 'skill'
$Name = 'ltm-vault'

# Кирилиця в консолі Windows без цього виводиться кракозябрами.
try { chcp 65001 > $null } catch {}
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Fail($msg) { Write-Host "ПОМИЛКА: $msg" -ForegroundColor Red; exit 1 }

# --- Python ------------------------------------------------------------------
$Py = $null
foreach ($c in @('python','python3','py')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            & $c -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { $Py = $c; break }
        } catch {}
    }
}
if (-not $Py) {
    Fail "потрібен Python 3.8 або новіший.
  Встанови з https://www.python.org/downloads/
  або командою: winget install Python.Python.3.12
  Під час встановлення обов'язково постав галочку 'Add Python to PATH'."
}

Write-Host "Python: $(& $Py --version 2>&1)"
if (-not (Test-Path $Src)) { Fail "не знайдено каталог skill\ поруч зі скриптом" }

# --- куди ставити ------------------------------------------------------------
$Targets = @()
function Add-Target($label, $path) {
    $script:Targets += [pscustomobject]@{ Label = $label; Path = $path }
}

$ClaudeDir = Join-Path $env:USERPROFILE '.claude\skills'
$AmpDir    = Join-Path $env:APPDATA    'amp\skills'
$GeminiDir = Join-Path $env:USERPROFILE '.gemini\skills'

switch ($Agent) {
    'claude' { Add-Target 'Claude Code' $ClaudeDir }
    'amp'    { Add-Target 'AMP Code'    $AmpDir }
    'gemini' { Add-Target 'Gemini CLI'  $GeminiDir }
    'all'    {
        Add-Target 'Claude Code' $ClaudeDir
        Add-Target 'AMP Code'    $AmpDir
        Add-Target 'Gemini CLI'  $GeminiDir
    }
    default {
        if (Test-Path (Join-Path $env:USERPROFILE '.claude')) { Add-Target 'Claude Code' $ClaudeDir }
        if (Test-Path (Join-Path $env:APPDATA 'amp'))         { Add-Target 'AMP Code'    $AmpDir }
        if (Test-Path (Join-Path $env:USERPROFILE '.gemini')) { Add-Target 'Gemini CLI'  $GeminiDir }
    }
}

if ($Targets.Count -eq 0) {
    Write-Host "Не знайдено жодного встановленого агента."
    Write-Host "Постав примусово: .\install.ps1 -Agent all"
    exit 1
}

# --- установка ---------------------------------------------------------------
foreach ($t in $Targets) {
    $dest = Join-Path $t.Path $Name
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item -Path (Join-Path $Src '*') -Destination $dest -Recurse -Force
    # __pycache__ лишається від запусків у репозиторії і їде до користувача.
    Get-ChildItem -Path $dest -Filter '__pycache__' -Recurse -Directory -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "встановлено: $($t.Label) -> $dest"
}

# --- перевірка ---------------------------------------------------------------
$first = Join-Path $Targets[0].Path $Name
$need = @('SKILL.md','scripts\ltm_detect.py','scripts\ltm_init.py',
          'scripts\ltm_doctor.py','scripts\ltm_schedule.py','scripts\ltm_seed.py',
          'scripts\ltm_uninstall.py','scripts\ltm_version.py','scripts\ltm_blocks.py')
$missing = $need | Where-Object { -not (Test-Path (Join-Path $first $_)) }
if ($missing) { Fail "не вистачає файлів: $($missing -join ', ')" }
Write-Host "перевірка: усі файли на місці"

Write-Host ""
Write-Host "Готово. Далі:"
Write-Host "  1. Перезапусти агента, щоб він побачив новий скіл."
Write-Host "  2. Попроси його: «розгорни довготривалу пам'ять»."
Write-Host "  3. Або запусти розвідку вручну:"
Write-Host "     $Py `"$first\scripts\ltm_detect.py`""
