"""
Resolução centralizada do caminho do FFmpeg e FFprobe.
Funciona tanto rodando via Python quanto empacotado com PyInstaller.
"""
import os
import sys
import shutil

from app.managers.SettingsManager import get_runtime_root


def get_ffmpeg_bin() -> str:
    """Retorna o caminho do binário ffmpeg."""
    return _resolve_bin('ffmpeg')


def get_ffprobe_bin() -> str:
    """Retorna o caminho do binário ffprobe."""
    return _resolve_bin('ffprobe')


def _resolve_bin(name: str) -> str:
    exe = f'{name}.exe' if os.name == 'nt' else name

    # 1) PyInstaller _MEIPASS (dados ficam em _internal/)
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            bundled = os.path.join(meipass, 'ffmpeg', 'bin', exe)
            if os.path.exists(bundled):
                return bundled

    # 2) bundled junto ao executável/projeto (modo dev)
    bundled = os.path.join(get_runtime_root(), 'ffmpeg', 'bin', exe)
    if os.path.exists(bundled):
        return bundled

    # 3) PATH do sistema
    which = shutil.which(name)
    if which:
        return which

    # fallback
    return name
