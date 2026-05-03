import tkinter as tk
from tkinter import messagebox
from typing import Callable, Optional

from ...__version__ import VERSAO


def _show_release_notes_initial(parent):
    """Janela com notas de atualizacao (clone simples — InitialScreen)."""
    import os
    from ...managers.SettingsManager import get_runtime_root
    notes_path = os.path.join(get_runtime_root(), "assets", "release_notes.txt")
    try:
        with open(notes_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        content = f"Arquivo de notas nao encontrado.\n\nPath: {notes_path}\n\nErro: {e}"
    win = tk.Toplevel(parent)
    win.title("Notas de Atualização")
    win.geometry("620x500")
    text = tk.Text(win, wrap="word", bg="#1e1e1e", fg="#d4d4d4",
                   font=("Consolas", 10), padx=12, pady=12)
    sb = tk.Scrollbar(win, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    text.pack(fill="both", expand=True)
    text.insert("1.0", content)
    text.configure(state="disabled")


class InitialScreen:
  on_proceed: Callable[[], None] = None
  on_open_renamer: Optional[Callable[[], None]] = None
  on_open_settings: Optional[Callable[[], None]] = None
  on_logout: Optional[Callable[[], None]] = None
  widget: tk.Frame = None

  def __init__(self, app: tk.Tk):
    self._app = app
    self.widget = tk.Frame(app)

    # ========== MENUBAR (especifico da InitialScreen) ==========
    self._menubar = self._build_menubar(app)

    self.status_label = tk.Label(self.widget, text='Verificando conexão com o Premiere...', font=('Arial', 10))
    self.status_label.pack(pady=(20, 8))

    btn_row = tk.Frame(self.widget)

    self.proceed_btn = tk.Button(btn_row, text='Prosseguir',
                                  command=lambda: (self.on_proceed()))
    self.proceed_btn.pack(side='left', padx=(0, 8))

    self.renamer_btn = tk.Button(btn_row, text='Renomear Cenas',
                                  font=('Arial', 10, 'bold'), bg='#5C2D91', fg='white',
                                  padx=20, pady=4,
                                  command=lambda: (self.on_open_renamer() if self.on_open_renamer else None))
    self.renamer_btn.pack(side='left')

    btn_row.pack(pady=(4, 12))

  def _build_menubar(self, app: tk.Tk) -> tk.Menu:
    menubar = tk.Menu(app)

    menu_opcoes = tk.Menu(menubar, tearoff=0)
    menu_opcoes.add_command(label='Credenciais',
                            command=lambda: (self.on_open_settings() if self.on_open_settings else None))
    menu_opcoes.add_separator()
    menu_opcoes.add_command(label='Instalar pymiere', command=self._open_install_dialog)
    menubar.add_cascade(label='Opcoes', menu=menu_opcoes)

    menu_ajuda = tk.Menu(menubar, tearoff=0)
    menu_ajuda.add_command(label='Notas de Atualização',
                           command=lambda: _show_release_notes_initial(app))
    menu_ajuda.add_command(label='Sobre', command=lambda: messagebox.showinfo(
        'Sobre', f'Automatizador do Premiere {VERSAO}\nDesenvolvido por Kolaias', parent=app))
    menubar.add_cascade(label='Ajuda', menu=menu_ajuda)

    menubar.add_command(label='Sair',
                        command=lambda: (self.on_logout() if self.on_logout else None))
    return menubar

  def _open_install_dialog(self):
    try:
      from ...utils.pymiere_installer import open_install_dialog
      open_install_dialog(self._app)
    except Exception as e:
      messagebox.showerror(
        'Instalar pymiere',
        f'Falha ao abrir o instalador:\n{type(e).__name__}: {e}',
        parent=self._app,
      )

  def set_status(self, text: str):
    try:
        self.status_label.config(text=text)
        self.widget.update_idletasks()
    except Exception:
        pass

  def render(self):
    try:
        self._app.config(menu=self._menubar)
    except Exception:
        pass
    self.widget.pack(expand=True)

  def unrender(self):
    self.widget.pack_forget()
