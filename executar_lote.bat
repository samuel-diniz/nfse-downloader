@echo off
REM Atalho do modo em lote: dois cliques para baixar as NFS-e de todos os clientes do clientes.csv.
REM Pergunta o periodo (ENTER = mes anterior). Usa o baixar_lote.exe se existir; senao, o Python.
cd /d "%~dp0"
if exist baixar_lote.exe (
    baixar_lote.exe %*
) else (
    python baixar_lote.py %*
)
echo.
pause
