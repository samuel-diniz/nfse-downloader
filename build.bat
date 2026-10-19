@echo off
REM Gera o executavel baixar_nfse.exe a partir do baixar_nfse.py.
REM So precisa ser rodado por quem MANTEM o programa (nao pelos usuarios
REM finais) -- exige Python instalado. Usuarios finais so usam o .exe
REM pronto, sem precisar disso.

echo Instalando/atualizando PyInstaller...
python -m pip install --quiet --upgrade pyinstaller
if errorlevel 1 goto erro

echo.
echo Gerando baixar_nfse.exe...
REM --noupx: sem compressao UPX, que e gatilho comum de falso positivo em antivirus.
python -m PyInstaller --onefile --noupx --name baixar_nfse --distpath . --workpath build_temp --specpath build_temp baixar_nfse.py
if errorlevel 1 goto erro

echo.
echo Pronto: baixar_nfse.exe atualizado nesta pasta.
pause
exit /b 0

:erro
echo.
echo Ocorreu um erro ao gerar o executavel. Veja as mensagens acima.
pause
exit /b 1
