import tkinter as tk
import sys
import threading


class WorkingScreen:
    widget: tk.Frame

    def __init__(self, app: tk.Tk):
        self.app = app
        self.widget = tk.Frame(app)

        header = tk.Label(
            self.widget,
            text='Processando... aguarde.\nClique "Ok" em erros de importacao.',
            font=('Arial', 11, 'bold'),
            pady=12
        )
        header.pack(expand=True)

    def render(self):
        self.widget.pack(expand=True, fill='both')

    def unrender(self):
        self.widget.pack_forget()


class TerminalPopup:
    """Terminal persistente em popup. Captura stdout/stderr sempre.

    Thread-safe: prints vindos de threads worker (Premiere/FFmpeg/Impact)
    sao agendados na main thread via app.after(0, ...) porque Tkinter NAO
    suporta atualizacao de widgets fora da main thread.
    """

    def __init__(self, app: tk.Tk):
        self.app = app
        self._window = None
        self._text = None
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self._buffer = ''
        self._main_thread_id = threading.get_ident()
        self._closed = False

        # Redireciona stdout/stderr permanentemente
        sys.stdout = _TerminalWriter(self._write, self._original_stdout)
        sys.stderr = _TerminalWriter(self._write, self._original_stderr)

    def _create_window(self):
        if self._window is not None and self._window.winfo_exists():
            return

        self._window = tk.Toplevel(self.app)
        self._window.title('Terminal')
        self._window.geometry('750x420')
        self._window.protocol('WM_DELETE_WINDOW', self._hide)

        frame = tk.Frame(self._window)
        frame.pack(expand=True, fill='both')

        self._text = tk.Text(
            frame,
            bg='#1e1e1e',
            fg='#cccccc',
            font=('Consolas', 9),
            wrap='word',
            state='disabled',
            relief='sunken',
            borderwidth=2
        )
        scrollbar = tk.Scrollbar(frame, command=self._text.yview)
        scrollbar.pack(side='right', fill='y')
        self._text.configure(yscrollcommand=scrollbar.set)
        self._text.pack(expand=True, fill='both', padx=4, pady=4)

        btn_frame = tk.Frame(self._window)
        tk.Button(btn_frame, text='Fechar', command=self._hide).pack(side='left', padx=4)
        btn_frame.pack(pady=(0, 4))

        # Insere o historico acumulado
        if hasattr(self, '_buffer'):
            self._text.configure(state='normal')
            self._text.insert('end', self._buffer)
            self._text.see('end')
            self._text.configure(state='disabled')

    def show(self):
        self._create_window()
        self._window.deiconify()
        self._window.lift()
        self._window.focus_force()

    def _hide(self):
        if self._window and self._window.winfo_exists():
            self._window.withdraw()

    def close(self):
        """Restaura stdout/stderr e destroi a janela. Usado no shutdown do app."""
        self._closed = True
        try:
            sys.stdout = self._original_stdout
            sys.stderr = self._original_stderr
        except Exception:
            pass
        try:
            if self._window and self._window.winfo_exists():
                self._window.destroy()
        except Exception:
            pass
        self._window = None
        self._text = None

    def _clear(self):
        if self._text:
            self._text.configure(state='normal')
            self._text.delete('1.0', 'end')
            self._text.configure(state='disabled')
        self._buffer = ''

    def _write(self, text: str):
        """Recebe texto do _TerminalWriter (de qualquer thread).

        Acumula no buffer (sempre seguro) e agenda a insercao no widget
        na main thread. Tkinter NAO eh thread-safe; chamar text.insert
        de uma thread worker falha silenciosamente, e era a causa raiz
        de logs aparecerem no CMD mas nao no terminal GUI.
        """
        if self._closed:
            return
        # Buffer eh sempre acumulado (lido pela main thread quando a janela
        # for criada). Concatenacao de string eh thread-safe em CPython
        # (GIL), entao nao precisa de lock aqui.
        self._buffer += text

        # Se ja estamos na main thread, escreve direto (mais rapido).
        if threading.get_ident() == self._main_thread_id:
            self._write_to_widget(text)
            return

        # Caso contrario, agenda na main thread via app.after(0, ...).
        try:
            self.app.after(0, lambda t=text: self._write_to_widget(t))
        except Exception:
            # app pode estar sendo destruido — ignora silenciosamente
            pass

    def _write_to_widget(self, text: str):
        """Insere texto no widget Text. SEMPRE roda na main thread."""
        if self._closed:
            return
        if not self._text or not self._window:
            return
        try:
            if not self._window.winfo_exists():
                return
            self._text.configure(state='normal')
            self._text.insert('end', text)
            self._text.see('end')
            self._text.configure(state='disabled')
        except Exception:
            pass


class _TerminalWriter:
    """Redireciona escrita para o terminal e para o stdout/stderr original."""

    def __init__(self, write_fn, original):
        self._write_fn = write_fn
        self._original = original

    def write(self, text):
        if text:
            if self._original:
                try:
                    self._original.write(text)
                except Exception:
                    pass
            try:
                self._write_fn(text)
            except Exception:
                pass

    def flush(self):
        if self._original:
            try:
                self._original.flush()
            except Exception:
                pass
