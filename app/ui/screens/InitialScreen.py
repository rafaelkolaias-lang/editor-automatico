import tkinter as tk
from typing import Callable, Optional


class InitialScreen:
  on_proceed: Callable[[], None] = None
  on_open_renamer: Optional[Callable[[], None]] = None
  widget: tk.Frame = None

  def __init__(self, app: tk.Tk):
    self.widget = tk.Frame(app)

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

  def set_status(self, text: str):
    try:
        self.status_label.config(text=text)
        self.widget.update_idletasks()
    except Exception:
        pass

  def render(self):
    self.widget.pack(expand=True)

  def unrender(self):
    self.widget.pack_forget()
