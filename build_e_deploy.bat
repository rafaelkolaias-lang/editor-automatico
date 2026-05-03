@echo off
echo.
echo ============================================
echo   Build e Deploy - Automatizador do Premiere
echo ============================================
echo.

:: ══════════════════════════════════════════════
:: PERGUNTAS INICIAIS
:: ══════════════════════════════════════════════

set ZIPAR=N
set DEPLOY=N

set /p ZIPAR="Deseja zipar o projeto? (S/N): "
if /i "%ZIPAR%"=="S" (
    set /p DEPLOY="Deseja fazer o deploy depois? (S/N): "
)

echo.

:: ══════════════════════════════════════════════
:: PARTE 1 — BUILD
:: ══════════════════════════════════════════════

python --version
if errorlevel 1 (
    echo [ERRO] Python nao encontrado no PATH.
    pause
    exit /b 1
)

echo [1/5] Lendo versao do versao.json...
for /f "delims=" %%v in ('powershell -Command "(Get-Content versao.json | ConvertFrom-Json).versao"') do set VERSAO=%%v
if "%VERSAO%"=="" (
    echo [ERRO] Nao foi possivel ler a versao do versao.json.
    pause
    exit /b 1
)
echo       Versao: %VERSAO%
set ZIP=editor-premiere-premium-v%VERSAO%.zip

echo [1.5/5] Sincronizando VERSAO no codigo Python com versao.json...
powershell -Command "$v='%VERSAO%'; $f='app\__version__.py'; $c=Get-Content $f -Raw -Encoding UTF8; $c=[regex]::Replace($c,'(?m)^VERSAO\s*=\s*\".*?\"',('VERSAO = \"'+$v+'\"')); $c=[regex]::Replace($c,'(?m)^VERSAO_APLICACAO\s*=\s*\".*?\"',('VERSAO_APLICACAO = \"v'+$v+'\"')); [System.IO.File]::WriteAllText((Resolve-Path $f),$c,(New-Object System.Text.UTF8Encoding($false)))"
if errorlevel 1 (
    echo [ERRO] Falha ao sincronizar VERSAO no codigo Python.
    pause
    exit /b 1
)
for /f "delims=" %%c in ('powershell -Command "(Select-String -Path app\__version__.py -Pattern '^VERSAO\s*=' | Select-Object -First 1).Line"') do echo       %%c
for /f "delims=" %%c in ('powershell -Command "(Select-String -Path app\__version__.py -Pattern '^VERSAO_APLICACAO\s*=' | Select-Object -First 1).Line"') do echo       %%c

echo [1.7/5] Atualizando numero da versao nas notas de atualizacao...
powershell -Command "$ErrorActionPreference='Stop'; $v='%VERSAO%'; $f='assets\release_notes.txt'; if (Test-Path $f) { $c=Get-Content $f -Raw -Encoding UTF8; $c=[regex]::Replace($c,'VERS\u00c3O\s+\d+(?:\.\d+)*\s+\u2605',('VERS\u00c3O '+$v)); [System.IO.File]::WriteAllText((Resolve-Path $f),$c,(New-Object System.Text.UTF8Encoding($false))) }"
if errorlevel 1 (
    echo [AVISO] Falha ao atualizar release_notes.txt. Verifique manualmente.
)

echo [2/5] Instalando PyInstaller...
python -m pip install --quiet --upgrade pyinstaller
if errorlevel 1 (
    echo [ERRO] Falha ao instalar PyInstaller.
    pause
    exit /b 1
)

echo [3/5] Limpando builds anteriores...
if exist projeto-compilado rmdir /s /q projeto-compilado
if exist build rmdir /s /q build
if exist Editor.spec del /f /q Editor.spec

echo [4/5] Gerando executavel (aguarde alguns minutos)...
echo.
python -m PyInstaller ^
    --name Editor ^
    --onedir ^
    --windowed ^
    --distpath projeto-compilado ^
    --hidden-import pymysql ^
    --hidden-import pymysql.cursors ^
    --hidden-import PIL ^
    --hidden-import PIL._tkinter_finder ^
    --hidden-import nacl ^
    --hidden-import nacl.secret ^
    --copy-metadata imageio ^
    --copy-metadata imageio-ffmpeg ^
    --copy-metadata moviepy ^
    --collect-data imageio_ffmpeg ^
    --collect-submodules assemblyai ^
    --collect-submodules openai ^
    --collect-submodules google.genai ^
    --collect-submodules pymiere ^
    index.py

if errorlevel 1 (
    echo.
    echo [ERRO] Build falhou. Verifique os erros acima.
    pause
    exit /b 1
)

echo.
echo [5/5] Organizando arquivos...

:: Move conteudo de projeto-compilado\Editor para projeto-compilado (sem subpasta)
robocopy "projeto-compilado\Editor" "projeto-compilado" /E /MOVE /NP /NFL /NDL /NJH /NJS
rmdir /s /q "projeto-compilado\Editor" 2>nul

:: ── COPIAR ASSETS ADICIONAIS ──
copy /Y versao.json projeto-compilado\

if exist ffmpeg\bin\ffmpeg.exe (
    xcopy /E /I /Y /Q ffmpeg projeto-compilado\ffmpeg
    echo       ffmpeg copiado.
) else (
    echo       [AVISO] ffmpeg\bin\ffmpeg.exe nao encontrado.
)

if exist fontes\ (
    xcopy /E /I /Y /Q fontes projeto-compilado\fontes
)

if exist assets\ (
    xcopy /E /I /Y /Q assets projeto-compilado\assets
)
:: ── FIM ASSETS ──

:: Limpa pasta build intermediaria do PyInstaller
if exist build rmdir /s /q build

echo.
echo ============================================
echo   Build concluido!
echo   Pasta : projeto-compilado\
echo ============================================
echo.

:: ══════════════════════════════════════════════
:: ZIP (opcional)
:: ══════════════════════════════════════════════

if /i not "%ZIPAR%"=="S" (
    echo Compactacao pulada conforme solicitado.
    pause
    exit /b 0
)

echo Compactando em %ZIP%...
if exist %ZIP% del /f /q %ZIP%
powershell -Command "Compress-Archive -Path 'projeto-compilado\*' -DestinationPath '%ZIP%' -Force"
echo        %ZIP% gerado.
echo.

:: ══════════════════════════════════════════════
:: PARTE 2 — DEPLOY (opcional)
:: ══════════════════════════════════════════════

if /i not "%DEPLOY%"=="S" (
    echo.
    if /i "%ZIPAR%"=="S" (
        echo Deploy nao solicitado. O ZIP esta pronto para envio manual.
    ) else (
        echo Deploy nao aplicavel ^(ZIP nao foi gerado^).
    )
    pause
    exit /b 0
)

echo.
echo ============================================
echo   Iniciando deploy...
echo ============================================
echo.

:: Credenciais FTP
set FTP_HOST=magiaparamor.com
set FTP_USER=magiap98
set FTP_PASS=Dfv15d9f5@@4vdfv.
set FTP_DIR=/public_html/aplicacoes/editor-premiere-premium/

echo [7/11] Preparando versao.json para upload...
powershell -Command "$ErrorActionPreference='Stop'; if (-not (Test-Path versao.json)) { throw 'versao.json nao existe.' }; $j=Get-Content versao.json -Raw | ConvertFrom-Json; $v=$j.versao; if ([string]::IsNullOrWhiteSpace($v)) { throw 'versao esta vazia ou nula em versao.json.' }; [PSCustomObject]@{versao=$v;arquivo=\"editor-premiere-premium-v$v.zip\";url=\"https://magiaparamor.com/aplicacoes/editor-premiere-premium/editor-premiere-premium-v$v.zip\"}|ConvertTo-Json|Set-Content versao.json"
if errorlevel 1 (
    echo [ERRO] Falha ao preparar versao.json. Verifique o conteudo do arquivo.
    pause & exit /b 1
)
echo        Status 'em desenvolvimento' removido.

echo [8/11] Verificando ZIP...
if not exist %ZIP% (
    echo [ERRO] %ZIP% nao encontrado.
    pause & exit /b 1
)
echo        %ZIP%  OK

echo [9/11] Verificando versoes antigas no servidor...
for /f "delims=" %%a in ('powershell -Command "$f=\"versoes_publicadas.txt\"; $v=\"%VERSAO%\"; $l=if(Test-Path $f){@(Get-Content $f)}else{@()}; $l=@($l)+$v; $del=\"\"; if($l.Count -gt 2){$del=$l[0]; $l=$l[1..($l.Count-1)]}; $l | Set-Content $f; Write-Output $del"') do set APAGAR=%%a

if not "%APAGAR%"=="" (
    echo        Deletando versao antiga: editor-premiere-premium-v%APAGAR%.zip
    curl ftp://%FTP_HOST%%FTP_DIR% --user %FTP_USER%:%FTP_PASS% --quote "DELE %FTP_DIR%editor-premiere-premium-v%APAGAR%.zip" --silent
    echo        Versao %APAGAR% removida do servidor.
) else (
    echo        Nenhuma versao antiga para remover.
)

echo [10/11] Enviando para o servidor...
echo.
curl -T versao.json ftp://%FTP_HOST%%FTP_DIR%versao.json --user %FTP_USER%:%FTP_PASS% --ftp-create-dirs
if errorlevel 1 (
    echo [ERRO] Falha ao enviar versao.json.
    pause & exit /b 1
)
echo        versao.json enviado.

echo        Enviando %ZIP% (pode demorar para arquivos grandes)...
curl -T %ZIP% ftp://%FTP_HOST%%FTP_DIR%%ZIP% --user %FTP_USER%:%FTP_PASS% --max-time 0 --retry 5 --retry-delay 15 --retry-connrefused
if errorlevel 1 (
    echo [ERRO] Falha ao enviar %ZIP%.
    pause & exit /b 1
)
echo        %ZIP% enviado.

echo [11/11] Incrementando versao para desenvolvimento...
powershell -Command "$ErrorActionPreference='Stop'; $j=Get-Content versao.json -Raw | ConvertFrom-Json; if ([string]::IsNullOrWhiteSpace($j.versao)) { throw 'versao esta vazia em versao.json.' }; $p=$j.versao.Split('.'); $p[-1]=[int]$p[-1]+1; $nova=$p -join '.'; [PSCustomObject]@{versao=$nova;status='em desenvolvimento';arquivo=\"editor-premiere-premium-v$nova.zip\";url=\"https://magiaparamor.com/aplicacoes/editor-premiere-premium/editor-premiere-premium-v$nova.zip\"}|ConvertTo-Json|Set-Content versao.json"
if errorlevel 1 (
    echo [AVISO] Falha ao incrementar versao local. Edite versao.json manualmente.
)
for /f "delims=" %%n in ('powershell -Command "(Get-Content versao.json | ConvertFrom-Json).versao"') do set PROXIMA=%%n
echo        Proxima versao: %PROXIMA% (em desenvolvimento)

echo.
echo ============================================
echo   Deploy concluido! Versao %VERSAO% publicada.
echo   Proxima: v%PROXIMA% (em desenvolvimento)
echo   https://magiaparamor.com/aplicacoes/editor-premiere-premium/versao.json
echo   https://magiaparamor.com/aplicacoes/editor-premiere-premium/%ZIP%
echo ============================================
echo.
pause
