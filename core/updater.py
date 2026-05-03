"""
Atualizador automatico do Editor Premiere Premium.
Verifica versao remota, baixa ZIP e instala substituindo os arquivos do app.
Funciona apenas quando executando como .exe (PyInstaller frozen).
"""
from __future__ import annotations

import os
import sys
import json
import zipfile
import tempfile
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable

# Versao usada no User-Agent das requests. Importada tardiamente para evitar
# ciclos e para continuar funcionando mesmo se app/__version__.py falhar.
try:
    from app.__version__ import VERSAO as _VERSAO
except Exception:
    _VERSAO = "0.0"

_USER_AGENT = f"EditorPremiere-Updater/{_VERSAO}"

URL_VERSAO_REMOTA = (
    "https://magiaparamor.com/aplicacoes/editor-premiere-premium/versao.json"
)


def _versao_para_tupla(v: str) -> tuple:
    """
    Normaliza strings de versao para comparacao:
      - Remove prefixo 'v' ('v2.1' == '2.1')
      - Ignora segmentos nao-numericos
      - NAO faz padding aqui — o padding e feito no momento da comparacao
    """
    try:
        raw = str(v).strip().lstrip("vV")
        partes = []
        for p in raw.split("."):
            p = p.strip()
            if p.isdigit():
                partes.append(int(p))
            else:
                break  # ao encontrar algo nao numerico (ex: "2.1-beta"), para
        return tuple(partes) if partes else (0,)
    except Exception:
        return (0,)


def _comparar_versoes(a: str, b: str) -> int:
    """Retorna -1 se a<b, 0 se a==b, 1 se a>b. Padding com zeros para
    normalizar comprimentos (ex: 2.1 == 2.1.0)."""
    ta = _versao_para_tupla(a)
    tb = _versao_para_tupla(b)
    tam = max(len(ta), len(tb))
    ta = ta + (0,) * (tam - len(ta))
    tb = tb + (0,) * (tam - len(tb))
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


def verificar_atualizacao(versao_local: str) -> tuple[dict | None, str | None]:
    """
    Consulta versao.json remoto e retorna (info_dict, None) se houver versao nova,
    (None, None) se estiver atualizado, ou (None, mensagem_erro) se a checagem falhar.
    """
    try:
        req = urllib.request.Request(
            URL_VERSAO_REMOTA,
            headers={"User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            dados = json.loads(resp.read().decode("utf-8"))
        versao_remota = str(dados.get("versao", "0"))
        # So atualiza se o remoto for estritamente maior que o local.
        if _comparar_versoes(versao_remota, versao_local) > 0:
            return dados, None
        return None, None
    except Exception:
        return None, "Nao foi possivel verificar atualizacoes. O programa sera aberto normalmente."


def baixar_zip(
    url: str,
    destino: str,
    callback: Callable[[int, int], None] | None = None,
) -> None:
    """
    Baixa o ZIP para `destino` em streaming de 256 KB por vez.
    callback(bytes_baixados, total_bytes) e chamado a cada bloco.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        baixados = 0
        bloco = 256 * 1024  # 256 KB
        with open(destino, "wb") as f:
            while True:
                chunk = resp.read(bloco)
                if not chunk:
                    break
                f.write(chunk)
                baixados += len(chunk)
                if callback:
                    callback(baixados, total)


def instalar_atualizacao(
    zip_path: str,
    status_callback: Callable[[str], None] | None = None,
) -> None:
    """
    Extrai o ZIP para pasta temporaria, cria script PowerShell de substituicao
    em pasta separada e reinicia o app com uma janela GUI de progresso.
    Usa PowerShell + WinForms para evitar problemas de encoding em caminhos Unicode.
    Encerra o processo atual via os._exit(0) (ver !manual-login.md §6.7).
    Lanca excecao se nao estiver rodando como exe compilado.
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError(
            "Atualizacao automatica disponivel apenas no executavel compilado (.exe)."
        )

    app_dir = Path(sys.executable).parent

    # Pasta separada para extrair o ZIP
    extract_dir = Path(tempfile.mkdtemp(prefix="editorpremiere_ext_"))
    # Pasta separada para o script de instalacao (fora do conteudo extraido)
    ps1_dir = Path(tempfile.mkdtemp(prefix="editorpremiere_ps1_"))

    # Extrai ZIP
    if status_callback:
        status_callback("Extraindo arquivos...")

    with zipfile.ZipFile(zip_path, "r") as zf:
        membros = zf.namelist()
        total = len(membros)
        for i, membro in enumerate(membros, 1):
            zf.extract(membro, extract_dir)
            if status_callback and i % 100 == 0:
                status_callback(f"Extraindo... {i}/{total} arquivos")

    if status_callback:
        status_callback("Preparando instalacao...")

    exe_destino = app_dir / "Editor.exe"
    ps1_path = ps1_dir / "_instalar.ps1"

    # Escapa aspas simples para uso seguro em strings PowerShell
    def _ps(p: Path) -> str:
        return str(p).replace("'", "''")

    log_file = ps1_dir / "install.log"

    ps1_content = f"""\
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$logPath = '{_ps(log_file)}'

$form = New-Object System.Windows.Forms.Form
$form.Text = "Automatizador do Premiere - Atualizando"
$form.Size = New-Object System.Drawing.Size(640, 420)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(13, 17, 23)

$status = New-Object System.Windows.Forms.Label
$status.ForeColor = [System.Drawing.Color]::FromArgb(230, 237, 243)
$status.Font = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold)
$status.AutoSize = $false
$status.Size = New-Object System.Drawing.Size(600, 24)
$status.Location = New-Object System.Drawing.Point(15, 12)
$status.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
$status.Text = "Aguardando o programa fechar..."
$form.Controls.Add($status)

$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true
$log.ScrollBars = "Vertical"
$log.ReadOnly = $true
$log.WordWrap = $true
$log.Font = New-Object System.Drawing.Font("Consolas", 9)
$log.BackColor = [System.Drawing.Color]::FromArgb(22, 27, 34)
$log.ForeColor = [System.Drawing.Color]::FromArgb(201, 209, 217)
$log.BorderStyle = "FixedSingle"
$log.Size = New-Object System.Drawing.Size(600, 290)
$log.Location = New-Object System.Drawing.Point(15, 42)
$form.Controls.Add($log)

$btnClose = New-Object System.Windows.Forms.Button
$btnClose.Text = "Fechar"
$btnClose.Size = New-Object System.Drawing.Size(90, 28)
$btnClose.Location = New-Object System.Drawing.Point(525, 342)
$btnClose.BackColor = [System.Drawing.Color]::FromArgb(48, 54, 61)
$btnClose.ForeColor = [System.Drawing.Color]::FromArgb(230, 237, 243)
$btnClose.FlatStyle = "Flat"
$btnClose.Visible = $false
$btnClose.Add_Click({{ $form.Close() }})
$form.Controls.Add($btnClose)

$form.Show()
[System.Windows.Forms.Application]::DoEvents()

function Write-Log {{
    param([string]$msg, [string]$level = 'info')
    $ts = (Get-Date).ToString('HH:mm:ss')
    $line = "[$ts] $msg"
    $log.AppendText($line + [Environment]::NewLine)
    Add-Content -Path $logPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
    [System.Windows.Forms.Application]::DoEvents()
}}

function Set-Status {{
    param([string]$txt)
    $status.Text = $txt
    [System.Windows.Forms.Application]::DoEvents()
}}

try {{
    Set-Status "Aguardando o programa fechar..."
    Write-Log "Aguardando 3 segundos para o programa liberar arquivos..."
    Start-Sleep -Seconds 3

    Set-Status "Instalando atualizacao..."
    Write-Log "Iniciando copia de arquivos (robocopy)..."
    Write-Log "Origem : '{_ps(extract_dir)}'"
    Write-Log "Destino: '{_ps(app_dir)}'"

    $rcOutput = robocopy '{_ps(extract_dir)}' '{_ps(app_dir)}' /E /IS /IT /IM /NP /NFL /NDL /NJH /NJS 2>&1
    $rcExit = $LASTEXITCODE

    foreach ($linha in $rcOutput) {{
        if ($linha -and $linha.ToString().Trim().Length -gt 0) {{
            Write-Log ("    " + $linha.ToString().Trim())
        }}
    }}

    Write-Log "Robocopy finalizou com codigo: $rcExit"

    # Robocopy: 0 = nada copiado (ja estava sincronizado), 1-7 = sucesso com variacoes
    # >= 8 = erro real
    if ($rcExit -ge 8) {{
        throw "Robocopy falhou com codigo $rcExit (erro na copia de arquivos)."
    }}

    Set-Status "Reiniciando..."
    Write-Log "Atualizacao concluida com sucesso!"
    Write-Log "Abrindo o programa atualizado..."
    Start-Process '{_ps(exe_destino)}'
    Start-Sleep -Seconds 1

    Write-Log "Limpando arquivos temporarios..."
    Remove-Item -LiteralPath '{_ps(Path(zip_path))}' -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath '{_ps(extract_dir)}' -Recurse -Force -ErrorAction SilentlyContinue

    Start-Sleep -Seconds 1
    $form.Close()

    Start-Sleep -Milliseconds 300
    Remove-Item -LiteralPath '{_ps(ps1_dir)}' -Recurse -Force -ErrorAction SilentlyContinue
}}
catch {{
    # Em caso de erro: mostra em vermelho, mantem janela aberta com botao Fechar
    Set-Status "Falha na atualizacao."
    $status.ForeColor = [System.Drawing.Color]::FromArgb(248, 81, 73)
    Write-Log ""
    Write-Log "!!! ERRO !!!"
    Write-Log $_.ToString()
    Write-Log ""
    Write-Log "O log completo foi salvo em:"
    Write-Log $logPath
    $btnClose.Visible = $true
    $form.TopMost = $false
    [System.Windows.Forms.Application]::Run($form)
}}
"""

    # UTF-8 com BOM: PowerShell le corretamente caminhos Unicode
    ps1_path.write_text(ps1_content, encoding="utf-8-sig")

    if status_callback:
        status_callback("Reiniciando o programa...")

    subprocess.Popen(
        [
            "powershell.exe",
            "-ExecutionPolicy", "Bypass",
            "-NonInteractive",
            "-WindowStyle", "Hidden",
            "-File", str(ps1_path),
        ],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    # CRITICO: os._exit(0), NUNCA sys.exit(0).
    # instalar_atualizacao() e chamada de dentro de uma thread (download bloqueia).
    # sys.exit(0) em thread apenas levanta SystemExit na propria thread — a janela
    # Tk/CTk continua rodando no main loop e segura o handle do .exe, o robocopy
    # dentro do PS1 trava em "Instalando atualizacao...".
    # os._exit(0) encerra o processo inteiro imediatamente, liberando todos os
    # handles de arquivo. (ver !manual-login.md §6.7 e §14)
    os._exit(0)
