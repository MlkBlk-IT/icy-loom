<#
.SYNOPSIS
    Installe IcyLoom (v2) sur Windows : environnement Python, paquet,
    configuration, icône de zone de notification, démarrage automatique et
    branchement du client MCP.

.DESCRIPTION
    Tout se fait au niveau utilisateur (%LOCALAPPDATA%\icyloom) : aucun droit
    administrateur n'est demandé. Le script est idempotent — le relancer
    réinstalle par-dessus sans casser l'existant.

.PARAMETER Source
    Origine du paquet : URL git, URL de wheel, chemin de wheel ou dossier
    local. Défaut : la branche main du dépôt.

.PARAMETER InstallDir
    Dossier d'installation (environnement virtuel + configuration).

.PARAMETER Clients
    Clients MCP à configurer automatiquement : cursor, claude-code, codex,
    windsurf. Passer @() pour n'en configurer aucun.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1 `
        -Clients cursor,codex -Source "git+https://github.com/MlkBlk-IT/icy-loom.git@v2.0.0"
#>
[CmdletBinding()]
param(
    [string]$Source = "git+https://github.com/MlkBlk-IT/icy-loom.git@main",
    [string]$InstallDir = "$env:LOCALAPPDATA\icyloom",
    [string[]]$Clients = @("cursor"),
    [switch]$NoAutostart,
    [switch]$NoClient
)

$ErrorActionPreference = "Stop"

function Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Ok([string]$Message) {
    Write-Host "    [ok] $Message" -ForegroundColor Green
}

function Fail([string]$Message) {
    Write-Host "    [!!] $Message" -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
Step "Recherche de Python 3.11+"

$python = $null
foreach ($candidate in @("py", "python")) {
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    if ($candidate -eq "py") {
        $probe = & $command.Source -3 -c "import sys; print(sys.version_info[:2])" 2>$null
    }
    else {
        $probe = & $command.Source -c "import sys; print(sys.version_info[:2])" 2>$null
    }
    if ($LASTEXITCODE -eq 0 -and $probe) {
        $parts = ($probe -split ",")
        if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 11) {
            $python = $command.Source
            if ($candidate -eq "py") { $pythonArgs = @("-3") } else { $pythonArgs = @() }
            break
        }
    }
}
if (-not $python) {
    Fail "Python 3.11+ est requis. Installe-le depuis https://www.python.org/downloads/"
    exit 1
}
Ok "Python : $python"

# ---------------------------------------------------------------------------
# 2. Environnement virtuel
# ---------------------------------------------------------------------------
Step "Environnement virtuel dans $InstallDir"

$venvDir = Join-Path $InstallDir "venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    & $python @pythonArgs -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { Fail "création du venv impossible"; exit 1 }
    Ok "venv créé"
}
else {
    Ok "venv déjà présent"
}

$icyloom = Join-Path $venvDir "Scripts\icyloom.exe"

# ---------------------------------------------------------------------------
# 3. Paquet
# ---------------------------------------------------------------------------
Step "Installation du paquet IcyLoom (source : $Source)"

$requirement = if ($Source -match "^(git\+|https?://)") {
    "icyloom[tray] @ $Source"
}
elseif ($Source -like "*.whl") {
    $full = (Resolve-Path $Source).Path
    "icyloom[tray] @ file:///$($full -replace '\\','/')"
}
else {
    $full = (Resolve-Path $Source).Path
    "$full[tray]"
}

& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install $requirement --quiet
if ($LASTEXITCODE -ne 0) { Fail "installation du paquet impossible"; exit 1 }
Ok "IcyLoom installé (avec le system tray)"

# ---------------------------------------------------------------------------
# 4. Configuration
# ---------------------------------------------------------------------------
Step "Configuration"

& $icyloom init
if ($LASTEXITCODE -ne 0) { Fail "écriture de la configuration impossible"; exit 1 }
& $icyloom paths
Ok "configuration prête"

# ---------------------------------------------------------------------------
# 5. System tray + démarrage automatique
# ---------------------------------------------------------------------------
Step "Icône de zone de notification"

if ($NoAutostart) {
    Ok "démarrage automatique non demandé (-NoAutostart)"
}
else {
    & $icyloom tray --install-autostart
    if ($LASTEXITCODE -ne 0) { Fail "enregistrement du démarrage automatique impossible"; exit 1 }
    Ok "démarrage automatique activé (HKCU Run)"
}

# ---------------------------------------------------------------------------
# 6. Clients MCP
# ---------------------------------------------------------------------------
Step "Clients MCP"
if ($NoClient -or -not $Clients -or $Clients.Count -eq 0) {
    Ok "aucun client configuré (-NoClient)"
}
else {
    foreach ($client in $Clients) {
        & $icyloom client --target $client --install
        if ($LASTEXITCODE -eq 0) { Ok "$client configuré" }
        else { Fail "$client non configuré" }
    }
}

# ---------------------------------------------------------------------------
# 7. Diagnostic
# ---------------------------------------------------------------------------
Step "Diagnostic"

& $icyloom doctor
& $icyloom tray --status

Write-Host ""
Write-Host "Installation terminée." -ForegroundColor Green
Write-Host ""
Write-Host "Pour lancer l'icône maintenant :" -ForegroundColor Cyan
Write-Host "    $venvDir\Scripts\icyloom.exe tray"
Write-Host ""
Write-Host "Pour retirer le démarrage automatique :" -ForegroundColor Cyan
Write-Host "    $venvDir\Scripts\icyloom.exe tray --uninstall-autostart"
Write-Host ""
