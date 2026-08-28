@echo off
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
echo ============================================
echo   Notas de Devolucao - Controle de Abatimento
echo ============================================
echo.
echo Abrindo no navegador... para fechar o app, feche esta janela.
echo.
.venv\Scripts\python.exe -m streamlit run app.py
pause
