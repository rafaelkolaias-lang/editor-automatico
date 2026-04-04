import tkinter as tk

class WorkingScreen:
  widget: tk.Frame
  WAIT_MESSAGE = '\n'.join([
    'O programa iniciou o processo de transcrição da(s) narração(ões) e, em seguida, iniciará a edição no Adobe Premiere.',
    'Este processo pode demorar bastante, então por favor aguarde.',
    'Caso apareçam alguns erros de importação, por favor clique em "Ok" em todos e continue aguardando.',
    'O programa tentará converter e reimportar os arquivos automaticamente.'
  ])

  def __init__(self, app: tk.Tk):
    self.widget = tk.Frame(app)

    wait_label = tk.Label(self.widget, text=self.WAIT_MESSAGE)
    wait_label.pack(expand=True)

  def render(self):
    self.widget.pack(expand=True)

  def unrender(self):
    self.widget.pack_forget()
