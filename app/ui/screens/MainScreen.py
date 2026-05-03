from threading import Thread
import tkinter as tk
from tkinter import messagebox, filedialog
from ...utils import debug_print  # <-- ADICIONAR ESTA LINHA
from typing import Callable
from os import path
import os
import time

from ...utils import create_renamed_file, handle_thread_error
from ...entities import Part
from ...entities import EXTENSIONS
from ..components import SelectComponent
from ...managers import ConversionManager, DirectoriesManager, PremiereManager, SettingsManager, TranscriptionManager
from ...managers.SettingsManager import get_runtime_root
from ...__version__ import VERSAO


class _Tip:
    """Tooltip ao passar o mouse."""
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self._tw = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _e=None):
        if self._tw:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self._tw = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left",
                 bg="#ffffe0", fg="#333", relief="solid", borderwidth=1,
                 font=("Arial", 9), padx=6, pady=4).pack()

    def _hide(self, _e=None):
        if self._tw:
            self._tw.destroy()
            self._tw = None


def _tip_label(parent, tip_text: str):
    """Cria um label (?) com tooltip e retorna o label."""
    lbl = tk.Label(parent, text="(?)", fg="#0078D7", cursor="hand2", font=("Arial", 8, "bold"))
    _Tip(lbl, tip_text)
    return lbl


def _show_release_notes(parent):
    """Abre janela com notas de atualizacao. Resolve o path compativel com
    PyInstaller (ao lado do exe) e com modo dev (raiz do projeto)."""
    notes_path = os.path.join(get_runtime_root(), "assets", "release_notes.txt")
    try:
        with open(notes_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        content = f"Arquivo de notas nao encontrado.\n\nPath tentado:\n{notes_path}\n\nErro: {e}"

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


class MainScreen:
    conversion_manager = ConversionManager()
    directories_manager = DirectoriesManager()
    premiere_manager = PremiereManager()
    settings_manager = SettingsManager()
    transcription_manager: TranscriptionManager = None

    on_open_settings: Callable[[], None] = None
    on_logout: Callable[[], None] = None
    on_working: Callable[[], None] = None
    on_working_done: Callable[[], None] = None

    script_select: SelectComponent = None
    music_select: SelectComponent = None
    # NOVO
    resolution_select: SelectComponent = None

    refresh_options_button: tk.Button = None
    export_xml_button: tk.Button = None
    export_project_button: tk.Button = None

    min_zoom_entry: tk.Entry = None
    max_zoom_entry: tk.Entry = None

    def __init__(self, app: tk.Tk):
        self._app = app
        self.widget = tk.Frame(app)

        # Garante que exista mesmo se o checkbox não tiver sido criado ainda
        self.fade_live_var = tk.IntVar(value=0)

        self.directories_manager.ensure_directories()
        self.settings_manager.ensure_settings()

        settings = self.settings_manager.read_settings()
        ui_cache = settings.get("ui_cache", {}) if isinstance(
            settings, dict) else {}
        if not isinstance(ui_cache, dict):
            ui_cache = {}

        # Credenciais vem do servidor sob demanda (manual-credenciais).
        self.transcription_manager = TranscriptionManager()

        # ========== MENU SUPERIOR ==========
        self.mode_var = tk.StringVar(value='transcription')
        menubar = tk.Menu(app)
        menu_opcoes = tk.Menu(menubar, tearoff=0)
        menu_opcoes.add_command(label='Credenciais', command=lambda: self.on_open_settings())
        menu_opcoes.add_separator()

        def _show_terminal():
            if hasattr(self, '_terminal_popup') and self._terminal_popup:
                self._terminal_popup.show()

        menu_opcoes.add_command(label='Terminal', command=_show_terminal)
        menubar.add_cascade(label='Opcoes', menu=menu_opcoes)

        # Menu Ajuda
        menu_ajuda = tk.Menu(menubar, tearoff=0)
        menu_ajuda.add_command(label='Notas de Atualização', command=lambda: _show_release_notes(app))
        menu_ajuda.add_command(label='Sobre', command=lambda: messagebox.showinfo(
            'Sobre', f'Automatizador do Premiere {VERSAO}\nDesenvolvido por Kolaias', parent=app))
        menubar.add_cascade(label='Ajuda', menu=menu_ajuda)

        # "Sair" direto na barra (sem submenu)
        menubar.add_command(label='Sair',
                            command=lambda: self.on_logout() if self.on_logout else None)

        # Guarda referencia; aplicacao acontece no render() para nao
        # vazar o menu para outras telas (ex.: InitialScreen tem menu proprio).
        self._menubar = menubar

        # Parent direto para conteudo do editor
        ep = self.widget

        # ========== SELECTS HORIZONTAIS (roteiro | musica | resolucao) ==========
        selects_frame = tk.Frame(ep)
        selects_frame.pack(fill='x', padx=12, pady=(8, 4))

        self.script_select = SelectComponent(selects_frame, 'Roteiro:', horizontal=True)
        self.music_select = SelectComponent(selects_frame, 'Musica:', horizontal=True)
        self.resolution_select = SelectComponent(selects_frame, 'Resolucao:', horizontal=True)

        self.script_select.widget.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.music_select.widget.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.resolution_select.widget.pack(side='left', fill='x', expand=True)

        res_opts = ['1920 x 1080', '1536 x 768']
        cached_res = (ui_cache.get("resolution") or "").strip()
        self.resolution_select.set_options(res_opts, selected=cached_res)

        # Botao recarregar (inline)
        self.refresh_options_button = tk.Button(selects_frame, text='Recarregar', font=('Arial', 8))

        def load_options():
            options = self.directories_manager.read_directories() or {}
            scripts = list(options.get("narracao") or [])
            musics = list(options.get("musica") or [])
            cached_script = (ui_cache.get("script_name") or "").strip()
            cached_music = (ui_cache.get("music_style") or "").strip()
            self.script_select.set_options(scripts, selected=cached_script)
            self.music_select.set_options(musics, selected=cached_music)
            # atualiza listas de pastas (logo, overlay, animacao)
            for _refresh in [self.__refresh_logo_list, self.__refresh_overlay_list, self.__refresh_cta_list]:
                if hasattr(self, 'logo_menu'):
                    try:
                        _refresh()
                    except Exception:
                        pass

        self.refresh_options_button.configure(command=load_options)
        self.refresh_options_button.pack(side='left', padx=(8, 0))

        load_options()

        # ========== ZOOM / FADE / OPCOES (linha horizontal) ==========
        def validate_percentage(value: str) -> bool:
            try:
                if value == '':
                    return True
                return int(value) >= 1
            except:
                return False

        percentage_validate_command = (self.widget.register(validate_percentage), '%P')

        params_row = tk.Frame(ep)

        tk.Label(params_row, text='Zoom:').pack(side='left', padx=(0, 2))
        self.min_zoom_entry = tk.Entry(params_row, width=4, validate='key', validatecommand=percentage_validate_command)
        self.min_zoom_entry.insert(0, '100')
        self.min_zoom_entry.pack(side='left')
        tk.Label(params_row, text='~').pack(side='left')
        self.max_zoom_entry = tk.Entry(params_row, width=4, validate='key', validatecommand=percentage_validate_command)
        self.max_zoom_entry.insert(0, '110')
        self.max_zoom_entry.pack(side='left')
        tk.Label(params_row, text='%').pack(side='left', padx=(0, 12))

        tk.Label(params_row, text='Fade:').pack(side='left', padx=(0, 2))
        self.fade_entry = tk.Entry(params_row, width=4, validate='key', validatecommand=percentage_validate_command)
        self.fade_entry.insert(0, '10')
        self.fade_entry.pack(side='left')
        tk.Label(params_row, text='%').pack(side='left', padx=(0, 12))

        self.dup_scenes_var = tk.IntVar(value=1)
        tk.Checkbutton(params_row, text='Duplicar cenas', variable=self.dup_scenes_var).pack(side='left', padx=(0, 0))
        _tip_label(params_row, "Repete a cena até a próxima marca\nda narração, preenchendo o tempo.").pack(side='left', padx=(0, 6))

        self.fill_gaps_var = tk.IntVar(value=0)
        self.fill_gaps_cb = tk.Checkbutton(params_row, text='Preencher gaps', variable=self.fill_gaps_var)
        self.fill_gaps_cb.pack(side='left', padx=(0, 0))
        _tip_label(params_row, "Preenche espaços sem cena com\ncenas aleatórias do roteiro.").pack(side='left', padx=(0, 4))

        def _validate_fill_dur(v: str) -> bool:
            if v == '':
                return True
            try:
                return int(v) >= 0
            except Exception:
                return False

        vcmd_fill_dur = (self.widget.register(_validate_fill_dur), '%P')
        self.fill_gaps_dur_frame = tk.Frame(params_row)
        tk.Label(self.fill_gaps_dur_frame, text='max:').pack(side='left')
        _tip_label(self.fill_gaps_dur_frame, "Duração máxima de cada cena aleatória\nem segundos. 0 = sem limite.").pack(side='left')
        self.max_fill_scene_entry = tk.Entry(self.fill_gaps_dur_frame, width=3, validate='key', validatecommand=vcmd_fill_dur)
        self.max_fill_scene_entry.insert(0, '7')
        self.max_fill_scene_entry.pack(side='left')
        tk.Label(self.fill_gaps_dur_frame, text='s').pack(side='left')
        self.fill_gaps_dur_frame.pack_forget()

        params_row.pack(anchor='center', pady=(4, 4))

        # Botoes de exportar e XML (hidden)
        self.export_project_button = tk.Button(ep, text='Exportar projeto', font=('Arial', 10, 'bold'), bg='#2d7d46', fg='white', padx=20, pady=4)
        self.export_xml_button = tk.Button(ep, text='Exportar XML')

        def _sync_fill_gaps_state(*_args):
            try:
                if bool(self.dup_scenes_var.get()):
                    self.fill_gaps_var.set(0)
                    self.fill_gaps_cb.configure(state='disabled')
                    self.fill_gaps_dur_frame.pack_forget()
                else:
                    self.fill_gaps_cb.configure(state='normal')
                    if bool(self.fill_gaps_var.get()) and self.get_mode() != 'mass':
                        self.fill_gaps_dur_frame.pack(anchor='center', pady=(2, 0))
                    else:
                        self.fill_gaps_dur_frame.pack_forget()
            except Exception:
                pass

        try:
            self.dup_scenes_var.trace_add('write', _sync_fill_gaps_state)
        except Exception:
            pass
        try:
            self.fill_gaps_var.trace_add('write', _sync_fill_gaps_state)
        except Exception:
            pass
        _sync_fill_gaps_state()

        # =========================
        # FRASES IMPACTANTES
        # =========================
        self.impact_frame = tk.LabelFrame(ep, text='Frases Impactantes (GPT)', padx=8, pady=4)

        self.impact_font_file_var = tk.StringVar(value='')
        self._font_files = self.__list_font_files()
        self._font_map = {fn: os.path.join(self.__get_fontes_dir(), fn) for fn in self._font_files}

        impact_cached = ui_cache.get("impact", {}) if isinstance(ui_cache, dict) else {}
        if not isinstance(impact_cached, dict):
            impact_cached = {}

        cached_choice = (impact_cached.get("font_choice") or "").strip()
        font_options = ["(Auto)"] + self._font_files
        default_opt = cached_choice if cached_choice in self._font_files else "(Auto)"
        self.impact_font_choice_var = tk.StringVar(value=default_opt)

        def _on_font_choice_change(*_):
            choice = (self.impact_font_choice_var.get() or "").strip()
            if choice in ("(Auto)", ""):
                self.impact_font_file_var.set("")
            else:
                self.impact_font_file_var.set(self._font_map.get(choice, ""))
            # Atualizar preview inline com a nova fonte
            try:
                self._update_impact_inline_preview()
            except Exception:
                pass

        try:
            self.impact_font_choice_var.trace_add("write", _on_font_choice_change)
        except Exception:
            pass

        def _validate_int_ge1(v):
            return v == '' or (v.isdigit() and int(v) >= 1)
        def _validate_float_ge0(v):
            if v == '':
                return True
            try:
                return float(v.replace(',', '.')) >= 0.0
            except:
                return False
        vcmd_int = (self.widget.register(_validate_int_ge1), '%P')
        vcmd_float = (self.widget.register(_validate_float_ge0), '%P')

        # Linha 1: ativar + modo + posicao
        ir1 = tk.Frame(self.impact_frame)
        self.impact_enable_var = tk.IntVar(value=0)
        tk.Checkbutton(ir1, text='Ativar', variable=self.impact_enable_var).pack(side='left', padx=(0, 6))
        self.impact_use_cache_var = tk.IntVar(value=1 if impact_cached.get("use_cache", False) else 0)
        tk.Checkbutton(ir1, text='Usar cache', variable=self.impact_use_cache_var).pack(side='left', padx=(0, 0))
        _tip_label(ir1, "Reutiliza as frases selecionadas da\núltima execução (pula chamada à OpenAI).\nDesmarque para recriar a seleção.").pack(side='left', padx=(0, 8))
        self.impact_mode_var = tk.StringVar(value='phrase')
        tk.Label(ir1, text='Modo:').pack(side='left')
        tk.Radiobutton(ir1, text='Frase', variable=self.impact_mode_var, value='phrase').pack(side='left', padx=3)
        tk.Radiobutton(ir1, text='Palavra', variable=self.impact_mode_var, value='word').pack(side='left', padx=(0, 0))
        _tip_label(ir1, "Frase: mostra a frase inteira na tela.\nPalavra: mostra uma palavra por vez.").pack(side='left', padx=(0, 8))
        tk.Label(ir1, text='Max frases:').pack(side='left')
        self.impact_max_entry = tk.Entry(ir1, width=3, validate='key', validatecommand=vcmd_int)
        self.impact_max_entry.insert(0, '5')
        self.impact_max_entry.pack(side='left', padx=(0, 0))
        _tip_label(ir1, "Número máximo de frases/palavras\nque aparecerão no vídeo.").pack(side='left', padx=(0, 8))
        tk.Label(ir1, text='Intervalo:').pack(side='left')
        self.impact_gap_entry = tk.Entry(ir1, width=4, validate='key', validatecommand=vcmd_float)
        self.impact_gap_entry.insert(0, '8')
        self.impact_gap_entry.pack(side='left')
        tk.Label(ir1, text='s').pack(side='left', padx=(0, 0))
        _tip_label(ir1, "Intervalo mínimo em segundos entre\ncada frase/palavra na tela.\nÉ ajustado automaticamente se necessário.").pack(side='left', padx=(0, 8))
        tk.Label(ir1, text='Fonte:').pack(side='left')
        self.impact_font_menu = tk.OptionMenu(ir1, self.impact_font_choice_var, *font_options)
        self.impact_font_menu.config(width=10)
        self.impact_font_menu.pack(side='left')
        ir1.pack(anchor='w', pady=(0, 2))
        _on_font_choice_change()

        # Linha 2: Estilo + preview inline
        ir2 = tk.Frame(self.impact_frame)
        tk.Label(ir2, text='Estilo:').pack(side='left')

        from ..dialogs.StyleEditorDialog import load_styles, get_selected_style_name
        self._impact_style_names = list(load_styles().get("styles", {}).keys())
        self.impact_style_var = tk.StringVar(value=get_selected_style_name())
        self.impact_style_menu = tk.OptionMenu(ir2, self.impact_style_var, *self._impact_style_names)
        self.impact_style_menu.config(width=16)
        self.impact_style_menu.pack(side='left', padx=(4, 8))

        def _open_style_editor():
            from ..dialogs.StyleEditorDialog import StyleEditorDialog
            font_file = self.impact_font_file_var.get() or ''
            StyleEditorDialog(app, on_style_changed=self._on_style_changed_callback,
                              font_file=font_file)

        tk.Button(ir2, text='Editar Estilos', command=_open_style_editor).pack(side='left', padx=(0, 6))

        def _preview_1080p_inline():
            from ..dialogs.StyleEditorDialog import load_styles, render_preview_image, HAS_PIL, DEFAULT_STYLE
            if not HAS_PIL:
                return
            style_name = (self.impact_style_var.get() or "Padrão").strip()
            data = load_styles()
            st = dict(DEFAULT_STYLE)
            st.update(data.get("styles", {}).get(style_name, {}))
            font_file = self.impact_font_file_var.get() or ''
            img = render_preview_image(st, 1920, 1080, font_file=font_file)
            from PIL import ImageTk
            preview_win = tk.Toplevel(app)
            preview_win.title(f"Preview 1080p - {style_name}")
            scale = 0.65
            dw, dh = int(1920 * scale), int(1080 * scale)
            from PIL import Image
            disp = img.resize((dw, dh), Image.LANCZOS)
            photo = ImageTk.PhotoImage(disp)
            canvas = tk.Canvas(preview_win, width=dw, height=dh)
            canvas.pack()
            canvas.create_image(0, 0, anchor="nw", image=photo)
            canvas._photo = photo

        tk.Button(ir2, text='Preview 1080p', command=_preview_1080p_inline).pack(side='left')
        ir2.pack(anchor='w', pady=(0, 2))

        # Linha 3: Preview inline do estilo
        self._impact_preview_canvas = tk.Canvas(self.impact_frame, width=650, height=70, bg='#1a1a1a')
        self._impact_preview_canvas.pack(anchor='w', padx=4, pady=(0, 2))
        self._impact_preview_photo = None

        # Trace para atualizar preview ao mudar estilo
        self.impact_style_var.trace_add('write', lambda *_: self._update_impact_inline_preview())
        self._update_impact_inline_preview()

        def _sync_impact_state(*_args):
            try:
                state = 'normal' if bool(self.impact_enable_var.get()) else 'disabled'
                self.impact_max_entry.configure(state=state)
                self.impact_gap_entry.configure(state=state)
            except Exception:
                pass

        try:
            self.impact_enable_var.trace_add('write', _sync_impact_state)
        except Exception:
            pass
        _sync_impact_state()
        self.impact_frame.pack(fill='x', padx=12, pady=(4, 4))

        # =========================
        # RECURSOS VISUAIS (Logo | Overlay | CTA) - 3 colunas
        # =========================
        self.visual_frame = tk.LabelFrame(ep, text='Recursos Visuais', padx=8, pady=4)

        # --- LOGO (pasta logo/) ---
        col_logo = tk.Frame(self.visual_frame)
        self._logo_files = self.__list_logo_files()
        logo_opts = ['(Nenhum)'] + self._logo_files
        cached_logo = (ui_cache.get("logo_file") or "").strip()
        self.logo_choice_var = tk.StringVar(value=cached_logo if cached_logo in self._logo_files else '(Nenhum)')

        tk.Label(col_logo, text='Logo (pasta logo/):').pack(anchor='w')
        self.logo_menu = tk.OptionMenu(col_logo, self.logo_choice_var, *logo_opts)
        self.logo_menu.config(width=16)
        self.logo_menu.pack(anchor='w')

        self.logo_status_label = tk.Label(col_logo, text='', font=('', 8))
        self.logo_status_label.pack(anchor='w')

        self.logo_pos_var = tk.StringVar(value=(ui_cache.get("logo_position") or "bottom_right").strip())
        pos_row = tk.Frame(col_logo)
        for t, v in [('Sup.Esq', 'top_left'), ('Sup.Dir', 'top_right'), ('Inf.Esq', 'bottom_left'), ('Inf.Dir', 'bottom_right')]:
            tk.Radiobutton(pos_row, text=t, variable=self.logo_pos_var, value=v).pack(side='left', padx=2)
        pos_row.pack(anchor='w', pady=(2, 0))
        col_logo.pack(side='left', padx=(0, 16), anchor='n')

        # --- OVERLAY (pasta overlay/) ---
        col_overlay = tk.Frame(self.visual_frame)
        self._overlay_files = self.__list_overlay_files()
        overlay_opts = ['(Nenhum)'] + self._overlay_files
        cached_ov = (ui_cache.get("overlay_file") or "").strip()
        self.overlay_choice_var = tk.StringVar(value=cached_ov if cached_ov in self._overlay_files else '(Nenhum)')

        tk.Label(col_overlay, text='Overlay (pasta overlay/):').pack(anchor='w')
        self.overlay_menu = tk.OptionMenu(col_overlay, self.overlay_choice_var, *overlay_opts)
        self.overlay_menu.config(width=16)
        self.overlay_menu.pack(anchor='w')
        self.overlay_status_label = tk.Label(col_overlay, text='', font=('', 8))
        self.overlay_status_label.pack(anchor='w')
        col_overlay.pack(side='left', padx=(0, 16), anchor='n')

        # --- CTA INSCREVA-SE (pasta animacao/) ---
        col_cta = tk.Frame(self.visual_frame)
        self._cta_files = self.__list_cta_files()
        cta_opts = ['(Nenhum)'] + self._cta_files
        cached_cta = (ui_cache.get("cta_file") or "").strip()
        self.cta_choice_var = tk.StringVar(value=cached_cta if cached_cta in self._cta_files else '(Nenhum)')

        tk.Label(col_cta, text='Animacao (pasta animacao/):').pack(anchor='w')
        self.cta_menu = tk.OptionMenu(col_cta, self.cta_choice_var, *cta_opts)
        self.cta_menu.config(width=16)
        self.cta_menu.pack(anchor='w')

        self.cta_enable_var = tk.IntVar(value=1 if ui_cache.get("cta_enabled") else 0)
        self.cta_chroma_var = tk.IntVar(value=1 if ui_cache.get("cta_chroma_key", True) else 0)
        cta_opts_row = tk.Frame(col_cta)
        tk.Checkbutton(cta_opts_row, text='Inscreva-se', variable=self.cta_enable_var).pack(side='left')
        tk.Checkbutton(cta_opts_row, text='Chroma Key', variable=self.cta_chroma_var).pack(side='left', padx=(8, 0))
        _tip_label(cta_opts_row, "Remove o fundo verde/azul da\nanimação do CTA (Inscreva-se)\nusando chroma key via FFmpeg.").pack(side='left')
        cta_opts_row.pack(anchor='w', pady=(2, 0))
        col_cta.pack(side='left', anchor='n')

        self.visual_frame.pack(fill='x', padx=12, pady=(4, 4))

        # Traces para atualizar indicadores de renderizacao
        def _update_render_indicators(*_args):
            self.__update_logo_render_status()
            self.__update_overlay_render_status()

        self.logo_choice_var.trace_add('write', _update_render_indicators)
        self.logo_pos_var.trace_add('write', _update_render_indicators)
        self.overlay_choice_var.trace_add('write', _update_render_indicators)
        _update_render_indicators()

        # Manter compatibilidade com overlay_path_var e cta_path_var
        self.overlay_path_var = tk.StringVar(value='')
        self.cta_path_var = tk.StringVar(value='')

        # =========================
        # MIXER DE AUDIO (volume por trilha em dB)
        # =========================
        mixer_frame = tk.LabelFrame(ep, text='Mixer de Audio (dB)', padx=8, pady=4)

        def _validate_db(v):
            if v in ('', '-'):
                return True
            try:
                float(v)
                return True
            except:
                return False

        vcmd_db = (self.widget.register(_validate_db), '%P')

        tk.Label(mixer_frame, text='Cenas (A1):').pack(side='left')
        self.vol_scene_entry = tk.Entry(mixer_frame, width=5, validate='key', validatecommand=vcmd_db)
        self.vol_scene_entry.insert(0, str(ui_cache.get("vol_scene", -99)))
        self.vol_scene_entry.pack(side='left', padx=(0, 12))

        tk.Label(mixer_frame, text='Narracao (A2):').pack(side='left')
        self.vol_narration_entry = tk.Entry(mixer_frame, width=5, validate='key', validatecommand=vcmd_db)
        self.vol_narration_entry.insert(0, str(ui_cache.get("vol_narration", 0)))
        self.vol_narration_entry.pack(side='left', padx=(0, 12))

        tk.Label(mixer_frame, text='Inscreva-se (A3):').pack(side='left')
        self.vol_cta_entry = tk.Entry(mixer_frame, width=5, validate='key', validatecommand=vcmd_db)
        self.vol_cta_entry.insert(0, str(ui_cache.get("vol_cta", -9)))
        self.vol_cta_entry.pack(side='left', padx=(0, 12))

        tk.Label(mixer_frame, text='Musica (A5):').pack(side='left')
        self.vol_music_entry = tk.Entry(mixer_frame, width=5, validate='key', validatecommand=vcmd_db)
        self.vol_music_entry.insert(0, str(ui_cache.get("vol_music", -12)))
        self.vol_music_entry.pack(side='left')

        mixer_frame.pack(fill='x', padx=12, pady=(4, 4))

        # --- ORDEM DAS CENAS (modo em massa) ---
        self.order_frame = tk.Frame(ep)
        tk.Label(self.order_frame, text='Ordem das cenas').pack(
            side='left', padx=(0, 8))
        self.mass_order_var = tk.StringVar(value='asc')
        tk.Radiobutton(self.order_frame, text='Crescente',
                       variable=self.mass_order_var, value='asc').pack(side='left', padx=6)
        tk.Radiobutton(self.order_frame, text='Aleatória',
                       variable=self.mass_order_var, value='random').pack(side='left', padx=6)
        self.order_frame.pack_forget()

        # --- TAMANHO DAS CENAS (modo em massa) ---
        self.duration_frame = tk.Frame(ep)
        tk.Label(self.duration_frame, text='Tamanho das cenas (s):').pack(
            side='left', padx=(0, 8))

        def _validate_secs(v: str) -> bool:
            if v == '':
                return True
            try:
                return int(v) >= 1
            except:
                return False

        vcmd_secs = (self.widget.register(_validate_secs), '%P')

        tk.Label(self.duration_frame, text='de').pack(side='left')
        self.min_scene_secs_entry = tk.Entry(
            self.duration_frame, width=4, validate='key', validatecommand=vcmd_secs)
        self.min_scene_secs_entry.insert(0, '5')
        self.min_scene_secs_entry.pack(side='left', padx=(4, 6))

        tk.Label(self.duration_frame, text='a').pack(side='left')
        self.max_scene_secs_entry = tk.Entry(
            self.duration_frame, width=4, validate='key', validatecommand=vcmd_secs)
        self.max_scene_secs_entry.insert(0, '7')
        self.max_scene_secs_entry.pack(side='left', padx=(4, 0))

        self.duration_frame.pack_forget()

        # ✅ aplica cache (campos que dependem de widgets existirem)
        try:
            self.__apply_ui_cache(ui_cache)
        except Exception:
            pass

        # ✅ ressincroniza estados após cache
        try:
            _sync_fill_gaps_state()
        except Exception:
            pass

        try:
            _sync_impact_state()
        except Exception:
            pass

        # ✅ garante que o modo aplica visibilidade correta
        try:
            self.__on_mode_change()
        except Exception:
            pass

        def on_export_project():
            debug_print('UI', 'Exportar projeto: clicado')
            # SALVAR CACHE DAS OPÇÕES DO GUI
            try:
                ui_cache2 = self.__read_ui_cache() or {}
                if not isinstance(ui_cache2, dict):
                    ui_cache2 = {}

                ui_cache2["mode"] = self.get_mode()
                ui_cache2["script_name"] = self.script_select.get_selected_option(
                ) or ""
                ui_cache2["music_style"] = self.music_select.get_selected_option() or ""
                ui_cache2["resolution"] = self.resolution_select.get_selected_option(
                ) or "1920 x 1080"

                ui_cache2["fade_percentage"] = self.get_fade_percentage()
                ui_cache2["fade_live"] = bool(self.fade_live_var.get())
                ui_cache2["zoom_min"] = int(self.min_zoom_entry.get() or 100)
                ui_cache2["zoom_max"] = int(self.max_zoom_entry.get() or 110)
                ui_cache2["duplicate_scenes"] = bool(self.dup_scenes_var.get())
                ui_cache2["fill_gaps_without_scene"] = bool(
                    self.fill_gaps_var.get())

                try:
                    impact_cfg = self.get_impact_config()
                except Exception:
                    impact_cfg = {}

                if not isinstance(impact_cfg, dict):
                    impact_cfg = {}

                try:
                    choice = (self.impact_font_choice_var.get() or "").strip()
                    impact_cfg["font_choice"] = "" if choice in (
                        "", "(Auto)") else choice
                except Exception:
                    pass

                ui_cache2["impact"] = impact_cfg
                self.__write_ui_cache(ui_cache2)
            except Exception:
                pass

            try:
                is_data_valid = self.validate_data()
                if not is_data_valid:
                    return

                self.selected_mode = self.get_mode()
                self.selected_order = self.get_mass_order()
                self.selected_scene_range = self.get_scene_duration_range()
                self.selected_fade_percentage = self.get_fade_percentage()
                fade_var = getattr(self, "fade_live_var", None)
                self.selected_fade_live = bool(
                    fade_var.get()) if fade_var is not None else False
                self.selected_duplicate_scenes = bool(
                    self.dup_scenes_var.get())
                self.selected_fill_gaps_without_scene = bool(
                    getattr(self, 'fill_gaps_var', tk.IntVar(value=0)).get())
                self.selected_max_fill_scene_duration = self.get_max_fill_scene_duration()
                self.selected_impact_config = self.get_impact_config()

                # Recursos visuais
                self.selected_logo_path = self._get_selected_logo_path()
                self.selected_logo_position = (self.logo_pos_var.get() or 'bottom_right').strip()
                self.selected_overlay_path = self._get_selected_overlay_path()
                self.selected_cta_enabled = bool(self.cta_enable_var.get())
                self.selected_cta_anim_path = self._get_selected_cta_path()
                self.selected_cta_chroma_key = bool(self.cta_chroma_var.get())

                # Mixer volumes
                try:
                    self.selected_vol_scene = float(self.vol_scene_entry.get() or -99)
                except Exception:
                    self.selected_vol_scene = -99.0
                try:
                    self.selected_vol_narration = float(self.vol_narration_entry.get() or 0)
                except Exception:
                    self.selected_vol_narration = 0.0
                try:
                    self.selected_vol_cta = float(self.vol_cta_entry.get() or -9)
                except Exception:
                    self.selected_vol_cta = -9.0
                try:
                    self.selected_vol_music = float(self.vol_music_entry.get() or -12)
                except Exception:
                    self.selected_vol_music = -12.0

                # Log de todas as configurações selecionadas
                _script = self.script_select.get_selected_option() or '?'
                _music = self.music_select.get_selected_option() or '?'
                _resolution = self.resolution_select.get_selected_option() or '?'
                _impact = self.selected_impact_config or {}
                _logo_file = os.path.basename(self.selected_logo_path) if self.selected_logo_path else '(Nenhum)'
                _overlay_file = os.path.basename(self.selected_overlay_path) if self.selected_overlay_path else '(Nenhum)'
                _cta_file = os.path.basename(self.selected_cta_anim_path) if self.selected_cta_anim_path else '(Nenhum)'

                print("=" * 60)
                print("  CONFIGURAÇÕES DO EXPORT")
                print("=" * 60)
                print(f"  Roteiro:        {_script}")
                print(f"  Música:         {_music}")
                print(f"  Resolução:      {_resolution}")
                print(f"  Zoom:           {self.min_zoom_entry.get()}% ~ {self.max_zoom_entry.get()}%")
                print(f"  Fade:           {self.selected_fade_percentage}%  (imediato: {self.selected_fade_live})")
                print(f"  Duplicar cenas: {self.selected_duplicate_scenes}")
                print(f"  Preencher gaps: {self.selected_fill_gaps_without_scene}  (max dur: {self.selected_max_fill_scene_duration}s)")
                print("-" * 60)
                print(f"  Frases Impact.: {'SIM' if _impact.get('enabled') else 'NAO'}")
                if _impact.get('enabled'):
                    _st = _impact.get('text_style', {})
                    print(f"    Estilo:       {self.impact_style_var.get()}")
                    print(f"    Modo:         {_impact.get('mode', '?')}")
                    print(f"    Max frases:   {_impact.get('max_phrases_total', '?')}")
                    print(f"    Intervalo:    {_impact.get('min_gap_seconds', '?')}s")
                    print(f"    Posição:      {_st.get('position', '?')}")
                    print(f"    Tamanho:      {_st.get('font_size', '?')}px")
                    print(f"    Fonte:        {_impact.get('font_file', '(auto)')}")
                    print(f"    Cor:          {_st.get('font_color', '?')} | Borda: {_st.get('border_color', '?')} ({_st.get('border_width', '?')}px)")
                    print(f"    CapsLock:     {'SIM' if _st.get('caps_lock') else 'NAO'} | Anim: {_st.get('animation', 'none')} ({_st.get('anim_in_pct', 10)}%/{_st.get('anim_out_pct', 10)}%)")
                    print(f"    Usar cache:   {'SIM' if _impact.get('use_cache') else 'NAO'}")
                print("-" * 60)
                print(f"  Logo:           {_logo_file}  (pos: {self.selected_logo_position})")
                print(f"  Overlay:        {_overlay_file}")
                print(f"  CTA:            {'SIM' if self.selected_cta_enabled else 'NAO'}  ({_cta_file})  chroma: {self.selected_cta_chroma_key}")
                print("-" * 60)
                print(f"  Volume Cenas:   {self.selected_vol_scene} dB")
                print(f"  Volume Narr.:   {self.selected_vol_narration} dB")
                print(f"  Volume CTA:     {self.selected_vol_cta} dB")
                print(f"  Volume Música:  {self.selected_vol_music} dB")
                print("=" * 60)

                def on_export_project_done():
                    selected_script = self.script_select.get_selected_option()
                    save_result = self.premiere_manager.save_project(
                        selected_script)
                    if save_result.success == False:
                        messagebox.showerror(
                            'Erro ao exportar projeto', 'Ocorreu um erro desconhecido ao exportar o projeto.')
                        return

                    messagebox.showinfo(
                        'Projeto exportado', f'A exportacao do projeto foi salva com sucesso em "{save_result.data}".')
                    self.on_working_done()
                    return

                paralell_work = Thread(target=self.get_premiere_worker(
                    callback=on_export_project_done), daemon=True)
                paralell_work.start()
                self.on_working()
            except Exception as error:
                handle_thread_error(error, app)

        self.export_project_button.configure(command=on_export_project)

        # Frame inferior com botoes lado a lado
        bottom_btns = tk.Frame(ep)
        self.export_project_button.pack_forget()
        self.export_project_button = tk.Button(bottom_btns, text='Exportar projeto',
                                                font=('Arial', 10, 'bold'), bg='#2d7d46', fg='white',
                                                padx=20, pady=4, command=on_export_project)
        self.export_project_button.pack(side='left', padx=(0, 8))

        def _open_renamer():
            from ..screens.RenamerFeedbackScreen import RenamerFeedbackScreen
            RenamerFeedbackScreen(app, self.settings_manager)

        tk.Button(bottom_btns, text='Renomear Cenas',
                  font=('Arial', 10, 'bold'), bg='#5C2D91', fg='white',
                  padx=20, pady=4, command=_open_renamer).pack(side='left')
        bottom_btns.pack(side='bottom', pady=(8, 12))

        def on_export_xml():
            try:
                is_data_valid = self.validate_data()
                if not is_data_valid:
                    return

                def on_export_xml_done():
                    export_path = self.premiere_manager.export_xml()
                    if export_path is None:
                        messagebox.showerror(
                            'Erro ao exportar XML', 'Ocorreu um erro desconhecido ao exportar o arquivo XML do projeto.')
                        return

                    messagebox.showinfo(
                        'XML exportado', f'A exportação do arquivo XML do projeto foi salva com sucesso em "{export_path}".')
                    self.on_working_done()
                    return

                paralell_work = Thread(
                    target=self.get_premiere_worker(callback=on_export_xml_done),
                    daemon=True)
                paralell_work.start()
                self.on_working()
            except Exception as error:
                handle_thread_error(error, app)

        self.export_xml_button.configure(command=on_export_xml)
        # self.export_xml_button.pack()  # descomentando você ativa o XML

    def __get_runtime_root(self) -> str:
        # runtime root é a pasta onde o settings.json fica
        return os.path.dirname(self.settings_manager.SETTINGS_PATH)

    def __get_fontes_dir(self) -> str:
        return os.path.join(self.__get_runtime_root(), "fontes")

    def __get_logo_dir(self) -> str:
        return os.path.join(self.__get_runtime_root(), "logo")

    def __list_logo_files(self) -> list[str]:
        d = self.__get_logo_dir()
        try:
            if not os.path.exists(d):
                os.makedirs(d, exist_ok=True)
            files = []
            for fn in os.listdir(d):
                low = fn.lower()
                if low.endswith("_10m.mp4") or '_10m_' in low:
                    continue
                if low.endswith((".png", ".jpg", ".jpeg", ".psd", ".tga", ".bmp", ".gif")):
                    files.append(fn)
            files.sort(key=lambda x: x.lower())
            return files
        except Exception:
            return []

    def __get_overlay_dir(self) -> str:
        return os.path.join(self.__get_runtime_root(), "overlay")

    def __get_cta_dir(self) -> str:
        return os.path.join(self.__get_runtime_root(), "animacao")

    def __list_overlay_files(self) -> list[str]:
        d = self.__get_overlay_dir()
        try:
            if not os.path.exists(d):
                os.makedirs(d, exist_ok=True)
            files = []
            for fn in os.listdir(d):
                low = fn.lower()
                if low.endswith("_10m.mp4") or '_10m_' in low:
                    continue
                if low.endswith((".mp4", ".mov", ".avi", ".mxf", ".webm")):
                    files.append(fn)
            files.sort(key=lambda x: x.lower())
            return files
        except Exception:
            return []

    def __list_cta_files(self) -> list[str]:
        d = self.__get_cta_dir()
        try:
            if not os.path.exists(d):
                os.makedirs(d, exist_ok=True)
            files = []
            for fn in os.listdir(d):
                low = fn.lower()
                if low.endswith((".mp4", ".mov", ".avi", ".webm")):
                    files.append(fn)
            files.sort(key=lambda x: x.lower())
            return files
        except Exception:
            return []

    def __update_logo_render_status(self):
        try:
            choice = (self.logo_choice_var.get() or "").strip()
            if choice in ("", "(Nenhum)"):
                self.logo_status_label.config(text='', fg='black')
                return
            logo_base = os.path.splitext(choice)[0]
            pos = (self.logo_pos_var.get() or "bottom_right").replace(' ', '_')
            res = (self.resolution_select.get_selected_option() or "1920 x 1080").replace(" ", "").replace("x", "x")
            rendered = os.path.join(self.__get_logo_dir(), f'{logo_base}_10m_{pos}_{res}.mp4')
            if os.path.exists(rendered) and os.path.getsize(rendered) > 0:
                self.logo_status_label.config(text='(Renderizado)', fg='green')
            else:
                self.logo_status_label.config(text='(Nao renderizado)', fg='red')
        except Exception:
            self.logo_status_label.config(text='', fg='black')

    def __update_overlay_render_status(self):
        try:
            choice = (self.overlay_choice_var.get() or "").strip()
            if choice in ("", "(Nenhum)"):
                self.overlay_status_label.config(text='', fg='black')
                return
            overlay_base = os.path.splitext(choice)[0]
            rendered = os.path.join(self.__get_overlay_dir(), f'{overlay_base}_10m.mp4')
            if os.path.exists(rendered) and os.path.getsize(rendered) > 0:
                self.overlay_status_label.config(text='(Renderizado)', fg='green')
            else:
                self.overlay_status_label.config(text='(Nao renderizado)', fg='red')
        except Exception:
            self.overlay_status_label.config(text='', fg='black')

    def __refresh_overlay_list(self):
        try:
            self._overlay_files = self.__list_overlay_files()
            opts = ["(Nenhum)"] + self._overlay_files
            cur = (self.overlay_choice_var.get() or "").strip()
            if cur not in opts:
                cur = "(Nenhum)"
                self.overlay_choice_var.set(cur)
            menu = self.overlay_menu["menu"]
            menu.delete(0, "end")
            for o in opts:
                menu.add_command(label=o, command=tk._setit(self.overlay_choice_var, o))
        except Exception:
            pass

    def __refresh_cta_list(self):
        try:
            self._cta_files = self.__list_cta_files()
            opts = ["(Nenhum)"] + self._cta_files
            cur = (self.cta_choice_var.get() or "").strip()
            if cur not in opts:
                cur = "(Nenhum)"
                self.cta_choice_var.set(cur)
            menu = self.cta_menu["menu"]
            menu.delete(0, "end")
            for o in opts:
                menu.add_command(label=o, command=tk._setit(self.cta_choice_var, o))
        except Exception:
            pass

    def _get_selected_overlay_path(self) -> str:
        choice = (self.overlay_choice_var.get() or "").strip()
        if choice in ("", "(Nenhum)"):
            return ""
        return os.path.join(self.__get_overlay_dir(), choice)

    def _get_selected_cta_path(self) -> str:
        choice = (self.cta_choice_var.get() or "").strip()
        if choice in ("", "(Nenhum)"):
            return ""
        return os.path.join(self.__get_cta_dir(), choice)

    def _refresh_style_list(self):
        from ..dialogs.StyleEditorDialog import load_styles, get_selected_style_name
        data = load_styles()
        names = list(data.get("styles", {}).keys())
        self._impact_style_names = names
        cur = get_selected_style_name()
        self.impact_style_var.set(cur)
        menu = self.impact_style_menu["menu"]
        menu.delete(0, "end")
        for n in names:
            menu.add_command(label=n, command=lambda v=n: self.impact_style_var.set(v))

    def _on_style_changed_callback(self):
        self._refresh_style_list()
        self._update_impact_inline_preview()

    def _update_impact_inline_preview(self):
        try:
            from ..dialogs.StyleEditorDialog import load_styles, render_preview_image, HAS_PIL
            if not HAS_PIL:
                return
            style_name = (self.impact_style_var.get() or "Padrão").strip()
            data = load_styles()
            st = data.get("styles", {}).get(style_name, {})
            font_file = self.impact_font_file_var.get() or ''
            img = render_preview_image(st, 650, 70, font_file=font_file)
            from PIL import ImageTk
            self._impact_preview_photo = ImageTk.PhotoImage(img)
            self._impact_preview_canvas.delete("all")
            self._impact_preview_canvas.create_image(0, 0, anchor="nw", image=self._impact_preview_photo)
        except Exception:
            pass

    def _get_selected_logo_path(self) -> str:
        choice = (self.logo_choice_var.get() or "").strip()
        if choice in ("", "(Nenhum)"):
            return ""
        return os.path.join(self.__get_logo_dir(), choice)

    def __refresh_logo_list(self):
        try:
            self._logo_files = self.__list_logo_files()
            options = ["(Nenhum)"] + self._logo_files

            cur = (self.logo_choice_var.get() or "").strip()
            if cur not in options:
                cur = "(Nenhum)"
                self.logo_choice_var.set(cur)

            menu = self.logo_menu["menu"]
            menu.delete(0, "end")
            for opt in options:
                menu.add_command(
                    label=opt,
                    command=tk._setit(self.logo_choice_var, opt)
                )
        except Exception:
            pass

    def __list_font_files(self) -> list[str]:
        d = self.__get_fontes_dir()
        try:
            if not os.path.exists(d):
                os.makedirs(d, exist_ok=True)
            files = []
            for fn in os.listdir(d):
                low = fn.lower()
                if low.endswith(".ttf") or low.endswith(".otf"):
                    files.append(fn)
            files.sort(key=lambda x: x.lower())
            return files
        except Exception:
            return []

    def __read_ui_cache(self) -> dict:
        try:
            s = self.settings_manager.read_settings()
            return (s.get("ui_cache") or {}) if isinstance(s, dict) else {}
        except Exception:
            return {}

    def __write_ui_cache(self, ui_cache: dict):
        """
        Salva o ui_cache no settings.json.

        Obs: além do que vier em 'ui_cache', este método também atualiza automaticamente
        com o estado atual do GUI (para garantir que ao reabrir ele venha igual).
        """
        try:
            if not isinstance(ui_cache, dict):
                ui_cache = {}

            # ---- estados principais ----
            try:
                ui_cache["mode"] = self.get_mode()
            except Exception:
                pass

            try:
                ui_cache["script"] = self.script_select.get_selected_option() or ""
            except Exception:
                pass

            try:
                ui_cache["music_style"] = self.music_select.get_selected_option() or ""
            except Exception:
                pass

            try:
                ui_cache["resolution"] = self.resolution_select.get_selected_option(
                ) or "1920 x 1080"
            except Exception:
                pass

            # zoom + fade
            try:
                ui_cache["zoom_min"] = int(self.min_zoom_entry.get() or 100)
            except Exception:
                ui_cache["zoom_min"] = ui_cache.get("zoom_min", 100)

            try:
                ui_cache["zoom_max"] = int(self.max_zoom_entry.get() or 110)
            except Exception:
                ui_cache["zoom_max"] = ui_cache.get("zoom_max", 110)

            try:
                ui_cache["fade_percentage"] = self.get_fade_percentage()
            except Exception:
                pass

            try:
                ui_cache["fade_live"] = bool(self.fade_live_var.get())
            except Exception:
                pass

            # cenas
            try:
                ui_cache["duplicate_scenes"] = bool(self.dup_scenes_var.get())
            except Exception:
                pass

            try:
                ui_cache["fill_gaps_without_scene"] = bool(
                    self.fill_gaps_var.get())
            except Exception:
                pass

            try:
                ui_cache["max_fill_scene_duration"] = self.get_max_fill_scene_duration()
            except Exception:
                pass

            # ---- modo em massa ----
            try:
                ui_cache["mass_order"] = self.get_mass_order()
            except Exception:
                pass

            try:
                a, b = self.get_scene_duration_range()
                ui_cache["min_scene_seconds"] = int(a)
                ui_cache["max_scene_seconds"] = int(b)
            except Exception:
                pass

            # ---- frases impactantes ----
            try:
                ui_cache["impact"] = self.get_impact_config()
            except Exception:
                pass

            # ---- recursos visuais ----
            try:
                choice = (self.logo_choice_var.get() or "").strip()
                ui_cache["logo_file"] = "" if choice == "(Nenhum)" else choice
            except Exception:
                pass

            try:
                ui_cache["logo_position"] = (self.logo_pos_var.get() or "bottom_right").strip()
            except Exception:
                pass

            try:
                ov_choice = (self.overlay_choice_var.get() or "").strip()
                ui_cache["overlay_file"] = "" if ov_choice == "(Nenhum)" else ov_choice
            except Exception:
                pass

            try:
                ui_cache["cta_enabled"] = bool(self.cta_enable_var.get())
            except Exception:
                pass

            try:
                cta_choice = (self.cta_choice_var.get() or "").strip()
                ui_cache["cta_file"] = "" if cta_choice == "(Nenhum)" else cta_choice
            except Exception:
                pass

            try:
                ui_cache["cta_chroma_key"] = bool(self.cta_chroma_var.get())
            except Exception:
                pass

            # mixer de audio
            try:
                ui_cache["vol_scene"] = float(self.vol_scene_entry.get() or -99)
            except Exception:
                pass
            try:
                ui_cache["vol_narration"] = float(self.vol_narration_entry.get() or 0)
            except Exception:
                pass
            try:
                ui_cache["vol_cta"] = float(self.vol_cta_entry.get() or -9)
            except Exception:
                pass
            try:
                ui_cache["vol_music"] = float(self.vol_music_entry.get() or -12)
            except Exception:
                pass

        except Exception:
            # se algo der ruim, ainda tenta salvar o que já tinha
            if not isinstance(ui_cache, dict):
                ui_cache = {}

        try:
            s = self.settings_manager.read_settings()
            if not isinstance(s, dict):
                s = {}
            s["ui_cache"] = ui_cache
            self.settings_manager.write_settings(s)
        except Exception:
            pass

    def __apply_ui_cache(self, ui_cache: dict):
        if not isinstance(ui_cache, dict):
            return

        # -------- Selects (roteiro / música / resolução) --------
        try:
            script = (ui_cache.get("script") or "").strip()
            if script:
                self.script_select.set_selected_option(script)
        except Exception:
            pass

        try:
            music = (ui_cache.get("music_style") or "").strip()
            if music:
                self.music_select.set_selected_option(music)
        except Exception:
            pass

        try:
            res = (ui_cache.get("resolution") or "").strip()
            if res:
                self.resolution_select.set_selected_option(res)
        except Exception:
            pass

        # -------- modo --------
        try:
            m = (ui_cache.get("mode") or "transcription").strip()
            if m in ("transcription", "mass"):
                self.mode_var.set(m)
        except Exception:
            pass

        # -------- zoom + fade --------
        try:
            self.min_zoom_entry.delete(0, "end")
            self.min_zoom_entry.insert(0, str(ui_cache.get("zoom_min", 100)))
        except Exception:
            pass

        try:
            self.max_zoom_entry.delete(0, "end")
            self.max_zoom_entry.insert(0, str(ui_cache.get("zoom_max", 110)))
        except Exception:
            pass

        try:
            self.fade_entry.delete(0, "end")
            self.fade_entry.insert(0, str(ui_cache.get("fade_percentage", 10)))
        except Exception:
            pass

        try:
            self.fade_live_var.set(
                1 if ui_cache.get("fade_live", False) else 0)
        except Exception:
            pass

        # -------- duplicar / preencher gaps --------
        try:
            self.dup_scenes_var.set(1 if ui_cache.get(
                "duplicate_scenes", True) else 0)
        except Exception:
            pass

        try:
            self.fill_gaps_var.set(1 if ui_cache.get(
                "fill_gaps_without_scene", False) else 0)
        except Exception:
            pass

        try:
            v = int(ui_cache.get("max_fill_scene_duration", 7))
            self.max_fill_scene_entry.delete(0, "end")
            self.max_fill_scene_entry.insert(0, str(max(0, v)))
        except Exception:
            pass

        # -------- modo em massa --------
        try:
            order = (ui_cache.get("mass_order") or "asc").strip()
            if order in ("asc", "random"):
                self.mass_order_var.set(order)
        except Exception:
            pass

        try:
            a = int(ui_cache.get("min_scene_seconds", 5))
            b = int(ui_cache.get("max_scene_seconds", 7))
            self.min_scene_secs_entry.delete(0, "end")
            self.min_scene_secs_entry.insert(0, str(max(1, a)))
            self.max_scene_secs_entry.delete(0, "end")
            self.max_scene_secs_entry.insert(0, str(max(1, b)))
        except Exception:
            pass

        # -------- impacto --------
        impact = ui_cache.get("impact", {})
        if isinstance(impact, dict):
            try:
                self.impact_enable_var.set(
                    1 if impact.get("enabled", False) else 0)
            except Exception:
                pass

            try:
                self.impact_mode_var.set(
                    (impact.get("mode") or "phrase").strip())
            except Exception:
                pass

            try:
                self.impact_use_cache_var.set(
                    1 if impact.get("use_cache", False) else 0)
            except Exception:
                pass

            try:
                self.impact_max_entry.delete(0, "end")
                self.impact_max_entry.insert(
                    0, str(impact.get("max_phrases_total", 5)))
            except Exception:
                pass

            try:
                self.impact_gap_entry.delete(0, "end")
                self.impact_gap_entry.insert(
                    0, str(impact.get("min_gap_seconds", 8.0)))
            except Exception:
                pass

            # fonte da pasta fontes (nome do arquivo)
            try:
                choice = (impact.get("font_choice") or "").strip()
                if choice and choice in (self._font_files or []):
                    self.impact_font_choice_var.set(choice)
                else:
                    self.impact_font_choice_var.set("(Auto)")
            except Exception:
                pass

        # -------- recursos visuais --------
        try:
            logo = (ui_cache.get("logo_file") or "").strip()
            if logo and logo in (self._logo_files or []):
                self.logo_choice_var.set(logo)
            else:
                self.logo_choice_var.set("(Nenhum)")
        except Exception:
            pass

        try:
            lpos = (ui_cache.get("logo_position") or "bottom_right").strip()
            if lpos in ("top_left", "top_right", "bottom_left", "bottom_right"):
                self.logo_pos_var.set(lpos)
        except Exception:
            pass

        try:
            ov = (ui_cache.get("overlay_file") or "").strip()
            if ov and ov in (self._overlay_files or []):
                self.overlay_choice_var.set(ov)
            else:
                self.overlay_choice_var.set("(Nenhum)")
        except Exception:
            pass

        try:
            self.cta_enable_var.set(1 if ui_cache.get("cta_enabled") else 0)
        except Exception:
            pass

        try:
            ct = (ui_cache.get("cta_file") or "").strip()
            if ct and ct in (self._cta_files or []):
                self.cta_choice_var.set(ct)
            else:
                self.cta_choice_var.set("(Nenhum)")
        except Exception:
            pass

        try:
            self.cta_chroma_var.set(1 if ui_cache.get("cta_chroma_key", True) else 0)
        except Exception:
            pass

        # aplica visibilidade correta do modo
        try:
            self.__on_mode_change()
        except Exception:
            pass

    def validate_data(self) -> bool:
        selected_script = self.script_select.get_selected_option()
        selected_music = self.music_select.get_selected_option()

        if self.mode_var.get() == 'mass':
            # precisa de música
            if selected_music == '':
                messagebox.showerror(
                    'Nenhum estilo de música selecionado', 'Por favor, selecione um estilo de música.')
                return False
            # validar intervalo (sempre no mass)
            try:
                a = int(self.min_scene_secs_entry.get())
                b = int(self.max_scene_secs_entry.get())
                if a < 1 or b < 1 or a > b:
                    raise ValueError()
            except Exception:
                messagebox.showerror(
                    'Intervalo inválido', 'Informe segundos inteiros (>=1) e com "de" <= "a" (ex.: 5 a 7).')
                return False
            return True

        # modo 'transcription' — precisa de roteiro e música
        if selected_script == '':
            messagebox.showerror('Nenhum roteiro selecionado',
                                 'Por favor, selecione um roteiro.')
            return False

        if selected_music == '':
            messagebox.showerror('Nenhum estilo de música selecionado',
                                 'Por favor, selecione um estilo de música.')
            return False

        # Se frases impactantes estiverem ligadas, exige credencial OpenAI remota
        try:
            impact_cfg = self.get_impact_config()
            if impact_cfg.get("enabled"):
                from core.remote_credentials import get_api_key
                if not get_api_key('OPENAI_API_KEY'):
                    messagebox.showerror(
                        'Credencial OpenAI indisponivel',
                        'Frases impactantes (GPT) exigem a credencial OpenAI do servidor. '
                        'Verifique seu login e a conexao, e tente novamente.'
                    )
                    return False
        except Exception:
            pass

        return True

    def __on_mode_change(self) -> None:
        mode = (self.mode_var.get() or 'transcription').strip()

        # Música e resolução ficam visíveis nos dois modos
        try:
            self.music_select.render()
        except Exception:
            pass

        try:
            self.resolution_select.render()
        except Exception:
            pass

        if mode == 'mass':
            # no modo em massa: some roteiro e impacto, aparece ordem + duração
            try:
                self.script_select.unrender()
            except Exception:
                pass

            try:
                self.order_frame.pack(pady=4)
            except Exception:
                pass

            try:
                self.duration_frame.pack(pady=4)
            except Exception:
                pass

            try:
                self.impact_frame.pack_forget()
            except Exception:
                pass

            # oculta frame de dur. máx. fill_gaps (só aparece no modo transcrição)
            try:
                self.fill_gaps_dur_frame.pack_forget()
            except Exception:
                pass
        else:
            # no modo transcrição: aparece roteiro e impacto, some ordem + duração
            try:
                self.script_select.render()
            except Exception:
                pass

            try:
                self.order_frame.pack_forget()
            except Exception:
                pass

            try:
                self.duration_frame.pack_forget()
            except Exception:
                pass

            try:
                self.impact_frame.pack(anchor='center', pady=(6, 8))
            except Exception:
                pass

    def get_mode(self) -> str:
        """Retorna 'transcription' ou 'mass'."""
        return self.mode_var.get()

    def get_scene_duration_range(self) -> tuple[int, int]:
        """Retorna (min_seg, max_seg) — inteiros >= 1."""
        try:
            a = max(1, int(self.min_scene_secs_entry.get()))
            b = max(1, int(self.max_scene_secs_entry.get()))
            if a > b:
                a, b = b, a
            return (a, b)
        except:
            return (5, 7)

    def get_max_fill_scene_duration(self) -> float:
        """Retorna dur. máx. de cena aleatória em segundos (0 = sem limite). Padrão: 7."""
        try:
            v = int(self.max_fill_scene_entry.get())
            return float(max(0, v))
        except Exception:
            return 7.0

    def get_fade_percentage(self) -> int:
        """Retorna a porcentagem do fade (inteiro >= 1). Padrão: 10."""
        try:
            v = int(self.fade_entry.get())
            return max(1, v)
        except Exception:
            return 10

    def get_impact_config(self) -> dict:
        """
        Config das frases impactantes (GUI).
        enabled: se vai usar GPT + overlay
        mode: 'phrase' (frase inteira) ou 'word' (palavra por palavra)
        max_phrases_total: limite total no vídeo
        min_gap_seconds: intervalo mínimo entre frases
        position: bottom|center|top
        font_choice: nome do arquivo dentro da pasta fontes (ou "")
        font_file: caminho absoluto resolvido (ou "")
        font_size_px: int ou None
        """
        try:
            enabled = bool(self.impact_enable_var.get())
        except Exception:
            enabled = False

        try:
            mode = (self.impact_mode_var.get() or 'phrase').strip().lower()
            if mode not in ('phrase', 'word'):
                mode = 'phrase'
        except Exception:
            mode = 'phrase'

        try:
            max_phrases = int(self.impact_max_entry.get())
            max_phrases = max(1, min(50, max_phrases))
        except Exception:
            max_phrases = 5

        try:
            gap_s = float((self.impact_gap_entry.get()
                          or '8').replace(',', '.'))
            gap_s = max(0.0, gap_s)
        except Exception:
            gap_s = 8.0

        # fonte vinda da pasta "fontes"
        try:
            choice = (self.impact_font_choice_var.get() or "").strip()
        except Exception:
            choice = ""

        if choice in ("", "(Auto)"):
            font_choice = ""
            font_file = ""
        else:
            font_choice = choice
            font_file = os.path.join(self.__get_fontes_dir(), choice)

        # Estilo visual (inclui font_size e position)
        try:
            from ..dialogs.StyleEditorDialog import load_styles, DEFAULT_STYLE
            style_name = (self.impact_style_var.get() or "Padrão").strip()
            styles_data = load_styles()
            text_style = dict(DEFAULT_STYLE)
            text_style.update(styles_data.get("styles", {}).get(style_name, {}))
            styles_data["selected"] = style_name
        except Exception:
            text_style = {}

        font_size_px = text_style.get("font_size", None)
        pos = text_style.get("position", "bottom")

        return {
            "enabled": enabled,
            "mode": mode,
            "max_phrases_total": max_phrases,
            "min_gap_seconds": gap_s,
            "position": pos,
            "font_choice": font_choice,
            "font_file": font_file,
            "font_size_px": font_size_px,
            "text_style": text_style,
            "use_cache": bool(self.impact_use_cache_var.get()),
        }

    def get_mass_order(self) -> str:
        """Retorna 'asc' ou 'random' (apenas para o modo em massa)."""
        try:
            return self.mass_order_var.get()
        except Exception:
            return 'asc'

    def get_premiere_worker(self, callback: Callable[[], None] = None) -> Callable[[], None]:
        def premiere_worker_fn():
            debug_print('Worker', 'Premiere worker iniciou')
            t0 = time.time()
            selected_script = self.script_select.get_selected_option()
            selected_music = self.music_select.get_selected_option()
            debug_print('Worker', 'Seleções',
                        script=selected_script, music=selected_music)

            # NOVO: aplicar resolução escolhida ao PremiereManager
            try:
                selected_res = self.resolution_select.get_selected_option() or '1920 x 1080'
                parts = selected_res.lower().replace(' ', '').split('x')
                w, h = int(parts[0]), int(parts[1])
            except Exception:
                w, h = 1920, 1080
            self.premiere_manager.set_frame_size(w, h)

            # --- fluxo 'Vídeo em massa' (desvio antecipado) ---
            if getattr(self, 'selected_mode', 'transcription') == 'mass':
                zoom_min_scale_multiplier = int(
                    self.min_zoom_entry.get()) / 100
                zoom_max_scale_multiplier = int(
                    self.max_zoom_entry.get()) / 100
                scene_min, scene_max = getattr(
                    self, 'selected_scene_range', self.get_scene_duration_range())

                return self._run_mass_flow(
                    selected_music=selected_music,
                    zoom_min=zoom_min_scale_multiplier,
                    zoom_max=zoom_max_scale_multiplier,
                    order_mode=getattr(self, 'selected_order',
                                       self.get_mass_order()),
                    scene_min_secs=scene_min,
                    scene_max_secs=scene_max,
                    fade_percentage=getattr(
                        self, 'selected_fade_percentage', self.get_fade_percentage()),
                    apply_fade_immediately=getattr(
                        self, 'selected_fade_live', False),
                    callback=callback
                )

            # --- fim do desvio ---

            try:
                self.premiere_manager.ensure_sequence(selected_script)
            except Exception as e:
                messagebox.showerror(
                    'Erro ao criar sequência no Premiere',
                    str(e)
                )
                self.on_working_done()
                return

            get_files_paths_result = self.premiere_manager.get_files_paths(
                script_name=selected_script,
                music_style=selected_music
            )

            if get_files_paths_result.success == False:
                messagebox.showerror(
                    'Erro ao carregar arquivos', get_files_paths_result.error)
                self.on_working_done()
                return

            narration_base_path: str = get_files_paths_result.data.get(
                'narration_base_path')
            narrations_files: list[str] = get_files_paths_result.data.get(
                'narrations_files')
            scenes_base_path: str = get_files_paths_result.data.get(
                'scenes_base_path')
            scenes_files: list[str] = get_files_paths_result.data.get(
                'scenes_files')

            paths_map: dict[str, str] = {}
            for file_path in get_files_paths_result.data.get('files_paths'):
                paths_map[file_path] = file_path

            # A bit hard logic to understand:
            # The files can fail during the importation process. If that happens, first we try to rename the file and import it again.
            # If that fails too, we try to convert the file and import it again. Failing again, we try to rename the converted file once more and import it again.
            # If all of that fails, we finally show an error message to the user.
            files_to_import: list[str] = get_files_paths_result.data.get(
                'files_paths')
            import_files_result = self.premiere_manager.import_files(
                files_to_import)

            if not all(import_files_result.values()):
                renamed_files_to_import: list[str] = []

                for import_path, import_result in import_files_result.items():
                    if import_result == True:
                        continue

                    renamed_file_path = create_renamed_file(
                        paths_map.get(import_path))
                    paths_map[import_path] = renamed_file_path
                    renamed_files_to_import.append(renamed_file_path)

                import_renamed_files_result = self.premiere_manager.import_files(
                    renamed_files_to_import)
                if not all(import_renamed_files_result.values()):
                    converted_files_to_import: list[str] = []

                    for renamed_import_path, renamed_import_result in import_renamed_files_result.items():
                        if renamed_import_result == True:
                            continue

                        converted_file_path = ''

                        file_type = self.conversion_manager.identify_file_type(
                            renamed_import_path)
                        if file_type == 'AUDIO':
                            audio_conversion_result = self.conversion_manager.convert_audio(
                                renamed_import_path)
                            if audio_conversion_result.success == False:
                                messagebox.showerror(
                                    'Erro ao converter arquivo de áudio', audio_conversion_result.error)
                                self.on_working_done()
                                return

                            converted_file_path = audio_conversion_result.data

                        elif file_type == 'IMAGE':
                            converted_file_path = self.conversion_manager.convert_image(
                                renamed_import_path)

                        elif file_type == 'VIDEO':
                            converted_file_path = self.conversion_manager.convert_video(
                                renamed_import_path)

                        else:
                            messagebox.showerror(
                                'Erro ao converter arquivo', f'O arquivo "{renamed_import_path}" é de um tipo não suportado.')
                            self.on_working_done()
                            return

                        # a chave aqui é o arquivo RENOMEADO que falhou
                        paths_map[renamed_import_path] = converted_file_path
                        converted_files_to_import.append(converted_file_path)

                    import_converted_files_result = self.premiere_manager.import_files(
                        converted_files_to_import)
                    if not all(import_converted_files_result.values()):
                        converted_renamed_files_to_import: list[str] = []

                        for converted_import_path, converted_import_result in import_converted_files_result.items():
                            if converted_import_result == True:
                                continue

                            # a chave aqui é o caminho do CONVERTIDO que falhou
                            converted_renamed_file_path = create_renamed_file(
                                paths_map.get(converted_import_path))
                            paths_map[converted_import_path] = converted_renamed_file_path
                            converted_renamed_files_to_import.append(
                                converted_renamed_file_path)

                        import_converted_renamed_files_result = self.premiere_manager.import_files(
                            converted_renamed_files_to_import)
                        if not all(import_converted_renamed_files_result.values()):
                            messagebox.showerror(
                                'Erro ao importar arquivos', 'Ocorreu um erro desconhecido ao importar ao menos um dos arquivos.')
                            self.on_working_done()
                            return

            narrations_transcriptions_result = self.transcription_manager.transcribe_multiple_audios([
                path.join(narration_base_path, narration_file) for narration_file in narrations_files
            ])

            if narrations_transcriptions_result.success == False:
                messagebox.showerror(
                    'Erro ao obter transcrições', narrations_transcriptions_result.error)
                self.on_working_done()
                return

            # Credencial OpenAI vem sob demanda do servidor (property).
            # set_openai_api_key agora e no-op, chamada preservada por legado.
            self.transcription_manager.set_openai_api_key('')

            narrations_map: dict[str, list[Part]] = {}
            for narration_index, narration_transcription in enumerate(narrations_transcriptions_result.data):
                narrations_parts = self.transcription_manager.find_parts_with_llm(
                    transcription=narration_transcription,
                    scenes_files=scenes_files
                )

                narrations_map[narrations_files[narration_index]
                               ] = narrations_parts

            zoom_min_scale_multiplier = int(self.min_zoom_entry.get()) / 100
            zoom_max_scale_multiplier = int(self.max_zoom_entry.get()) / 100

            # NOVO: config frases impactantes + chave OpenAI
            try:
                impact_cfg = getattr(self, 'selected_impact_config', None)
                if not isinstance(impact_cfg, dict):
                    impact_cfg = {"enabled": False}
            except Exception:
                impact_cfg = {"enabled": False}

            try:
                from core.remote_credentials import get_api_key
                openai_key = get_api_key('OPENAI_API_KEY')
            except Exception:
                openai_key = ''

            mount_sequence_result = self.premiere_manager.mount_sequence(
                narrations_files=narrations_files,
                narration_base_path=narration_base_path,
                scenes_base_path=scenes_base_path,
                musics_files=get_files_paths_result.data.get('musics_files'),
                musics_base_path=get_files_paths_result.data.get(
                    'musics_base_path'),
                paths_map=paths_map,
                narrations_map=narrations_map,

                narrations_transcriptions=narrations_transcriptions_result.data,
                impact_phrases_config=impact_cfg,
                openai_api_key=openai_key,

                zoom_min_scale_multiplier=zoom_min_scale_multiplier,
                zoom_max_scale_multiplier=zoom_max_scale_multiplier,
                fade_percentage=self.get_fade_percentage(),
                apply_fade_immediately=getattr(
                    self, 'selected_fade_live', False),
                duplicate_scenes_until_next=getattr(
                    self, 'selected_duplicate_scenes', True),
                fill_gaps_with_random_scenes=getattr(
                    self, 'selected_fill_gaps_without_scene', False),
                max_fill_scene_duration=getattr(
                    self, 'selected_max_fill_scene_duration', 0.0),

                # Recursos visuais
                logo_path=getattr(self, 'selected_logo_path', ''),
                logo_position=getattr(self, 'selected_logo_position', 'bottom_right'),
                overlay_path=getattr(self, 'selected_overlay_path', ''),
                cta_enabled=getattr(self, 'selected_cta_enabled', False),
                cta_anim_path=getattr(self, 'selected_cta_anim_path', ''),
                cta_chroma_key=getattr(self, 'selected_cta_chroma_key', True),

                # Mixer
                vol_scene_db=getattr(self, 'selected_vol_scene', -99.0),
                vol_narration_db=getattr(self, 'selected_vol_narration', 0.0),
                vol_cta_db=getattr(self, 'selected_vol_cta', -9.0),
                vol_music_db=getattr(self, 'selected_vol_music', -12.0),
            )

            if mount_sequence_result.success == False:
                messagebox.showerror(
                    'Erro ao montar sequência', mount_sequence_result.error)
                self.on_working_done()
                return

            return callback()

        return premiere_worker_fn

    def __refresh_font_list(self):
        try:
            self._font_files = self.__list_font_files()
            options = ["(Auto)"] + self._font_files

            # mantém seleção atual se ainda existir
            cur = (self.impact_font_choice_var.get() or "").strip()
            if cur not in options:
                cur = "(Auto)"
                self.impact_font_choice_var.set(cur)

            # atualiza o menu do OptionMenu
            menu = self.impact_font_menu["menu"]
            menu.delete(0, "end")
            for opt in options:
                menu.add_command(
                    label=opt,
                    command=tk._setit(self.impact_font_choice_var, opt)
                )
        except Exception:
            pass

    def _run_mass_flow(self, selected_music: str, zoom_min: float, zoom_max: float,
                       callback: Callable[[], None], order_mode: str = 'asc',
                       scene_min_secs: int = 5, scene_max_secs: int = 7,
                       fade_percentage: int = 10,
                       apply_fade_immediately: bool = False):
        """
        Fluxo 'Vídeo em massa':
        - Lê partes/roteiro_X/cenas_Y
        - Importa todos os arquivos (com fallback renomear/convert­er)
        - Prepara músicas do estilo selecionado
        - (PARTE 4) Monta o projeto chamando PremiereManager.mount_mass_project(...)
        """
        try:
            # 1) Ler estrutura de 'partes'
            mass_structure = self.directories_manager.read_mass_structure()
            if not mass_structure.get('roteiros'):
                messagebox.showerror(
                    'Partes não encontradas', 'Não há pastas "roteiro_X/cenas_Y" em "partes/".')
                self.on_working_done()
                return

            # 2) Montar lista de importação (partes + músicas)
            paths_map: dict[str, str] = {}
            files_to_import: list[str] = []

            # 2.1) adicionar todos os arquivos das partes
            for p in mass_structure.get('files_paths', []):
                paths_map[p] = p
                files_to_import.append(p)

            # 2.2) resolver músicas do estilo selecionado
            musics_base_path = os.path.join(
                self.directories_manager.CWD, 'musica', selected_music)
            if not os.path.exists(musics_base_path):
                messagebox.showerror(
                    'Músicas não encontradas', f'Pasta inexistente: "{musics_base_path}".')
                self.on_working_done()
                return

            # filtra arquivos de áudio suportados e ordena por nome
            musics_files_all = [f for f in os.listdir(musics_base_path)
                                if os.path.isfile(os.path.join(musics_base_path, f))]
            musics_files = []
            for f in sorted(musics_files_all):
                _, ext = os.path.splitext(f)
                if ext.lower() in EXTENSIONS['AUDIO']:
                    musics_files.append(f)
                    absf = os.path.join(musics_base_path, f)
                    if absf not in paths_map:
                        paths_map[absf] = absf
                        files_to_import.append(absf)

            if not musics_files:
                messagebox.showerror(
                    'Nenhuma música', f'Não há áudios suportados em "{musics_base_path}".')
                self.on_working_done()
                return

            # 3) Importar tudo com fallback (renomear/convert­er/renomear-convertido)
            if not self.__import_with_fallback(paths_map, files_to_import):
                # a própria rotina já mostra mensagem de erro
                self.on_working_done()
                return

            # 4) (PARTE 4) Montagem no Premiere sem transcrição
            # Vamos implementar este método na próxima parte.
            # Assinatura prevista:
            #   mount_mass_project(mass_structure, musics_files, musics_base_path, paths_map, zoom_min_scale_multiplier, zoom_max_scale_multiplier)
            try:
                mount_res = self.premiere_manager.mount_mass_project(
                    mass_structure=mass_structure,
                    musics_files=musics_files,
                    musics_base_path=musics_base_path,
                    paths_map=paths_map,
                    zoom_min_scale_multiplier=zoom_min,
                    zoom_max_scale_multiplier=zoom_max,
                    order_mode=order_mode,
                    min_scene_seconds=scene_min_secs,
                    max_scene_seconds=scene_max_secs,
                    fade_percentage=fade_percentage,
                    apply_fade_immediately=apply_fade_immediately
                )

            except AttributeError:
                messagebox.showerror(
                    'Função ausente',
                    'O método "mount_mass_project" ainda não existe no PremiereManager. '
                    'Conclua a PARTE 4 para habilitar a montagem do modo "Vídeo em massa".'
                )
                self.on_working_done()
                return

            if not mount_res or mount_res.success is False:
                messagebox.showerror('Erro ao montar (mass)', getattr(
                    mount_res, 'error', 'Ocorreu um erro desconhecido ao montar o projeto.'))
                self.on_working_done()
                return

            # deu bom 🎉
            return callback()

        except Exception as error:
            handle_thread_error(error, None)
            return

    def __import_with_fallback(self, paths_map: dict[str, str], files_to_import: list[str]) -> bool:
        """
        Importa arquivos no Premiere com fallback:
        - tenta importar;
        - renomeia os que falharam e tenta de novo;
        - converte (áudio→mp3, imagem→png, vídeo→mp4) os que continuarem falhando;
        - renomeia os convertidos se ainda falhar.
        Retorna True se tudo deu certo; False se ainda restarem falhas.
        """
        # 1) tentativa direta
        import_files_result = self.premiere_manager.import_files(
            files_to_import)
        if all(import_files_result.values()):
            return True

        # 2) renomear os que falharam
        renamed_files_to_import: list[str] = []
        for import_path, import_result in import_files_result.items():
            if import_result is True:
                continue
            renamed_file_path = create_renamed_file(paths_map.get(import_path))
            paths_map[import_path] = renamed_file_path
            renamed_files_to_import.append(renamed_file_path)

        import_renamed_files_result = self.premiere_manager.import_files(
            renamed_files_to_import)
        if all(import_renamed_files_result.values()):
            return True

        # 3) converter os que ainda falharem
        converted_files_to_import: list[str] = []
        for renamed_import_path, renamed_import_result in import_renamed_files_result.items():
            if renamed_import_result is True:
                continue

            converted_file_path = ''
            file_type = self.conversion_manager.identify_file_type(
                renamed_import_path)

            if file_type == 'AUDIO':
                audio_conversion_result = self.conversion_manager.convert_audio(
                    renamed_import_path)
                if audio_conversion_result.success is False:
                    messagebox.showerror(
                        'Erro ao converter arquivo de áudio', audio_conversion_result.error)
                    return False
                converted_file_path = audio_conversion_result.data

            elif file_type == 'IMAGE':
                converted_file_path = self.conversion_manager.convert_image(
                    renamed_import_path)

            elif file_type == 'VIDEO':
                converted_file_path = self.conversion_manager.convert_video(
                    renamed_import_path)

            else:
                messagebox.showerror(
                    'Erro ao converter arquivo', f'O arquivo "{renamed_import_path}" é de um tipo não suportado.')
                return False

            # ATENÇÃO: chave correta (renamed_import_path)
            paths_map[renamed_import_path] = converted_file_path
            converted_files_to_import.append(converted_file_path)

        import_converted_files_result = self.premiere_manager.import_files(
            converted_files_to_import)
        if all(import_converted_files_result.values()):
            return True

        # 4) renomear convertidos que ainda falharam
        converted_renamed_files_to_import: list[str] = []
        for converted_import_path, converted_import_result in import_converted_files_result.items():
            if converted_import_result is True:
                continue

            # ATENÇÃO: chave correta (converted_import_path)
            converted_renamed_file_path = create_renamed_file(
                paths_map.get(converted_import_path))
            paths_map[converted_import_path] = converted_renamed_file_path
            converted_renamed_files_to_import.append(
                converted_renamed_file_path)

        import_converted_renamed_files_result = self.premiere_manager.import_files(
            converted_renamed_files_to_import)
        if all(import_converted_renamed_files_result.values()):
            return True

        # ainda restaram falhas
        messagebox.showerror('Erro ao importar arquivos',
                             'Ocorreu um erro desconhecido ao importar ao menos um dos arquivos.')
        return False

    def render(self):
        # Credencial AssemblyAI resolvida sob demanda via property.
        try:
            self._app.config(menu=self._menubar)
        except Exception:
            pass
        self.widget.pack(expand=True)

    def unrender(self):
        self.widget.pack_forget()
