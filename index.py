import tkinter as tk
from tkinter import messagebox

from app.ui import InitialScreen, MainScreen, SettingsScreen, WorkingScreen
from app.managers import PremiereManager
from app.utils import get_error_handler, debug_print

app = tk.Tk()
app.title('Automatizador do Premiere')
app.geometry('640x720')

initial_screen = InitialScreen(app)
main_screen = MainScreen(app)
settings_screen = SettingsScreen(app)
working_screen = WorkingScreen(app)

premiere = PremiereManager()

tk.Tk.report_callback_exception = get_error_handler(app)


def on_initial_screen_proceed():
    debug_print('UI', 'InitialScreen: Prosseguir clicado')
    status = premiere.get_status()
    debug_print('Premiere', 'get_status() retornou', status=status)

    if status == 'PLUGIN_NOT_INSTALLED':
        debug_print('UI', 'Bloqueado: plugin não instalado')
        return messagebox.showerror(
            'Erro: Plugin não instalado',
            'O plugin que conecta o Adobe Premiere ao Python não foi instalado. Verifique a descrição do projeto para instalá-lo.'
        )

    if status == 'PREMIERE_NOT_OPEN':
        debug_print('UI', 'Bloqueado: Premiere fechado')
        return messagebox.showerror(
            'Erro: Premiere fechado',
            'O Adobe Premiere Pro deve estar aberto.'
        )

    if status == 'PROJECT_NOT_OPEN':
        debug_print('UI', 'Bloqueado: projeto não aberto')
        return messagebox.showerror(
            'Erro: Projeto fechado',
            'Um projeto deve estar aberto.'
        )

    debug_print('UI', 'Trocando tela', de='InitialScreen', para='MainScreen')
    initial_screen.unrender()
    main_screen.render()


def on_open_settings():
    debug_print('UI', 'Abrindo Settings')
    main_screen.unrender()
    settings_screen.render()


def on_close_settings():
    debug_print('UI', 'Fechando Settings')
    settings_screen.unrender()
    main_screen.render()


def on_working():
    debug_print('UI', 'Entrando na tela Working')
    main_screen.unrender()
    working_screen.render()


def on_working_done():
    debug_print('UI', 'Saindo da tela Working')
    working_screen.unrender()
    main_screen.render()


initial_screen.on_proceed = on_initial_screen_proceed
settings_screen.on_close = on_close_settings
main_screen.on_open_settings = on_open_settings
main_screen.on_working = on_working
main_screen.on_working_done = on_working_done

initial_screen.render()

app.mainloop()
