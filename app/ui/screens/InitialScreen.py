import tkinter as tk
from typing import Callable

class InitialScreen:
  on_proceed: Callable[[], None] = None
  widget: tk.Frame = None

  def __init__(self, app: tk.Tk):
    self.widget = tk.Frame(app)

    proceed_label = tk.Label(self.widget, text='Para prosseguir, o Adobe Premiere Pro precisa estar aberto em um projeto.')
    proceed_label.pack()

    proceed_btn = tk.Button(self.widget, text='Prosseguir', command=lambda: (self.on_proceed()))
    proceed_btn.pack()

  def render(self):
    self.widget.pack(expand=True)

  def unrender(self):
    self.widget.pack_forget()


