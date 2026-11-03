@echo off
rem Lance IcyLoom dans la zone de notification de Windows (system tray).
rem Aucune fenetre : l'icone ouvre la console et pilote la pile.
set "HERE=%~dp0"
set "PY=%HERE%..\.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%HERE%..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [IcyLoom] environnement introuvable : cree-le avec "python -m venv .venv" dans le depot.
    pause
    exit /b 1
)
start "" "%PY%" -m icyloom tray --background
