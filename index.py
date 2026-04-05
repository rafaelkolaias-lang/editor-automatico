import tkinter as tk
from tkinter import messagebox

from app.ui import InitialScreen, MainScreen, SettingsScreen, WorkingScreen
from app.ui.screens.WorkingScreen import TerminalPopup
from app.managers import PremiereManager
from app.utils import get_error_handler, debug_print

app = tk.Tk()
app.title('Automatizador do Premiere 2.0')
app.geometry('800x620')

initial_screen = InitialScreen(app)
main_screen = MainScreen(app)
settings_screen = SettingsScreen(app)
working_screen = WorkingScreen(app)

# Terminal persistente (captura stdout/stderr desde o inicio)
terminal_popup = TerminalPopup(app)

premiere = PremiereManager()

tk.Tk.report_callback_exception = get_error_handler(app)


def on_initial_screen_proceed():
    debug_print('UI', 'InitialScreen: Prosseguir clicado')
    status = premiere.get_status()
    debug_print('Premiere', 'get_status() retornou', status=status)

    if status == 'PLUGIN_NOT_INSTALLED':
        debug_print('UI', 'Bloqueado: plugin nao instalado')
        return messagebox.showerror(
            'Erro: Plugin nao instalado',
            'O plugin que conecta o Adobe Premiere ao Python nao foi instalado. Verifique a descricao do projeto para instala-lo.'
        )

    if status == 'PREMIERE_NOT_OPEN':
        debug_print('UI', 'Bloqueado: Premiere fechado')
        return messagebox.showerror(
            'Erro: Premiere fechado',
            'O Adobe Premiere Pro deve estar aberto.'
        )

    if status == 'PROJECT_NOT_OPEN':
        debug_print('UI', 'Bloqueado: projeto nao aberto')
        return messagebox.showerror(
            'Erro: Projeto fechado',
            'Um projeto deve estar aberto.'
        )

    debug_print('UI', 'Trocando tela', de='InitialScreen', para='MainScreen')
    initial_screen.unrender()
    main_screen.render()


_settings_opened_from = 'initial'  # 'initial' ou 'main'

def on_open_settings():
    global _settings_opened_from
    debug_print('UI', 'Abrindo Settings')
    # Detectar de qual tela veio
    if main_screen.widget.winfo_ismapped():
        _settings_opened_from = 'main'
        main_screen.unrender()
    else:
        _settings_opened_from = 'initial'
        initial_screen.unrender()
    settings_screen.render()


def on_close_settings():
    debug_print('UI', 'Fechando Settings')
    settings_screen.unrender()
    if _settings_opened_from == 'main':
        main_screen.render()
    else:
        initial_screen.render()


def on_working():
    debug_print('UI', 'Entrando na tela Working')
    main_screen.unrender()
    working_screen.render()
    # Abre o terminal automaticamente ao iniciar processamento
    terminal_popup.show()


def on_working_done():
    debug_print('UI', 'Saindo da tela Working')
    working_screen.unrender()
    main_screen.render()
    # Terminal continua acessivel pelo menu, nao fecha


def _open_renamer_from_initial():
    from app.ui.screens.RenamerFeedbackScreen import RenamerFeedbackScreen
    RenamerFeedbackScreen(app, main_screen.settings_manager)


initial_screen.on_proceed = on_initial_screen_proceed
initial_screen.on_open_renamer = _open_renamer_from_initial
settings_screen.on_close = on_close_settings
main_screen.on_open_settings = on_open_settings
main_screen.on_working = on_working
main_screen.on_working_done = on_working_done

# Passa referencia do terminal para o MainScreen poder adicionar ao menu
main_screen._terminal_popup = terminal_popup


def _auto_check_premiere():
    """Verifica automaticamente se o Premiere está pronto ao iniciar."""
    initial_screen.set_status('Verificando conexão com o Premiere...')
    try:
        status = premiere.get_status()
        debug_print('Premiere', 'auto-check status:', status=status)
        if status == 'READY':
            debug_print('UI', 'Auto-check: Premiere pronto, pulando tela inicial')
            initial_screen.unrender()
            main_screen.render()
            return
    except Exception:
        pass
    initial_screen.set_status('Para prosseguir, o Adobe Premiere Pro precisa estar aberto em um projeto.')


initial_screen.render()
# Auto-check apos a tela ser renderizada (100ms delay para a UI aparecer)
app.after(100, _auto_check_premiere)

app.mainloop()
