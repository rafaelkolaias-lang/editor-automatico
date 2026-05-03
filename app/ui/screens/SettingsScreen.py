import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable

from core.remote_credentials import (
    status_credencial, SLUG_OPENAI, SLUG_ASSEMBLY, SLUG_GEMINI,
)


# Mapeamento slug -> nome amigavel
_NOMES_SERVICOS = [
    ('OpenAI (GPT)', SLUG_OPENAI),
    ('AssemblyAI (transcricao)', SLUG_ASSEMBLY),
    ('Google Gemini (renomeador)', SLUG_GEMINI),
]
_SLUG_TO_NOME = {
    SLUG_OPENAI: 'OpenAI',
    SLUG_ASSEMBLY: 'AssemblyAI',
    SLUG_GEMINI: 'Gemini',
}


class SettingsScreen:
  """
  Tela informativa/status de credenciais do usuario.

  Regras:
  - Credenciais vem do servidor (manual-credenciais), nunca em disco.
  - Refresh roda em background (nao trava a UI).
  - Se alguma credencial estiver indisponivel, a tela bloqueia a saida.
  - Modo "force_block": acionado pelo startup do app quando ha pendencia.
  """
  widget: tk.Frame = None
  on_close: Callable[[], None] = None
  on_logout: Callable[[], None] = None

  def __init__(self, app: tk.Tk):
    self._app = app
    self.widget = tk.Frame(app)
    self._status_labels: dict[str, tk.Label] = {}
    self._status_slugs: dict[str, bool] = {}  # slug -> ok
    self._force_block = False
    self._checking = False

    self._title = tk.Label(self.widget, text='Credenciais do usuario',
                           font=('Arial', 12, 'bold'))
    self._title.pack(pady=(10, 4))

    self._info = tk.Label(
      self.widget,
      text=('As chaves de API sao obtidas do servidor com base no seu login.\n'
            'Elas nao sao armazenadas neste computador.'),
      justify='center'
    )
    self._info.pack(pady=(0, 6))

    # Aviso de bloqueio (inicialmente oculto)
    self._block_label = tk.Label(
      self.widget,
      text='',
      fg='#b33a3a',
      font=('Arial', 10, 'bold'),
      wraplength=420,
      justify='center'
    )
    self._block_label.pack(pady=(0, 8))

    for nome, slug in _NOMES_SERVICOS:
      row = tk.Frame(self.widget)
      row.pack(fill='x', padx=16, pady=3)
      tk.Label(row, text=f'{nome}:', width=28, anchor='w').pack(side='left')
      lbl = tk.Label(row, text='...', anchor='w', fg='#555')
      lbl.pack(side='left', fill='x', expand=True)
      self._status_labels[slug] = lbl
      self._status_slugs[slug] = False

    btn_row = tk.Frame(self.widget)
    btn_row.pack(pady=12)
    self._btn_refresh = tk.Button(btn_row, text='Atualizar status',
                                  command=self._on_click_refresh)
    self._btn_refresh.pack(side='left', padx=4)
    self._btn_close = tk.Button(btn_row, text='Voltar',
                                command=self._handle_close)
    self._btn_close.pack(side='left', padx=4)
    self._btn_logout = tk.Button(btn_row, text='Sair (deslogar)',
                                 command=self._handle_logout)
    self._btn_logout.pack(side='left', padx=4)

  # ------------------------------------------------------------------
  # Public API
  # ------------------------------------------------------------------
  def render(self, force_block: bool = False):
    self._force_block = bool(force_block)
    self._apply_block_ui()
    self.widget.pack(expand=True)
    # Refresh automatico ao abrir
    self._start_refresh(force_pending=self._force_block)

  def unrender(self):
    self.widget.pack_forget()

  def todas_ok(self) -> bool:
    return all(self._status_slugs.values())

  def pendentes(self) -> list[str]:
    return [_SLUG_TO_NOME.get(s, s)
            for s, ok in self._status_slugs.items() if not ok]

  # ------------------------------------------------------------------
  # Refresh flow
  # ------------------------------------------------------------------
  def _on_click_refresh(self):
    # Se ja esta tudo OK, faz refresh leve (usa cache).
    # Se ha pendencia, forca busca nova no servidor.
    self._start_refresh(force_pending=not self.todas_ok())

  def _start_refresh(self, force_pending: bool = False):
    if self._checking:
      return
    self._checking = True
    try:
      self._btn_refresh.config(state='disabled')
    except Exception:
      pass
    for slug, lbl in self._status_labels.items():
      try:
        lbl.config(text='verificando...', fg='#555')
      except Exception:
        pass

    def _worker():
      resultados: dict[str, tuple[bool, str]] = {}
      for _, slug in _NOMES_SERVICOS:
        # Se este slug esta pendente e o caller pediu refresh forcado,
        # consulta ignorando cache.
        usar_cache = not (force_pending and not self._status_slugs.get(slug, False))
        try:
          ok, msg = status_credencial(slug, usar_cache=usar_cache)
        except TypeError:
          # compatibilidade se a assinatura nao aceitar usar_cache
          ok, msg = status_credencial(slug)
        except Exception as e:
          ok, msg = False, f'erro ({e.__class__.__name__})'
        resultados[slug] = (ok, msg)
      self._app.after(0, lambda: self._apply_results(resultados))

    threading.Thread(target=_worker, daemon=True).start()

  def _apply_results(self, resultados: dict):
    for slug, (ok, msg) in resultados.items():
      self._status_slugs[slug] = bool(ok)
      lbl = self._status_labels.get(slug)
      if not lbl:
        continue
      if ok:
        lbl.config(text='OK — disponivel', fg='#1a7f37')
      else:
        lbl.config(text=f'indisponivel — {msg}', fg='#b33a3a')

    self._checking = False
    try:
      self._btn_refresh.config(state='normal')
    except Exception:
      pass

    self._apply_block_ui()

    # Se estava em modo bloqueado e agora todas OK, libera e fecha
    if self._force_block and self.todas_ok():
      self._force_block = False
      try:
        if self.on_close:
          self.on_close()
      except Exception:
        pass

  def _apply_block_ui(self):
    """Atualiza aviso de bloqueio conforme estado atual."""
    pendentes = self.pendentes()
    if self._force_block and pendentes:
      nomes = ', '.join(pendentes)
      self._block_label.config(
        text=(f'Credencial indisponivel: {nomes}.\n'
              'Contacte o desenvolvedor.\n'
              'Voce nao pode sair desta tela ate que todas estejam disponiveis.')
      )
    else:
      self._block_label.config(text='')

  # ------------------------------------------------------------------
  # Close / block
  # ------------------------------------------------------------------
  def _handle_logout(self):
    # Logout funciona mesmo em modo bloqueado (troca de usuario).
    if self.on_logout:
      self.on_logout()

  def _handle_close(self):
    if self._force_block and not self.todas_ok():
      pendentes = self.pendentes()
      nomes = ', '.join(pendentes) if pendentes else '(verificando)'
      messagebox.showwarning(
        'Credenciais indisponiveis',
        f'Credencial indisponivel: {nomes}.\n'
        'Contacte o desenvolvedor.\n'
        'Nao e possivel sair ate que todas estejam disponiveis.',
        parent=self._app
      )
      return
    if self.on_close:
      self.on_close()
