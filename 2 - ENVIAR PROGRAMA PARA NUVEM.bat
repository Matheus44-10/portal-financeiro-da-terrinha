@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Enviar o PROGRAMA do portal para a nuvem
echo ============================================
echo.
echo Isso envia as telas e melhorias do portal (nao os dados).
echo Os dados continuam sendo enviados pelo botao "Publicar para nuvem" do app.
echo.
pause

git add app.py config/settings.cloud.toml config/settings.example.toml docs/historico_desenvolvimento.md src/notas_devolucao Notas_Devolucao_Antes_Depois.pptx "2 - ENVIAR PROGRAMA PARA NUVEM.bat"
git commit -m "Atualiza o programa do portal (historico, materia-prima, motivo, factoring)"
git push origin main

echo.
if %errorlevel%==0 (
    echo Pronto! Em 2 a 3 minutos o portal da nuvem estara com o programa novo.
) else (
    echo Algo deu errado - tire um print desta janela e mande pro Claude.
)
echo.
pause
