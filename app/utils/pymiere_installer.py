"""Instalador do plugin pymiere (Pymiere Link) no Adobe Premiere Pro.

Fluxo completo (acionado pelo menu "Opcoes > Instalar pymiere"):
  1. Baixa e instala silenciosamente um Python embeddable (em
     %LOCALAPPDATA%\\PymiereInstaller\\python).
  2. Habilita pip nesse Python e roda ``pip install pymiere``.
  3. Copia os painéis CEP bundled em ``assets/pymiere_panel/`` para
     %APPDATA%\\Adobe\\CEP\\extensions e também para a pasta system-wide
     em ``Program Files (x86)\\Common Files\\Adobe\\CEP\\extensions``
     (best-effort — system-wide só funciona com admin).
  4. Habilita ``PlayerDebugMode`` em ``HKCU\\Software\\Adobe\\CSXS.{9..12}``.
  5. Avisa o usuário para reiniciar o Adobe Premiere Pro.

API pública:
    install_pymiere_panel(progress=...) -> InstallResult
    open_install_dialog(parent)
"""

from __future__ import annotations

import os
import sys
import shutil
import ctypes
import subprocess
import threading
import urllib.request
import urllib.error
import zipfile
import tkinter as tk
from tkinter import messagebox
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

# Constante para evitar janelas de console em subprocess no Windows
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# ──────────────────────────────────────────────────────────────────────
# Constantes de download (Python embeddable + get-pip)
# ──────────────────────────────────────────────────────────────────────

_PY_VERSION = "3.11.9"
_PY_EMBED_URL = (
    f"https://www.python.org/ftp/python/{_PY_VERSION}"
    f"/python-{_PY_VERSION}-embed-amd64.zip"
)
_GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


# ──────────────────────────────────────────────────────────────────────
# Localização da fonte (painéis empacotados em assets/pymiere_panel/)
# ──────────────────────────────────────────────────────────────────────

def _bundled_panels_root() -> Path:
    """Pasta com os painéis pymiere empacotados (assets/pymiere_panel)."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parents[2]
    return base / "assets" / "pymiere_panel"


# ──────────────────────────────────────────────────────────────────────
# Destinos possíveis no sistema (CEP extensions)
# ──────────────────────────────────────────────────────────────────────

def _user_extensions_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        appdata = str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Adobe" / "CEP" / "extensions"


def _system_extensions_dir_x86() -> Path:
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    return Path(pf86) / "Common Files" / "Adobe" / "CEP" / "extensions"


def _system_extensions_dir_x64() -> Path:
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    return Path(pf) / "Common Files" / "Adobe" / "CEP" / "extensions"


def _embedded_python_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / "PymiereInstaller" / "python"


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────
# Verificações
# ──────────────────────────────────────────────────────────────────────

def _premiere_running() -> bool:
    """Detecta se Adobe Premiere Pro está rodando (evita arquivos travados)."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq Adobe Premiere Pro.exe"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            creationflags=_NO_WINDOW,
        )
        return "Adobe Premiere Pro.exe" in out
    except Exception:
        return False


def _can_write(path: Path) -> bool:
    """Testa se conseguimos escrever em path (criando-o se preciso)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = path / ".pymiere_install_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


# ──────────────────────────────────────────────────────────────────────
# Download com progresso
# ──────────────────────────────────────────────────────────────────────

def _download_with_progress(
    url: str,
    dst: Path,
    label: str,
    log: Callable[[str], None],
) -> None:
    """Baixa um arquivo reportando progresso a cada ~5%."""
    log(f"Baixando {label}: {url}")
    last_pct = -1

    def _hook(block_num: int, block_size: int, total_size: int) -> None:
        nonlocal last_pct
        if total_size <= 0:
            return
        downloaded = block_num * block_size
        pct = min(100, int(downloaded * 100 / total_size))
        if pct - last_pct >= 5 or pct == 100:
            last_pct = pct
            mb = downloaded / (1024 * 1024)
            tot = total_size / (1024 * 1024)
            log(f"  ...{pct:3d}% ({mb:.1f} / {tot:.1f} MB)")

    dst.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dst, reporthook=_hook)
    log(f"  baixado em: {dst}")


# ──────────────────────────────────────────────────────────────────────
# Python embeddable + pip install pymiere
# ──────────────────────────────────────────────────────────────────────

def _python_has_pymiere(python_exe: Path) -> bool:
    if not python_exe.exists():
        return False
    try:
        subprocess.check_call(
            [str(python_exe), "-c", "import pymiere"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            creationflags=_NO_WINDOW,
        )
        return True
    except Exception:
        return False


def _enable_pip_in_pth(python_root: Path, log: Callable[[str], None]) -> None:
    """Descomenta 'import site' no python*._pth para permitir pip e site-packages."""
    pth_files = list(python_root.glob("python*._pth"))
    if not pth_files:
        log("  Aviso: nenhum python*._pth encontrado (pip pode falhar).")
        return
    pth = pth_files[0]
    content = pth.read_text(encoding="utf-8")
    new_content = content.replace("#import site", "import site")
    if new_content != content:
        pth.write_text(new_content, encoding="utf-8")
        log(f"  Habilitado 'import site' em {pth.name}")
    else:
        log(f"  '{pth.name}' ja permitia site-packages.")


def _install_embedded_python(log: Callable[[str], None]) -> Path:
    """Baixa e configura Python embeddable, instala pip e pymiere.

    Retorna o caminho do python.exe instalado. Levanta exceção em caso de erro.
    """
    install_root = _embedded_python_root()
    python_exe = install_root / "python.exe"

    if _python_has_pymiere(python_exe):
        log(f"Python embeddable + pymiere ja instalados em: {install_root}")
        return python_exe

    install_root.mkdir(parents=True, exist_ok=True)
    zip_path = install_root.parent / "python-embed.zip"

    # 1. Download do Python embeddable
    _download_with_progress(_PY_EMBED_URL, zip_path,
                            f"Python {_PY_VERSION} embeddable", log)

    # 2. Extração
    log("Extraindo Python embeddable...")
    if python_exe.exists():
        # já existe instalação parcial, limpar o conteúdo (mantém pasta)
        for item in install_root.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                try:
                    item.unlink()
                except OSError:
                    pass
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(install_root)
    try:
        zip_path.unlink()
    except OSError:
        pass

    if not python_exe.exists():
        raise RuntimeError(
            f"python.exe nao apareceu em {install_root} apos extracao."
        )
    log(f"  python.exe disponivel em: {python_exe}")

    # 3. Habilitar pip via _pth
    _enable_pip_in_pth(install_root, log)

    # 4. Baixar e rodar get-pip.py
    get_pip_path = install_root / "get-pip.py"
    _download_with_progress(_GET_PIP_URL, get_pip_path, "get-pip.py", log)

    log("Instalando pip no Python embeddable...")
    try:
        subprocess.check_call(
            [str(python_exe), str(get_pip_path), "--no-warn-script-location"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            timeout=300,
            creationflags=_NO_WINDOW,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Falha ao executar get-pip.py (exit {e.returncode}).")
    log("  pip instalado.")

    # 5. pip install pymiere
    log("Rodando 'pip install pymiere' (pode levar 1-2 minutos)...")
    try:
        subprocess.check_call(
            [str(python_exe), "-m", "pip", "install",
             "--no-warn-script-location", "pymiere"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            timeout=600,
            creationflags=_NO_WINDOW,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Falha ao instalar pymiere (exit {e.returncode}).")
    log("  pymiere instalado.")

    return python_exe


# ──────────────────────────────────────────────────────────────────────
# Registro: PlayerDebugMode
# ──────────────────────────────────────────────────────────────────────

def _enable_player_debug_mode(log: Callable[[str], None]) -> List[str]:
    """Habilita PlayerDebugMode em HKCU para CSXS 9..12. Não-fatal."""
    try:
        import winreg
    except ImportError:
        log("Aviso: winreg indisponivel — pulando PlayerDebugMode.")
        return []
    changed: List[str] = []
    for v in range(9, 13):
        sub = fr"Software\Adobe\CSXS.{v}"
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub, 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "PlayerDebugMode", 0, winreg.REG_SZ, "1")
            changed.append(f"HKCU\\{sub}")
        except OSError as e:
            log(f"  Aviso: nao gravou {sub}: {e}")
    return changed


# ──────────────────────────────────────────────────────────────────────
# Cópia dos painéis
# ──────────────────────────────────────────────────────────────────────

def _copy_panel(src: Path, dst: Path, log: Callable[[str], None]) -> None:
    if dst.exists():
        log(f"  - Removendo versao anterior: {dst}")
        shutil.rmtree(dst)
    log(f"  - Copiando {src.name} -> {dst.parent}")
    shutil.copytree(src, dst)


def _copy_panels_to_all(
    panels: List[Path],
    candidates: List[Path],
    log: Callable[[str], None],
) -> List[Path]:
    """Copia cada painel para todos os destinos onde temos permissão.

    Retorna a lista de pastas-destino onde a cópia teve sucesso.
    """
    copied_to: List[Path] = []
    for dst_root in candidates:
        log(f"Destino: {dst_root}")
        if not _can_write(dst_root):
            log(f"  ! Sem permissao de escrita — pulando "
                f"({'requer admin' if 'Program Files' in str(dst_root) else 'pasta protegida'}).")
            continue
        try:
            for panel in panels:
                _copy_panel(panel, dst_root / panel.name, log)
            copied_to.append(dst_root)
            log(f"  OK -> instalado em {dst_root}")
        except PermissionError as e:
            log(f"  ! Permissao negada: {e}")
        except OSError as e:
            log(f"  ! Erro de IO: {e}")
        except Exception as e:  # noqa: BLE001
            log(f"  ! Erro inesperado: {type(e).__name__}: {e}")
    return copied_to


# ──────────────────────────────────────────────────────────────────────
# Resultado e API principal
# ──────────────────────────────────────────────────────────────────────

@dataclass
class InstallResult:
    ok: bool
    message: str
    panel_targets: List[Path] = field(default_factory=list)
    python_exe: Optional[Path] = None
    log: List[str] = field(default_factory=list)


def install_pymiere_panel(
    *,
    progress: Callable[[str], None] = lambda _m: None,
) -> InstallResult:
    """Instala Python embeddable + pymiere + painéis CEP em todos os destinos.

    Cada erro é capturado e devolvido em ``InstallResult``.
    """
    log: List[str] = []

    def add(msg: str) -> None:
        log.append(msg)
        try:
            progress(msg)
        except Exception:
            pass

    try:
        # ── 0. Origem dos painéis ────────────────────────────────────
        src_root = _bundled_panels_root()
        add(f"Origem dos paineis: {src_root}")
        if not src_root.is_dir():
            return InstallResult(
                ok=False,
                message=(
                    "Pasta dos paineis nao encontrada no executavel.\n"
                    f"Esperado em: {src_root}\n\n"
                    "Reinstale o aplicativo ou contate o suporte."
                ),
                log=log,
            )
        panels = sorted(p for p in src_root.iterdir() if p.is_dir())
        if not panels:
            return InstallResult(
                ok=False,
                message=f"Nenhum painel encontrado em {src_root}",
                log=log,
            )
        add(f"Paineis a instalar: {', '.join(p.name for p in panels)}")

        # ── 1. Premiere fechado ──────────────────────────────────────
        if _premiere_running():
            return InstallResult(
                ok=False,
                message=(
                    "O Adobe Premiere Pro esta aberto.\n\n"
                    "Feche o Premiere completamente e tente novamente "
                    "(arquivos podem ficar travados se o Premiere estiver rodando)."
                ),
                log=log,
            )
        add("Adobe Premiere Pro nao esta em execucao. OK.")

        # ── 2. Python embeddable + pip install pymiere ──────────────
        add("")
        add("=== ETAPA 1/4: Python embeddable + pymiere ===")
        python_exe: Optional[Path] = None
        try:
            python_exe = _install_embedded_python(add)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            return InstallResult(
                ok=False,
                message=(
                    "Falha ao baixar o Python embeddable.\n\n"
                    f"Erro: {e}\n\n"
                    "Verifique sua conexao com a internet e tente novamente. "
                    "Se estiver atras de um proxy/firewall corporativo, "
                    "libere acesso a python.org e bootstrap.pypa.io."
                ),
                log=log,
            )
        except subprocess.TimeoutExpired:
            return InstallResult(
                ok=False,
                message=(
                    "Tempo esgotado durante a instalacao do Python/pymiere.\n\n"
                    "Verifique sua conexao com a internet e tente novamente."
                ),
                log=log,
            )
        except RuntimeError as e:
            return InstallResult(
                ok=False,
                message=f"Falha na instalacao do Python embeddable:\n{e}",
                log=log,
            )

        # ── 3. Cópia dos painéis em TODOS os destinos ───────────────
        add("")
        add("=== ETAPA 2/4: Copiar paineis CEP ===")
        candidates = [
            _user_extensions_dir(),         # %APPDATA%\Adobe\CEP\extensions
            _system_extensions_dir_x86(),   # Program Files (x86)\... (admin)
            _system_extensions_dir_x64(),   # Program Files\... (admin, raro)
        ]
        copied_to = _copy_panels_to_all(panels, candidates, add)
        if not copied_to:
            admin = "SIM" if _is_admin() else "NAO"
            return InstallResult(
                ok=False,
                message=(
                    "Nao foi possivel copiar os paineis em nenhum destino.\n\n"
                    f"Executando como administrador: {admin}\n\n"
                    "Possiveis causas:\n"
                    "  - Antivirus bloqueando a copia\n"
                    "  - Pastas CEP protegidas pelo sistema\n"
                    "  - Disco cheio ou somente-leitura\n\n"
                    "Tente: (1) executar o Editor como Administrador, "
                    "(2) desativar o antivirus temporariamente."
                ),
                python_exe=python_exe,
                log=log,
            )

        # ── 4. PlayerDebugMode ──────────────────────────────────────
        add("")
        add("=== ETAPA 3/4: Habilitar PlayerDebugMode ===")
        changed = _enable_player_debug_mode(add)
        if changed:
            add(f"PlayerDebugMode habilitado em: {', '.join(changed)}")
        else:
            add("Aviso: PlayerDebugMode nao foi habilitado em nenhuma chave.")

        # ── 5. Sucesso ───────────────────────────────────────────────
        add("")
        add("=== ETAPA 4/4: Concluido ===")
        targets_str = "\n".join(f"  - {p}" for p in copied_to)
        message = (
            "Instalacao concluida com sucesso!\n\n"
            f"Python + pymiere:\n  {python_exe.parent if python_exe else '(nao instalado)'}\n\n"
            f"Paineis CEP copiados em:\n{targets_str}\n\n"
            "==> REINICIE o Adobe Premiere Pro para ativar o painel.\n\n"
            "No Premiere, abra: Janela > Extensoes > Pymiere Link"
        )
        add(message)
        return InstallResult(
            ok=True,
            message=message,
            panel_targets=copied_to,
            python_exe=python_exe,
            log=log,
        )

    except Exception as e:  # noqa: BLE001
        return InstallResult(
            ok=False,
            message=(
                f"Erro inesperado durante a instalacao:\n"
                f"{type(e).__name__}: {e}\n\n"
                "Veja o log completo na janela do instalador."
            ),
            log=log + [f"EXCECAO: {type(e).__name__}: {e}"],
        )


# ──────────────────────────────────────────────────────────────────────
# UI Tk: janela com log de progresso
# ──────────────────────────────────────────────────────────────────────

def open_install_dialog(parent: tk.Misc) -> None:
    """Abre janela Tk com confirmação, executa instalação em thread e mostra log."""

    if not messagebox.askyesno(
        "Instalar painel pymiere",
        (
            "Esta operacao vai:\n\n"
            "  1. Baixar e instalar um Python embeddable (~10MB)\n"
            "  2. Rodar 'pip install pymiere' nele\n"
            "  3. Copiar o painel CEP para %APPDATA%\\Adobe\\CEP\\extensions\n"
            "     e tambem para a pasta system-wide (se tiver permissao)\n"
            "  4. Habilitar PlayerDebugMode no registro\n\n"
            "IMPORTANTE: feche o Adobe Premiere Pro antes de continuar.\n"
            "Necessario conexao com a internet.\n\n"
            "Deseja prosseguir?"
        ),
        parent=parent,
    ):
        return

    win = tk.Toplevel(parent)
    win.title("Instalar painel pymiere")
    win.geometry("720x500")
    win.transient(parent)
    win.grab_set()

    bg = "#1e1e1e"
    win.configure(bg=bg)

    title = tk.Label(
        win,
        text="Instalando painel pymiere...",
        bg=bg, fg="#ffffff",
        font=("Arial", 12, "bold"),
        pady=10,
    )
    title.pack(fill="x")

    text_frame = tk.Frame(win, bg=bg)
    text_frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    text = tk.Text(
        text_frame, wrap="word",
        bg="#0d1117", fg="#d4d4d4",
        font=("Consolas", 9),
        padx=8, pady=8,
        state="disabled",
        relief="flat",
    )
    sb = tk.Scrollbar(text_frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)

    btn_frame = tk.Frame(win, bg=bg)
    btn_frame.pack(fill="x", padx=12, pady=(0, 12))
    close_btn = tk.Button(btn_frame, text="Fechar", state="disabled",
                          command=win.destroy, width=14)
    close_btn.pack(side="right")

    def append(msg: str) -> None:
        text.configure(state="normal")
        text.insert("end", msg + "\n")
        text.see("end")
        text.configure(state="disabled")

    def progress(msg: str) -> None:
        try:
            win.after(0, lambda m=msg: append(m))
        except Exception:
            pass

    def worker() -> None:
        result = install_pymiere_panel(progress=progress)

        def finalize() -> None:
            append("")
            append("=" * 60)
            append("RESULTADO:")
            append(result.message)
            title.configure(
                text=("Instalacao concluida — REINICIE o Premiere"
                      if result.ok else "Instalacao falhou"),
                fg=("#22c55e" if result.ok else "#ef4444"),
            )
            close_btn.configure(state="normal")
            if result.ok:
                messagebox.showinfo("Pymiere", result.message, parent=win)
            else:
                messagebox.showerror("Pymiere", result.message, parent=win)

        try:
            win.after(0, finalize)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()
