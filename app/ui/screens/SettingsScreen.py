import tkinter as tk
from typing import Callable

from ...managers import SettingsManager


class SettingsScreen:
  widget: tk.Frame = None

  assembly_ai_key_entry: tk.Entry = None
  assembly_ai_key_stringvar: tk.StringVar = None

  openai_api_key_entry: tk.Entry = None
  openai_api_key_stringvar: tk.StringVar = None

  on_close: Callable[[], None] = None

  settings_manager = SettingsManager()

  def __init__(self, app: tk.Tk):
    self.widget = tk.Frame(app)

    # garante que o arquivo exista e tenha as chaves novas
    self.settings = self.settings_manager.read_settings()

    settings_label = tk.Label(self.widget, text='Configurações:')
    settings_label.pack()

    # AssemblyAI
    assembly_ai_key_label = tk.Label(self.widget, text='Chave da API da AssemblyAI:')
    assembly_ai_key_label.pack()

    self.assembly_ai_key_stringvar = tk.StringVar()
    self.assembly_ai_key_stringvar.set(self.settings.get('env', {}).get('ASSEMBLY_AI_KEY', ''))

    self.assembly_ai_key_entry = tk.Entry(self.widget, textvariable=self.assembly_ai_key_stringvar)
    self.assembly_ai_key_entry.pack()

    # OpenAI
    openai_api_key_label = tk.Label(self.widget, text='Chave da API da OpenAI (GPT):')
    openai_api_key_label.pack()

    self.openai_api_key_stringvar = tk.StringVar()
    self.openai_api_key_stringvar.set(self.settings.get('env', {}).get('OPENAI_API_KEY', ''))

    # show="*" esconde a chave na tela (mais seguro)
    self.openai_api_key_entry = tk.Entry(self.widget, textvariable=self.openai_api_key_stringvar, show="*")
    self.openai_api_key_entry.pack()

    settings_btn = tk.Button(self.widget, text='Salvar e voltar', command=self.save_settings)
    settings_btn.pack()

    return_btn = tk.Button(self.widget, text='Voltar sem salvar', command=self.close_settings)
    return_btn.pack()

  def save_settings(self):
    env = self.settings.get('env', {})
    if not isinstance(env, dict):
      env = {}
      self.settings['env'] = env

    env.update({
      'ASSEMBLY_AI_KEY': self.assembly_ai_key_entry.get(),
      'OPENAI_API_KEY': self.openai_api_key_entry.get()
    })

    self.settings_manager.write_settings(self.settings)
    self.on_close()

  def close_settings(self):
    self.assembly_ai_key_stringvar.set(self.settings.get('env', {}).get('ASSEMBLY_AI_KEY', ''))
    self.openai_api_key_stringvar.set(self.settings.get('env', {}).get('OPENAI_API_KEY', ''))
    self.on_close()

  def render(self):
    self.widget.pack(expand=True)

  def unrender(self):
    self.widget.pack_forget()