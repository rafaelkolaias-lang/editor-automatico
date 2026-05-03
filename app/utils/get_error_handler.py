import tkinter as tk
from .handle_thread_error import _write_crash_log, _show_fatal_error_message


# Erros que sao apenas "widget ja destruido" — acontecem quando um callback
# agendado via after() dispara depois da janela ser fechada. Sao cosmeticos;
# nao devem derrubar o app nem mostrar popup fatal.
_IGNORE_SUBSTRINGS = (
    "invalid command name",
    "application has been destroyed",
    "can't invoke",
)


def _is_benign_tcl_error(exc_value: BaseException) -> bool:
    if not isinstance(exc_value, tk.TclError):
        return False
    msg = str(exc_value).lower()
    return any(s in msg for s in _IGNORE_SUBSTRINGS)


def get_error_handler(app: tk.Tk):
    def handle_error(self, *args):
        exc_info = args if len(args) == 3 else None
        exc_value = exc_info[1] if exc_info else None

        # Ignora erros de widgets ja destruidos (callbacks agendados apos
        # fechamento da janela). So loga no crash log sem popup nem destroy.
        if exc_value is not None and _is_benign_tcl_error(exc_value):
            try:
                _write_crash_log("Tk callback after window destroyed (ignorado)",
                                 exc_info=exc_info)
            except Exception:
                pass
            return

        try:
            _write_crash_log("Unhandled exception (Tk callback)", exc_info=exc_info)
        except Exception:
            pass
        try:
            _show_fatal_error_message()
        except Exception:
            pass

        # Guard: app pode ja ter sido destruido por outro caminho.
        try:
            if app.winfo_exists():
                app.destroy()
        except Exception:
            pass

    return handle_error
