@echo off
rem Lance IcyLoom dans la zone de notification de Windows (system tray).
rem Aucune fenetre : l'icone permet d'ouvrir la console et d'arreter la pile.
set "HERE=%~dp0"
if exist "%HERE%..\.venv\Scripts\pythonw.exe" (
    start "" "%HERE%..\.venv\Scripts\pythonw.exe" "%HERE%tray.py"
) else (
    start "" pythonw "%HERE%tray.py"
)
