import logging
import os
import time
import tkinter as tk
from tkinter import messagebox


def _write_crash_log(message: str, *, exc_info=None) -> str:
    """
    Escreve um log em logs/crash_<timestamp>.log e retorna o caminho do arquivo.
    exc_info pode ser:
      - None
      - True (usa sys.exc_info do contexto atual)
      - tuple (exc_type, exc_value, exc_tb)
    """
    timestamp = int(time.time())

    os.makedirs('logs', exist_ok=True)
    log_path = os.path.join('logs', f'crash_{timestamp}.log')

    logger = logging.getLogger('crash_logger')
    logger.setLevel(logging.ERROR)
    logger.propagate = False

    # remove handlers antigos (evita duplicar linhas se a função for chamada mais de uma vez)
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)

    handler = logging.FileHandler(log_path, encoding='utf-8')
    handler.setLevel(logging.ERROR)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logger.error(message, exc_info=exc_info)

    try:
        handler.close()
    except Exception:
        pass
    try:
        logger.removeHandler(handler)
    except Exception:
        pass

    return log_path


def _show_fatal_error_message():
    messagebox.showerror(
        'Erro inesperado',
        'Um erro inesperado ocorreu e o aplicativo será encerrado. '
        'Um arquivo de log foi gerado com informações sobre o erro ocorrido.'
    )


def handle_thread_error(error: Exception, app: tk.Tk | None = None):
    # tenta logar com traceback real do erro, mesmo fora de "except"
    exc_info = None
    try:
        exc_info = (type(error), error, error.__traceback__)
    except Exception:
        exc_info = None

    _write_crash_log(
        f"An unexpected error occured: {error}", exc_info=exc_info)

    _show_fatal_error_message()

    if app is not None:
        app.destroy()
