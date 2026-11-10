@echo off
REM Gera os executaveis a partir do codigo Python:
REM   baixar_nfse.exe  (um cliente, conforme o config.ini)
REM   baixar_lote.exe  (varios clientes, conforme o clientes.csv)
REM So precisa ser rodado por quem MANTEM o programa (nao pelos usuarios finais) --
REM exige Python instalado. Usuarios finais so usam os .exe prontos.

echo Instalando/atualizando PyInstaller e reportlab...
python -m pip install --quiet --upgrade pyinstaller reportlab
if errorlevel 1 goto erro

for %%P in (baixar_nfse baixar_lote) do (
    echo.
    echo Gerando %%P.exe...
    REM --noupx: sem compressao UPX, que e gatilho comum de falso positivo em antivirus.
    REM --collect-submodules: a reportlab carrega os modulos de codigo de barras dinamicamente.
    python -m PyInstaller --onefile --noupx --name %%P --distpath . --workpath build_temp --specpath build_temp --collect-submodules reportlab.graphics.barcode %%P.py
    if errorlevel 1 goto erro
)

echo.
echo Pronto: baixar_nfse.exe e baixar_lote.exe atualizados nesta pasta.
pause
exit /b 0

:erro
echo.
echo Ocorreu um erro ao gerar os executaveis. Veja as mensagens acima.
pause
exit /b 1
