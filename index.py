import os
import sys
import threading
import tempfile
from app.utils.ffmpeg_path import get_ffmpeg_bin

# Garante que moviepy/imageio usem o ffmpeg bundled (antes de qualquer import de moviepy)
os.environ.setdefault('IMAGEIO_FFMPEG_EXE', get_ffmpeg_bin())

import tkinter as tk
from tkinter import ttk, messagebox

from app.ui import InitialScreen, MainScreen, SettingsScreen, WorkingScreen
from app.ui.screens.WorkingScreen import TerminalPopup
from app.managers import PremiereManager
from app.utils import get_error_handler, debug_print

from core.auth import RepositorioAuth, ler_login_salvo, salvar_login
from core.updater import verificar_atualizacao, baixar_zip, instalar_atualizacao
from core.remote_credentials import (
    set_credenciais_usuario, limpar_cache,
    status_credencial, SLUG_OPENAI, SLUG_ASSEMBLY, SLUG_GEMINI,
)
from app.__version__ import VERSAO, VERSAO_APLICACAO

# Flag global de encerramento — evita callbacks atrasados rodarem apos _on_close.
_app_closing = False

# ── App principal ───────────────────────────────────────────────
app = tk.Tk()
app.title(f'Automatizador do Premiere {VERSAO}')
app.geometry('480x520')
app.configure(bg='#0d1117')

# Telas (criadas mas nao renderizadas ate apos o login)
initial_screen = InitialScreen(app)
main_screen = MainScreen(app)
settings_screen = SettingsScreen(app)
working_screen = WorkingScreen(app)

# Terminal persistente (captura stdout/stderr desde o inicio)
terminal_popup = TerminalPopup(app)

premiere = PremiereManager()

tk.Tk.report_callback_exception = get_error_handler(app)

# ── Auth ────────────────────────────────────────────────────────
_auth = RepositorioAuth()
_usuario = None
_login_frame = None

_var_user = None
_var_chave = None
_var_status_login = None

# ── Frames de update ───────────────────────────────────────────
_update_frame = None
_update_status_var = None
_update_progress = None


# ================================================================
#  LOGIN UI
# ================================================================

def _montar_tela_login():
    global _login_frame, _var_user, _var_chave, _var_status_login

    _var_user = tk.StringVar()
    _var_chave = tk.StringVar()
    _var_status_login = tk.StringVar()

    _login_frame = tk.Frame(app, bg='#0d1117')
    _login_frame.pack(expand=True, fill='both')

    # Card central
    card = tk.Frame(_login_frame, bg='#161b22', padx=40, pady=30)
    card.place(relx=0.5, rely=0.5, anchor='center')

    tk.Label(card, text='Automatizador do Premiere',
             font=('Segoe UI', 18, 'bold'), fg='#e6edf3', bg='#161b22').pack(pady=(0, 4))

    tk.Label(card, text='Faca login para continuar',
             font=('Segoe UI', 10), fg='#8b949e', bg='#161b22').pack(pady=(0, 24))

    # Usuario
    tk.Label(card, text='USUARIO', font=('Segoe UI', 9, 'bold'),
             fg='#8b949e', bg='#161b22', anchor='w').pack(fill='x', pady=(0, 4))
    entry_user = tk.Entry(card, textvariable=_var_user, font=('Segoe UI', 11),
                          bg='#0d1117', fg='#e6edf3', insertbackground='#e6edf3',
                          relief='flat', bd=0, highlightthickness=1,
                          highlightbackground='#30363d', highlightcolor='#1b6ef3')
    entry_user.pack(fill='x', ipady=8, pady=(0, 16))

    # Chave
    tk.Label(card, text='CHAVE', font=('Segoe UI', 9, 'bold'),
             fg='#8b949e', bg='#161b22', anchor='w').pack(fill='x', pady=(0, 4))
    entry_chave = tk.Entry(card, textvariable=_var_chave, show='*', font=('Segoe UI', 11),
                           bg='#0d1117', fg='#e6edf3', insertbackground='#e6edf3',
                           relief='flat', bd=0, highlightthickness=1,
                           highlightbackground='#30363d', highlightcolor='#1b6ef3')
    entry_chave.pack(fill='x', ipady=8, pady=(0, 24))

    # Botao Entrar
    btn = tk.Button(card, text='Entrar', font=('Segoe UI', 11, 'bold'),
                    bg='#1b6ef3', fg='white', activebackground='#1558c9',
                    activeforeground='white', relief='flat', cursor='hand2',
                    command=_logar, padx=20, pady=6)
    btn.pack(fill='x', ipady=4)

    # Status
    tk.Label(card, textvariable=_var_status_login, font=('Segoe UI', 9),
             fg='#f85149', bg='#161b22', wraplength=320).pack(pady=(16, 0))

    # Enter dispara login
    entry_chave.bind('<Return>', lambda _: _logar())
    entry_user.bind('<Return>', lambda _: entry_chave.focus_set())

    entry_user.focus_set()


_skip_auto_login = False  # True apos logout manual: abre login preenchido sem auto-autenticar


def _init_login():
    """Tenta auto-login com credenciais salvas.
    Se _skip_auto_login estiver ativo (apos logout manual), apenas preenche
    os campos sem disparar autenticacao automatica.
    """
    global _skip_auto_login
    salvo = ler_login_salvo()
    if not salvo:
        _montar_tela_login()
        return

    _montar_tela_login()
    _var_user.set(salvo['user_id'])
    _var_chave.set(salvo['chave'])

    if _skip_auto_login:
        _skip_auto_login = False
        _var_status_login.set('')
        return

    _var_status_login.set('Verificando...')

    def _tentar():
        try:
            resultado = _auth.autenticar_usuario(salvo['user_id'], salvo['chave'])
        except Exception:
            resultado = None

        if resultado:
            app.after(0, lambda: _login_sucesso(resultado, salvo['user_id'], salvo['chave']))
        else:
            app.after(0, lambda: _var_status_login.set('Sessao expirada. Faca login novamente.'))

    threading.Thread(target=_tentar, daemon=True).start()


def _logout():
    """
    Encerra a sessao atual e volta para a tela de login.
    Mantem o arquivo ~/.credenciais_rk.json para pre-preencher os campos,
    mas NAO dispara auto-login (usuario precisa clicar em Entrar).
    """
    global _usuario, _skip_auto_login
    debug_print('UI', 'Logout solicitado')

    # Limpa cache de credenciais remotas
    try:
        limpar_cache()
    except Exception:
        pass
    _usuario = None

    # Desrenderiza telas ativas
    for tela in (main_screen, initial_screen, settings_screen, working_screen):
        try:
            tela.unrender()
        except Exception:
            pass

    # Volta janela ao tamanho de login
    app.geometry('480x520')
    app.configure(bg='#0d1117')

    _skip_auto_login = True
    _init_login()


def _logar():
    uid = _var_user.get().strip()
    chave = _var_chave.get().strip()

    if not uid or not chave:
        _var_status_login.set('Preencha usuario e chave.')
        return

    _var_status_login.set('Autenticando...')

    def _validar():
        try:
            resultado = _auth.autenticar_usuario(uid, chave)
        except Exception as e:
            app.after(0, lambda: _var_status_login.set(f'Erro de conexao: {e}'))
            return

        if resultado:
            app.after(0, lambda: _login_sucesso(resultado, uid, chave))
        else:
            app.after(0, lambda: _var_status_login.set('Usuario ou chave invalidos.'))

    threading.Thread(target=_validar, daemon=True).start()


def _login_sucesso(resultado, uid, chave):
    global _usuario
    _usuario = resultado
    salvar_login(uid, chave)
    set_credenciais_usuario(uid, chave)
    _apos_login()


def _apos_login():
    """Remove tela de login e segue o fluxo (update check → tela inicial)."""
    global _login_frame
    if _login_frame:
        _login_frame.destroy()
        _login_frame = None

    if getattr(sys, 'frozen', False):
        _verificar_update()
    else:
        _ir_para_tela_inicial()


# ================================================================
#  AUTO-UPDATE
# ================================================================

def _verificar_update():
    """Verifica atualizacao em thread separada."""
    global _update_frame, _update_status_var, _update_progress

    _update_frame = tk.Frame(app, bg='#0d1117')
    _update_frame.pack(expand=True, fill='both')

    card = tk.Frame(_update_frame, bg='#161b22', padx=40, pady=30)
    card.place(relx=0.5, rely=0.5, anchor='center')

    tk.Label(card, text='Verificando atualizacoes...',
             font=('Segoe UI', 12), fg='#e6edf3', bg='#161b22').pack(pady=(0, 16))

    _update_status_var = tk.StringVar(value='Consultando servidor...')
    tk.Label(card, textvariable=_update_status_var, font=('Segoe UI', 9),
             fg='#8b949e', bg='#161b22', wraplength=320).pack(pady=(0, 12))

    style = ttk.Style()
    style.theme_use('default')
    style.configure('Update.Horizontal.TProgressbar',
                    troughcolor='#0d1117', background='#1b6ef3',
                    thickness=8)

    _update_progress = ttk.Progressbar(card, length=300, mode='determinate',
                                        style='Update.Horizontal.TProgressbar')
    _update_progress.pack(pady=(0, 8))
    _update_progress['value'] = 0

    def _checar():
        info, erro = verificar_atualizacao(VERSAO_APLICACAO)

        if erro:
            app.after(0, lambda: _update_status_var.set(erro))
            app.after(2000, _finalizar_update)
            return

        if info is None:
            app.after(0, lambda: _update_status_var.set('Voce ja esta na versao mais recente.'))
            app.after(1500, _finalizar_update)
            return

        # Ha atualizacao disponivel
        url_zip = info.get('url', '')
        versao_nova = info.get('versao', '?')
        app.after(0, lambda: _update_status_var.set(
            f'Atualizacao v{versao_nova} disponivel. Baixando...'))

        zip_dest = os.path.join(tempfile.gettempdir(),
                                info.get('arquivo', 'update.zip'))

        def _progresso(baixados, total):
            if total > 0:
                pct = min(baixados / total * 100, 100)
                app.after(0, lambda p=pct: _update_progress.configure(value=p))

        try:
            baixar_zip(url_zip, zip_dest, callback=_progresso)
        except Exception as e:
            app.after(0, lambda: _update_status_var.set(
                f'Falha no download: {e}'))
            app.after(3000, _finalizar_update)
            return

        app.after(0, lambda: _update_status_var.set('Instalando atualizacao...'))

        try:
            instalar_atualizacao(zip_dest, status_callback=lambda msg:
                app.after(0, lambda m=msg: _update_status_var.set(m)))
        except Exception as e:
            app.after(0, lambda: _update_status_var.set(
                f'Falha na instalacao: {e}'))
            app.after(3000, _finalizar_update)

    threading.Thread(target=_checar, daemon=True).start()


def _finalizar_update():
    global _update_frame
    if _update_frame:
        _update_frame.destroy()
        _update_frame = None
    _ir_para_tela_inicial()


# ================================================================
#  FLUXO ORIGINAL (pos-login)
# ================================================================

def _ir_para_tela_inicial():
    """Redimensiona janela e mostra InitialScreen + auto-check.
    Antes de liberar o fluxo normal, valida se todas as 3 credenciais
    obrigatorias estao disponiveis. Se faltar alguma, abre SettingsScreen
    em modo bloqueado ate que o problema seja resolvido.
    """
    app.geometry('800x620')
    app.configure(bg='SystemButtonFace')

    def _worker():
      # Aquece cache e coleta status
      pendentes = []
      for slug in (SLUG_OPENAI, SLUG_ASSEMBLY, SLUG_GEMINI):
        try:
          ok, _ = status_credencial(slug)
        except Exception:
          ok = False
        if not ok:
          pendentes.append(slug)
      app.after(0, lambda: _apos_checar_credenciais(pendentes))

    threading.Thread(target=_worker, daemon=True).start()


def _apos_checar_credenciais(pendentes: list):
    """Decide se entra no fluxo normal ou bloqueia em SettingsScreen."""
    if pendentes:
      debug_print('Credenciais', 'pendentes no startup', pendentes=pendentes)
      # Abre SettingsScreen em modo bloqueado
      global _settings_opened_from
      _settings_opened_from = 'initial'
      settings_screen.render(force_block=True)
      return

    # Tudo OK — fluxo normal
    initial_screen.render()
    app.after(100, _auto_check_premiere)


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
        app.after(100, _auto_check_premiere)


def on_working():
    debug_print('UI', 'Entrando na tela Working')
    main_screen.unrender()
    working_screen.render()
    # Abre o terminal automaticamente ao iniciar processamento
    terminal_popup.show()


def on_working_done():
    if _app_closing:
        return
    debug_print('UI', 'Saindo da tela Working')
    working_screen.unrender()
    main_screen.render()
    # Terminal continua acessivel pelo menu, nao fecha


def _open_renamer_from_initial():
    from app.ui.screens.RenamerFeedbackScreen import RenamerFeedbackScreen
    RenamerFeedbackScreen(app, main_screen.settings_manager)


initial_screen.on_proceed = on_initial_screen_proceed
initial_screen.on_open_renamer = _open_renamer_from_initial
initial_screen.on_open_settings = on_open_settings
initial_screen.on_logout = _logout
settings_screen.on_close = on_close_settings
settings_screen.on_logout = _logout
main_screen.on_open_settings = on_open_settings
main_screen.on_logout = _logout
main_screen.on_working = on_working
main_screen.on_working_done = on_working_done

# Passa referencia do terminal para o MainScreen poder adicionar ao menu
main_screen._terminal_popup = terminal_popup


def _auto_check_premiere():
    """Verifica automaticamente se o Premiere esta pronto ao iniciar."""
    initial_screen.set_status('Verificando conexao com o Premiere...')
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


def _on_close():
    """Encerra o processo inteiro: limpa cache, restaura stdout/stderr, mata workers."""
    global _app_closing
    _app_closing = True

    try:
        limpar_cache()
    except Exception:
        pass

    try:
        terminal_popup.close()
    except Exception:
        pass

    try:
        app.quit()
    except Exception:
        pass

    try:
        app.destroy()
    except Exception:
        pass

    os._exit(0)


app.protocol('WM_DELETE_WINDOW', _on_close)

# ── Inicia o fluxo pelo login ──────────────────────────────────
_init_login()

app.mainloop()
