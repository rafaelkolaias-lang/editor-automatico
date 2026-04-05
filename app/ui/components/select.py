import tkinter as tk

from typing import List

DEFAULT_OPTION_TEXT = 'Selecione uma opção'
NO_AVAIABLE_OPTIONS_TEXT = 'Não há opções disponíveis'


class SelectComponent:
    # Textos padrão (como class attributes, pra você poder usar self.DEFAULT_OPTION_TEXT sem erro)
    DEFAULT_OPTION_TEXT = "Selecione uma opção"
    NO_OPTIONS_TEXT = "Não há opções disponíveis"

    def __init__(self, parent: tk.Widget, label_text: str, *, no_options_text: str | None = None, horizontal: bool = False):
        self.label_text = label_text

        self.DEFAULT_OPTION_TEXT = getattr(
            self, "DEFAULT_OPTION_TEXT", "Selecione uma opcao")
        self.NO_OPTIONS_TEXT = getattr(
            self, "NO_OPTIONS_TEXT", "Nao ha opcoes disponiveis")

        self.no_options_text = (
            no_options_text or "").strip() or self.NO_OPTIONS_TEXT

        self.widget = tk.Frame(parent)

        self.label = tk.Label(self.widget, text=self.label_text)
        if horizontal:
            self.label.pack(side="left", padx=(0, 4))
        else:
            self.label.pack(anchor="w")

        self.options: list[str] = []
        self.selected_option = tk.StringVar(value=self.DEFAULT_OPTION_TEXT)

        self.select = tk.OptionMenu(
            self.widget, self.selected_option, self.DEFAULT_OPTION_TEXT)
        if horizontal:
            self.select.pack(side="left", fill="x", expand=True)
        else:
            self.select.pack(fill="x")

    def set_options(self, options: list[str] | None, *, selected: str | None = None):
        """
        Atualiza as opções do dropdown.
        - options: lista de strings
        - selected: (opcional) valor que deve ficar selecionado, se existir nas opções
        """
        self.options = list(options or [])

        # normaliza seleção desejada
        selected = (selected or "").strip()

        menu = self.select["menu"]
        menu.delete(0, "end")

        # Sem opções -> trava no texto "sem opções"
        if not self.options:
            self.selected_option.set(self.no_options_text)
            menu.add_command(label=self.no_options_text, command=tk._setit(
                self.selected_option, self.no_options_text))
            return

        # Se veio "selected" e ele existe, seleciona ele
        if selected and selected in self.options:
            self.selected_option.set(selected)
        else:
            # se o valor atual é inválido, volta pro default
            cur = (self.selected_option.get() or "").strip()
            if cur in ("", self.DEFAULT_OPTION_TEXT, self.NO_OPTIONS_TEXT, self.no_options_text) or cur not in self.options:
                self.selected_option.set(self.DEFAULT_OPTION_TEXT)

        # Primeiro item: placeholder (default)
        menu.add_command(label=self.DEFAULT_OPTION_TEXT, command=tk._setit(
            self.selected_option, self.DEFAULT_OPTION_TEXT))

        # Depois: opções reais
        for opt in self.options:
            menu.add_command(label=opt, command=tk._setit(
                self.selected_option, opt))

    def get_selected_option(self) -> str:
        cur = (self.selected_option.get() or "").strip()
        if cur in ("", self.DEFAULT_OPTION_TEXT, self.NO_OPTIONS_TEXT, self.no_options_text):
            return ""
        return cur

    def render(self):
        self.widget.pack(pady=8, fill="x")

    def unrender(self):
        self.widget.pack_forget()
