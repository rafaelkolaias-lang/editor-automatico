"""
Tela de Casamento de Cenas com Feedback Visual.

Fluxo:
1. Usuário seleciona arquivos de cenas (videos suportados pelo Gemini) + arquivo de roteiro + pasta de saída.
2. Clica em [Processar] → SceneRenamerManager roda em thread (Gemini + embeddings).
3. Tela transita para revisão: split-screen Listbox (esquerda) x Cards (direita).
4. Usuário revisa, faz ajustes manuais, clica [Aplicar Cópia de Arquivos].
5. Arquivos são copiados/renomeados na pasta de saída.
"""

import os
import sys
import json
import shutil
import threading
import queue
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

from ...managers.SettingsManager import SettingsManager

# ---------- constantes de UI ----------
_UI_CACHE_KEY = "renamer_ui"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}

# Formatos de video que o Gemini aceita nativamente.
# Fonte: https://ai.google.dev/gemini-api/docs/vision
GEMINI_VIDEO_EXTS = {".mp4", ".mpeg", ".mpg", ".mov", ".avi", ".flv",
                     ".webm", ".wmv", ".3gp", ".3gpp"}
CARD_MIN_WIDTH = 260
THUMB_W, THUMB_H = 240, 135  # 16:9
SEM_CENA_LABEL = "⚠ SEM CENA"


def _abrir_arquivo(path: Path):
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        import subprocess
        subprocess.Popen(["open", str(path)])
    else:
        import subprocess
        subprocess.Popen(["xdg-open", str(path)])


class _ToolTip:
    """Tooltip simples que aparece ao passar o mouse sobre um widget."""
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event=None):
        if self.tip_window:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(tw, text=self.text, justify="left",
                       bg="#ffffe0", fg="#333", relief="solid", borderwidth=1,
                       font=("Arial", 9), padx=6, pady=4)
        lbl.pack()

    def _hide(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


def _listar_cenas(pasta: Path) -> tuple[dict, list]:
    """Retorna (mapa nome→Path, lista de nomes) de todas as mídias em pasta."""
    mapa: dict[str, Path] = {}
    for root, _, files in os.walk(str(pasta)):
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in VIDEO_EXTS or ext in IMAGE_EXTS:
                nome = Path(f).stem
                mapa[nome] = Path(root) / f
    nomes = sorted(mapa.keys())
    return mapa, nomes


def _listar_cenas_arquivos(paths: list[str]) -> tuple[dict, list]:
    """Retorna (mapa nome→Path, lista de nomes) a partir de uma lista de arquivos."""
    mapa: dict[str, Path] = {}
    for p in paths:
        path_obj = Path(p)
        if not path_obj.is_file():
            continue
        ext = path_obj.suffix.lower()
        if ext not in GEMINI_VIDEO_EXTS and ext not in IMAGE_EXTS:
            continue
        nome = path_obj.stem
        mapa[nome] = path_obj
    nomes = sorted(mapa.keys())
    return mapa, nomes


class RenamerFeedbackScreen:
    """
    Janela Toplevel que integra SceneRenamerManager ao app principal.
    Abre a partir de um botão na MainScreen.
    """

    def __init__(self, master: tk.Tk, settings_manager: SettingsManager):
        self.master = master
        self.settings_manager = settings_manager
        self.top = tk.Toplevel(master)
        self.top.title("Renomeador de Cenas — Feedback Visual")
        self.top.geometry("1400x780")
        self.top.protocol("WM_DELETE_WINDOW", self._on_close)

        # estado
        self._assignments: list[dict] = []        # [{phrase, assigned_scene}]
        self._scene_paths: dict[str, Path] = {}   # nome → Path
        self._scene_names: list[str] = []

        # vars de controle de preview de vídeo
        self.hover_scene_name: Optional[str] = None
        self.hover_cap = None
        self.hover_running = False
        self.thumbnails: dict[str, "ImageTk.PhotoImage"] = {}
        self.video_thumbs: dict[str, "ImageTk.PhotoImage"] = {}
        self.video_labels: dict[str, tk.Label] = {}
        self.scene_cards: dict[str, tk.Widget] = {}
        self.scene_vars: dict[str, tk.IntVar] = {}
        self._last_cols_count: Optional[int] = None

        # filtro da listbox
        self.filter_var = tk.StringVar(value="Todos")
        self.filtered_indices: list[int] = []
        self.selected_scene_var = tk.StringVar(value="")

        # fila de logs (thread-safe)
        self._log_queue: queue.Queue = queue.Queue()

        # estado para reprocessamento e undo
        self._last_script_items = None
        self._last_scene_descs = None
        self._last_manager = None
        self._last_copied_files: list[str] = []

        # thread de stop
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        self._build_setup_phase()
        self.top.after(100, self._poll_log_queue)

    # ------------------------------------------------------------------ #
    # FASE 1 – configuração / processamento
    # ------------------------------------------------------------------ #

    def _build_setup_phase(self):
        """Monta a tela de seleção de pastas + botão Processar."""
        self._setup_frame = tk.Frame(self.top)
        self._setup_frame.pack(fill="both", expand=True, padx=16, pady=12)

        tk.Label(self._setup_frame, text="Renomeador de Cenas",
                 font=("Arial", 14, "bold")).pack(anchor="w")
        tk.Label(self._setup_frame,
                 text="Selecione os arquivos e processe o casamento semântico antes de revisar.",
                 wraplength=800).pack(anchor="w", pady=(0, 10))

        # Arquivos de cenas (vídeos suportados pelo Gemini)
        row_cenas = tk.Frame(self._setup_frame)
        row_cenas.pack(fill="x", pady=3)
        tk.Label(row_cenas, text="Arquivos de cenas:", width=18, anchor="w").pack(side="left")
        self._cenas_var = tk.StringVar()
        self._cenas_var.trace_add("write", lambda *_: self._refresh_cenas_info())
        tk.Entry(row_cenas, textvariable=self._cenas_var, width=60).pack(side="left", padx=4)
        tk.Button(row_cenas, text="Escolher",
                  command=self._pick_cenas).pack(side="left")

        # Label com resumo (ex: "3 arquivos selecionados" ou "pasta: /caminho")
        row_cenas_info = tk.Frame(self._setup_frame)
        row_cenas_info.pack(fill="x")
        tk.Label(row_cenas_info, text="", width=18).pack(side="left")
        self._cenas_info_var = tk.StringVar(value="")
        tk.Label(row_cenas_info, textvariable=self._cenas_info_var,
                 fg="#0078D7", anchor="w").pack(side="left", fill="x", expand=True)

        # Arquivo de roteiro
        row_rot = tk.Frame(self._setup_frame)
        row_rot.pack(fill="x", pady=3)
        tk.Label(row_rot, text="Arquivo de roteiro:", width=18, anchor="w").pack(side="left")
        self._roteiro_var = tk.StringVar()
        tk.Entry(row_rot, textvariable=self._roteiro_var, width=60).pack(side="left", padx=4)
        tk.Button(row_rot, text="Escolher",
                  command=self._pick_roteiro).pack(side="left")

        # Pasta de saída
        row_out = tk.Frame(self._setup_frame)
        row_out.pack(fill="x", pady=3)
        tk.Label(row_out, text="Pasta de saída:", width=18, anchor="w").pack(side="left")
        self._output_var = tk.StringVar()
        tk.Entry(row_out, textvariable=self._output_var, width=60).pack(side="left", padx=4)
        tk.Button(row_out, text="Escolher",
                  command=self._pick_output).pack(side="left")

        # Opções linha 1
        row_opts = tk.Frame(self._setup_frame)
        row_opts.pack(fill="x", pady=(6, 2))
        self._allow_pro_var = tk.IntVar(value=1)
        tk.Checkbutton(row_opts, text="Usar Gemini Pro como fallback",
                       variable=self._allow_pro_var).pack(side="left")
        self._include_audio_var = tk.IntVar(value=1)
        tk.Checkbutton(row_opts, text="Incluir áudio do vídeo na análise",
                       variable=self._include_audio_var).pack(side="left", padx=10)
        self._allow_reuse_var = tk.IntVar(value=1)
        tk.Checkbutton(row_opts, text="Permitir repetir cena",
                       variable=self._allow_reuse_var).pack(side="left", padx=10)

        # Opções linha 2: parâmetros numéricos com tooltips
        row_opts2 = tk.Frame(self._setup_frame)
        row_opts2.pack(fill="x", pady=(0, 4))

        def _add_field_with_tip(parent, label_text, tip_text, var, from_, to_):
            frm = tk.Frame(parent)
            frm.pack(side="left", padx=(0, 10))
            tk.Label(frm, text=label_text).pack(side="left")
            spn = tk.Spinbox(frm, from_=from_, to=to_, width=3, textvariable=var)
            spn.pack(side="left", padx=2)
            tip_lbl = tk.Label(frm, text="(?)", fg="#0078D7", cursor="hand2", font=("Arial", 9, "bold"))
            tip_lbl.pack(side="left")
            _ToolTip(tip_lbl, tip_text)

        self._n_words_var = tk.IntVar(value=6)
        _add_field_with_tip(row_opts2, "Palavras no nome:",
                            "Quantas palavras da frase serão usadas\npara nomear o arquivo copiado.\nEx: 6 = primeiras 6 palavras.",
                            self._n_words_var, 2, 20)

        self._sentences_per_chunk_var = tk.IntVar(value=2)
        _add_field_with_tip(row_opts2, "Frases por item:",
                            "Agrupa N frases do roteiro em 1 item\npara o casamento com cenas.\n1 = cada frase vira 1 item.\n2 = cada 2 frases viram 1 item.",
                            self._sentences_per_chunk_var, 1, 5)

        self._max_uses_var = tk.IntVar(value=2)
        _add_field_with_tip(row_opts2, "Repetições:",
                            "Quantas vezes a mesma cena pode ser\nusada em frases diferentes.\n1 = cada cena usada 1 vez.\n3 = mesma cena pode aparecer em até 3 frases.",
                            self._max_uses_var, 1, 10)

        # Barra de progresso + log
        self._progress_var = tk.DoubleVar(value=0)
        self._progress_bar = ttk.Progressbar(self._setup_frame,
                                             variable=self._progress_var,
                                             maximum=100)
        self._progress_bar.pack(fill="x", pady=(8, 2))

        self._log_text = tk.Text(self._setup_frame, height=12, state="disabled",
                                 wrap="word", bg="#1e1e1e", fg="#d4d4d4")
        self._log_text.pack(fill="both", expand=True, pady=4)
        log_scroll = ttk.Scrollbar(self._setup_frame, orient="vertical",
                                   command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_scroll.set)

        # Botões de ação
        row_btns = tk.Frame(self._setup_frame)
        row_btns.pack(fill="x", pady=8)
        self._btn_processar = tk.Button(row_btns, text="▶  Processar",
                                        bg="#0078D7", fg="white",
                                        font=("Arial", 11, "bold"),
                                        command=self._start_processing)
        self._btn_processar.pack(side="left", padx=4)

        self._btn_cancelar = tk.Button(row_btns, text="Cancelar",
                                       state="disabled",
                                       command=self._cancel_processing)
        self._btn_cancelar.pack(side="left", padx=4)

        self._btn_reprocess = tk.Button(row_btns, text="Reprocessar Pendências",
                                         bg="#D4A017", fg="white",
                                         font=("Arial", 10, "bold"),
                                         command=self._reprocess_pending)
        self._btn_reprocess.pack(side="left", padx=4)

        self._btn_undo = tk.Button(row_btns, text="Desfazer Última",
                                    command=self._undo_last_run)
        self._btn_undo.pack(side="left", padx=4)

        self._btn_revisar = tk.Button(row_btns, text="Ir para Revisão →",
                                      state="disabled", bg="#107C10", fg="white",
                                      font=("Arial", 11, "bold"),
                                      command=self._show_review_phase)
        self._btn_revisar.pack(side="right", padx=4)

        # Restaura configurações salvas
        self._load_ui_cache()

    # ---------- cache de UI ----------

    def _load_ui_cache(self):
        try:
            settings = self.settings_manager.read_settings()
            cfg = settings.get(_UI_CACHE_KEY) or {}
            sp = getattr(self.settings_manager, 'SETTINGS_PATH', '?')
            if cfg:
                self._log(f"Config anterior carregada de: {sp}")
            else:
                self._log(f"Nenhuma config anterior encontrada em: {sp}")

            if cfg.get("cenas_dir"):
                self._cenas_var.set(cfg["cenas_dir"])
            if cfg.get("roteiro_file"):
                self._roteiro_var.set(cfg["roteiro_file"])
            if cfg.get("output_dir"):
                self._output_var.set(cfg["output_dir"])
            if "allow_pro" in cfg:
                self._allow_pro_var.set(int(cfg["allow_pro"]))
            if "include_audio" in cfg:
                self._include_audio_var.set(int(cfg["include_audio"]))
            if "allow_reuse" in cfg:
                self._allow_reuse_var.set(int(cfg["allow_reuse"]))
            if "n_words" in cfg:
                self._n_words_var.set(int(cfg["n_words"]))
            if "sentences_per_chunk" in cfg:
                self._sentences_per_chunk_var.set(int(cfg["sentences_per_chunk"]))
            if "max_uses" in cfg:
                self._max_uses_var.set(int(cfg["max_uses"]))
        except Exception as e:
            try:
                self._log(f"[ERRO] Falha ao carregar config anterior: {e}")
            except Exception:
                pass

    def _save_ui_cache(self):
        try:
            settings = self.settings_manager.read_settings()
            settings[_UI_CACHE_KEY] = {
                "cenas_dir": self._cenas_var.get(),
                "roteiro_file": self._roteiro_var.get(),
                "output_dir": self._output_var.get(),
                "allow_pro": self._allow_pro_var.get(),
                "include_audio": self._include_audio_var.get(),
                "allow_reuse": self._allow_reuse_var.get(),
                "n_words": self._n_words_var.get(),
                "sentences_per_chunk": self._sentences_per_chunk_var.get(),
                "max_uses": self._max_uses_var.get(),
            }
            self.settings_manager.write_settings(settings)
        except Exception:
            pass

    def _refresh_cenas_info(self):
        """Mostra um resumo amigavel ao lado do campo de cenas."""
        try:
            raw = self._cenas_var.get().strip()
            if not raw:
                self._cenas_info_var.set("")
                return
            if os.path.isdir(raw):
                self._cenas_info_var.set(f"Pasta: {raw}")
                return
            files = [p for p in raw.split(";") if p.strip()]
            if len(files) == 1:
                self._cenas_info_var.set(f"1 arquivo: {Path(files[0]).name}")
            elif files:
                nomes = ", ".join(Path(p).name for p in files[:3])
                sufixo = f" e +{len(files) - 3}" if len(files) > 3 else ""
                self._cenas_info_var.set(f"{len(files)} arquivos: {nomes}{sufixo}")
        except Exception:
            pass

    def _pick_cenas(self):
        # Filetypes: todos os formatos de video aceitos pelo Gemini
        exts_gemini = " ".join(f"*{e}" for e in sorted(GEMINI_VIDEO_EXTS))
        files = filedialog.askopenfilenames(
            title="Selecione os arquivos de cenas (videos)",
            filetypes=[
                ("Videos suportados (Gemini)", exts_gemini),
                ("Todos", "*.*"),
            ],
            parent=self.top)
        if not files:
            return

        # Filtra por extensao compativel (caso o usuario escolha via "Todos")
        aceitos = []
        rejeitados = []
        for f in files:
            if Path(f).suffix.lower() in GEMINI_VIDEO_EXTS:
                aceitos.append(f)
            else:
                rejeitados.append(Path(f).name)

        if rejeitados:
            messagebox.showwarning(
                "Arquivos ignorados",
                "Estes arquivos nao sao suportados pelo Gemini e serao ignorados:\n\n"
                + "\n".join(rejeitados[:10])
                + ("\n..." if len(rejeitados) > 10 else ""),
                parent=self.top)

        if not aceitos:
            return

        # Armazena como string separada por ';' (compativel com StringVar)
        self._cenas_var.set(";".join(aceitos))
        self._save_ui_cache()

    def _pick_roteiro(self):
        f = filedialog.askopenfilename(
            title="Selecione o arquivo de roteiro",
            filetypes=[("Texto", "*.txt"), ("Todos", "*.*")],
            parent=self.top)
        if f:
            self._roteiro_var.set(f)
            self._save_ui_cache()

    def _pick_output(self):
        d = filedialog.askdirectory(title="Selecione a pasta de saída", parent=self.top)
        if d:
            self._output_var.set(d)
            self._save_ui_cache()

    # ---------- processamento ----------

    def _start_processing(self):
        cenas_raw = self._cenas_var.get().strip()
        roteiro_file = self._roteiro_var.get().strip()

        if not cenas_raw:
            messagebox.showerror("Erro", "Selecione os arquivos de cenas.", parent=self.top)
            return

        # Pode ser: uma pasta (legado) OU uma lista de arquivos separada por ';'
        cenas_is_dir = os.path.isdir(cenas_raw)
        cenas_files: list[str] = []
        if cenas_is_dir:
            # Compatibilidade retroativa: ainda aceita pasta
            cenas_dir = cenas_raw
        else:
            cenas_files = [p for p in cenas_raw.split(";") if p.strip()]
            faltando = [p for p in cenas_files if not os.path.isfile(p)]
            if faltando:
                messagebox.showerror(
                    "Erro",
                    "Estes arquivos nao existem mais:\n\n"
                    + "\n".join(faltando[:10])
                    + ("\n..." if len(faltando) > 10 else "")
                    + "\n\nSelecione os arquivos novamente.",
                    parent=self.top)
                return
            if not cenas_files:
                messagebox.showerror(
                    "Erro",
                    f"Nao foi possivel interpretar a selecao:\n{cenas_raw}\n\nSelecione os arquivos de cenas.",
                    parent=self.top)
                return
            # Primeiro arquivo define o diretorio base usado internamente (logs/caches)
            cenas_dir = str(Path(cenas_files[0]).parent)
        if not roteiro_file:
            messagebox.showerror("Erro", "Selecione um arquivo de roteiro.", parent=self.top)
            return
        if not os.path.isfile(roteiro_file):
            messagebox.showerror(
                "Erro",
                f"O arquivo de roteiro nao existe mais:\n{roteiro_file}\n\nEscolha um arquivo valido.",
                parent=self.top)
            return

        from core.remote_credentials import get_api_key
        gemini_key = get_api_key("GEMINI_API_KEY")
        if not gemini_key:
            messagebox.showerror(
                "Credencial Gemini indisponivel",
                "Nao foi possivel obter a chave Gemini do servidor. "
                "Verifique seu login e tente novamente.",
                parent=self.top)
            return

        try:
            with open(roteiro_file, "r", encoding="utf-8") as fh:
                roteiro_text = fh.read()
        except Exception as e:
            messagebox.showerror("Erro ao ler roteiro", str(e), parent=self.top)
            return

        # carrega cenas (lista de arquivos selecionados ou pasta legada)
        if cenas_is_dir:
            self._scene_paths, self._scene_names = _listar_cenas(Path(cenas_dir))
        else:
            self._scene_paths, self._scene_names = _listar_cenas_arquivos(cenas_files)
        if not self._scene_names:
            messagebox.showerror("Erro", "Nenhuma cena suportada foi encontrada.", parent=self.top)
            return

        self._log(f"Cenas encontradas: {len(self._scene_names)}")
        self._log(f"Roteiro: {len(roteiro_text)} caracteres")

        self._save_ui_cache()
        self._stop_event.clear()
        self._btn_processar.config(state="disabled")
        self._btn_cancelar.config(state="normal")
        self._btn_revisar.config(state="disabled")
        self._progress_var.set(0)

        self._worker_thread = threading.Thread(
            target=self._processing_worker,
            args=(gemini_key, roteiro_text, cenas_dir),
            daemon=True)
        self._worker_thread.start()

    def _processing_worker(self, gemini_key: str, roteiro_text: str, cenas_dir: str):
        try:
            from ...managers.SceneRenamerManager import (
                SceneRenamerManager,
                ProcessingCancelled,
                QuotaExhaustedError,
                _is_rate_limit_error,
            )
            from ...utils.renamer_utils import build_script_items
        except ImportError as e:
            self._log(f"[ERRO] Não foi possível importar SceneRenamerManager: {e}")
            self._log("[ERRO] Verifique se o módulo foi instalado corretamente.")
            self.top.after(0, lambda: self._btn_processar.config(state="normal"))
            self.top.after(0, lambda: self._btn_cancelar.config(state="disabled"))
            return

        script_items = None
        scene_descs = None
        manager = None

        try:
            manager = SceneRenamerManager(
                gemini_api_key=gemini_key,
                log_fn=self._log,
            )

            # 1. Divide o roteiro em frases
            spc = max(1, self._sentences_per_chunk_var.get())
            script_items = build_script_items(roteiro_text, sentences_per_chunk=spc)
            self._log(f"Frases no roteiro: {len(script_items)} (frases/item={spc})")

            total_scenes = len(self._scene_names)

            def _on_progress(n: int):
                pct = int(n / total_scenes * 50)  # 0-50% para descrição
                self.top.after(0, lambda p=pct: self._progress_var.set(p))

            # 2. Descreve todas as cenas em paralelo
            self._log("Descrevendo cenas com Gemini...")
            scene_files = [str(self._scene_paths[n]) for n in self._scene_names]
            scene_descs = manager.describe_scenes(
                selected_files=scene_files,
                allow_pro_fallback=bool(self._allow_pro_var.get()),
                include_video_audio=bool(self._include_audio_var.get()),
                stop_event=self._stop_event,
                progress_fn=_on_progress,
            )
            self._progress_var.set(55)

            if self._stop_event.is_set():
                raise ProcessingCancelled("Cancelado pelo usuário.")

            # Preserva o estado de descricao antes do matching — assim, se a
            # etapa seguinte cair por rate limit, o usuario consegue reprocessar
            # sem precisar redescrever tudo do zero.
            self._last_script_items = script_items
            self._last_scene_descs = scene_descs
            self._last_manager = manager
            self._assignments = [
                {
                    "index": i,
                    "script_fragment": phrase,
                    "assigned_scene": None,
                }
                for i, phrase in enumerate(script_items)
            ]

            # 3. Matching semântico
            max_uses = max(1, self._max_uses_var.get()) if self._allow_reuse_var.get() else 1
            self._log(f"Calculando casamento semântico (max_usos/cena={max_uses})...")
            try:
                assignments_result = manager.compute_assignments(
                    script_items=script_items,
                    scene_descs=scene_descs,
                    max_uses_per_scene=max_uses,
                )
            except QuotaExhaustedError as qe:
                self._log("")
                self._log("[RATE LIMIT] O Gemini recusou mais embeddings por cota/limite temporario.")
                self._log("             As descricoes das cenas ja foram preservadas no cache local.")
                self._log("             Aguarde alguns minutos e use 'Reprocessar Pendencias' para tentar o")
                self._log("             casamento de novo — o trabalho anterior NAO sera perdido.")
                self._log(f"             Detalhe: {qe}")
                self._progress_var.set(55)
                # Habilita revisao parcial (frases sem cena) e o botao de reprocess
                self.top.after(0, lambda: self._btn_revisar.config(state="normal"))
                return
            # compute_assignments devolve (List[Assignment], Set[int], Set[int])
            assignments_list = assignments_result[0] if isinstance(assignments_result, tuple) else assignments_result
            self._progress_var.set(90)

            # 4. Preenche cenas atribuidas sobre o esqueleto criado acima
            for i in range(len(script_items)):
                assigned = None
                for asn in assignments_list:
                    if asn.phrase_idx == i:
                        assigned = self._scene_names[asn.scene_idx] if asn.scene_idx < len(self._scene_names) else None
                        break
                self._assignments[i]["assigned_scene"] = assigned

            self._progress_var.set(100)
            matched = sum(1 for a in self._assignments if a['assigned_scene'])
            pending = len(self._assignments) - matched
            self._log(f"Concluído! {matched} de {len(self._assignments)} frases com cena atribuída.")
            if pending > 0:
                self._log(f"[PENDENTE] {pending} frase(s) sem cena. Use 'Reprocessar Pendências' para tentar novamente.")

            self.top.after(0, lambda: self._btn_revisar.config(state="normal"))

        except ProcessingCancelled:
            self._log("Processamento cancelado.")
        except Exception as e:
            if _is_rate_limit_error(e):
                self._log("")
                self._log("[RATE LIMIT] Gemini temporariamente sem cota para esta operacao.")
                self._log("             Aguarde alguns minutos e tente novamente.")
                if scene_descs:
                    self._log("             As descricoes ja geradas estao no cache — nada sera redescrito.")
                self._log(f"             Detalhe: {e}")
                if scene_descs:
                    self.top.after(0, lambda: self._btn_revisar.config(state="normal"))
            else:
                self._log(f"[ERRO] {e}")
                import traceback
                self._log(traceback.format_exc())
        finally:
            # Guard: a janela Toplevel pode ter sido fechada enquanto o worker
            # estava processando — nesse caso, pular o reset dos botoes.
            def _reset_buttons():
                try:
                    if self._btn_processar.winfo_exists():
                        self._btn_processar.config(state="normal")
                    if self._btn_cancelar.winfo_exists():
                        self._btn_cancelar.config(state="disabled")
                except Exception:
                    pass
            try:
                if self.top.winfo_exists():
                    self.top.after(0, _reset_buttons)
            except Exception:
                pass

    def _cancel_processing(self):
        self._stop_event.set()
        self._log("Cancelando...")

    def _reprocess_pending(self):
        """Reprocessa apenas as frases que ficaram sem cena na última execução."""
        if not hasattr(self, '_last_script_items') or not hasattr(self, '_last_scene_descs'):
            messagebox.showwarning("Aviso", "Execute o processamento primeiro.", parent=self.top)
            return
        if not self._assignments:
            messagebox.showwarning("Aviso", "Nenhum resultado anterior.", parent=self.top)
            return

        pending_idxs = [a["index"] for a in self._assignments if not a.get("assigned_scene")]
        if not pending_idxs:
            messagebox.showinfo("Info", "Nenhuma frase pendente.", parent=self.top)
            return

        self._log(f"\n{'='*50}")
        self._log(f"[REPROCESS] Reprocessando {len(pending_idxs)} frases pendentes...")

        max_uses = max(1, self._max_uses_var.get()) if self._allow_reuse_var.get() else 1

        # Cenas já usadas — contar usos atuais
        from ...utils.renamer_utils import Assignment as _Asn
        scene_use_counts: dict = {}
        for a in self._assignments:
            if a.get("assigned_scene"):
                sn = a["assigned_scene"]
                si = self._scene_names.index(sn) if sn in self._scene_names else -1
                if si >= 0:
                    scene_use_counts[si] = scene_use_counts.get(si, 0) + 1

        # Sub-roteiro e sub-cenas para reprocessar
        pending_script = [self._last_script_items[i] for i in pending_idxs]

        try:
            self._log("Calculando casamento para pendentes...")
            result = self._last_manager.compute_assignments(
                script_items=pending_script,
                scene_descs=self._last_scene_descs,
                max_uses_per_scene=max(max_uses, 2),  # mais flexível no reprocess
                initial_scene_use_counts=scene_use_counts,
                min_assign_score=0.70,  # score mais baixo para pendentes
            )
            new_assignments = result[0] if isinstance(result, tuple) else result

            resolved = 0
            for new_a in new_assignments:
                orig_idx = pending_idxs[new_a.phrase_idx]
                scene_idx = new_a.scene_idx
                if scene_idx < len(self._scene_names):
                    self._assignments[orig_idx]["assigned_scene"] = self._scene_names[scene_idx]
                    resolved += 1

            still_pending = len(pending_idxs) - resolved
            self._log(f"[REPROCESS] Resolvidas: {resolved} | Ainda pendentes: {still_pending}")

            if hasattr(self, '_review_frame') and self._review_frame.winfo_ismapped():
                self._populate_review_list()

        except Exception as e:
            self._log(f"[REPROCESS ERRO] {e}")

    def _undo_last_run(self):
        """Desfaz a última cópia de arquivos (remove arquivos copiados)."""
        if not hasattr(self, '_last_copied_files') or not self._last_copied_files:
            messagebox.showwarning("Aviso", "Nenhuma execução anterior para desfazer.", parent=self.top)
            return

        resp = messagebox.askyesno(
            "Confirmar",
            f"Remover {len(self._last_copied_files)} arquivo(s) copiados na última execução?",
            parent=self.top)
        if not resp:
            return

        removed = 0
        erros = []
        for path in self._last_copied_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
                    removed += 1
            except Exception as e:
                erros.append(f"{os.path.basename(path)}: {e}")

        self._last_copied_files = []
        msg = f"{removed} arquivo(s) removido(s)."
        if erros:
            msg += f"\nErros: {len(erros)}\n" + "\n".join(erros[:5])
        self._log(f"[UNDO] {msg}")
        messagebox.showinfo("Desfazer", msg, parent=self.top)

    # ------------------------------------------------------------------ #
    # FASE 2 – revisão (split-screen)
    # ------------------------------------------------------------------ #

    def _show_review_phase(self):
        # oculta a tela de setup
        self._setup_frame.pack_forget()

        # Tela de carregamento
        self._loading_frame = tk.Frame(self.top)
        self._loading_frame.pack(fill="both", expand=True)
        self._loading_label = tk.Label(self._loading_frame, text="Carregando revisão...",
                                        font=("Arial", 14), fg="#555")
        self._loading_label.pack(expand=True)
        self.top.update_idletasks()

        # Constrói a review escondida, só mostra quando tudo estiver pronto
        self.top.after(50, self._build_review_phase)

    def _build_review_phase(self):
        # Cria o frame escondido (não faz pack ainda)
        self._review_frame = tk.Frame(self.top)

        # PanedWindow horizontal
        pane = tk.PanedWindow(self._review_frame, orient="horizontal", sashrelief="raised")
        pane.pack(fill="both", expand=True, padx=4, pady=4)

        # ----- PAINEL ESQUERDO: frases -----
        frame_left = ttk.Frame(pane)
        pane.add(frame_left, width=420)

        ttk.Label(frame_left, text="Roteiro × Cenas",
                  font=("Arial", 11, "bold")).pack(anchor="w", padx=6, pady=(6, 2))

        # filtro
        row_filter = ttk.Frame(frame_left)
        row_filter.pack(fill="x", padx=6, pady=2)
        ttk.Label(row_filter, text="Exibir:").pack(side="left")
        for lbl, val in [("Todos", "Todos"), ("Sem cena", "Sem cena"), ("Com cena", "Com cena")]:
            ttk.Radiobutton(row_filter, text=lbl, value=val,
                            variable=self.filter_var,
                            command=self._rebuild_trechos_list).pack(side="left", padx=3)

        # listbox
        frame_lb = ttk.Frame(frame_left)
        frame_lb.pack(fill="both", expand=True, padx=6)

        self.list_trechos = tk.Listbox(frame_lb, exportselection=False, width=46,
                                       selectbackground="#0078D7", selectforeground="white")
        sb_lb = ttk.Scrollbar(frame_lb, orient="vertical", command=self.list_trechos.yview)
        self.list_trechos.configure(yscrollcommand=sb_lb.set)
        self.list_trechos.pack(side="left", fill="both", expand=True)
        sb_lb.pack(side="right", fill="y")
        self.list_trechos.bind("<<ListboxSelect>>", self._on_select_trecho)

        # texto completo do trecho
        ttk.Label(frame_left, text="Trecho selecionado:").pack(anchor="w", padx=6, pady=(6, 0))
        self.text_trecho = tk.Text(frame_left, height=5, wrap="word",
                                   state="disabled", bg="#f5f5f5")
        self.text_trecho.pack(fill="x", padx=6, pady=2)

        # cena atual
        self.lbl_cena_atual = ttk.Label(frame_left, text="Cena: (nenhuma)",
                                        foreground="#555")
        self.lbl_cena_atual.pack(anchor="w", padx=6, pady=2)

        # ----- PAINEL DIREITO: cards de cenas -----
        frame_right = ttk.Frame(pane)
        pane.add(frame_right)

        ttk.Label(frame_right, text="Mídias Disponíveis",
                  font=("Arial", 11, "bold")).pack(anchor="w", padx=6, pady=(6, 2))

        # canvas rolável para os cards
        frame_canvas_outer = ttk.Frame(frame_right)
        frame_canvas_outer.pack(fill="both", expand=True, padx=4)

        self.canvas_cenas = tk.Canvas(frame_canvas_outer, bg="#2b2b2b")
        self.canvas_cenas.bind("<Enter>", lambda e: self._bind_mousewheel())
        self.canvas_cenas.bind("<Leave>", lambda e: self._unbind_mousewheel())

        sb_canvas = ttk.Scrollbar(frame_canvas_outer, orient="vertical",
                                  command=self.canvas_cenas.yview)
        self.canvas_cenas.configure(yscrollcommand=sb_canvas.set)
        self.canvas_cenas.pack(side="left", fill="both", expand=True)
        sb_canvas.pack(side="right", fill="y")

        self.scenes_container = ttk.Frame(self.canvas_cenas)
        self._scenes_window_id = self.canvas_cenas.create_window(
            (0, 0), window=self.scenes_container, anchor="nw")

        self.scenes_container.bind("<Configure>", lambda e: self.canvas_cenas.configure(
            scrollregion=self.canvas_cenas.bbox("all")))
        self.canvas_cenas.bind("<Configure>", self._on_canvas_configure)

        # ----- BARRA INFERIOR -----
        frame_bottom = tk.Frame(self._review_frame, bg="#f0f0f0", pady=6)
        frame_bottom.pack(fill="x", side="bottom", padx=8)

        self._total_label = tk.Label(frame_bottom, text="", bg="#f0f0f0")
        self._total_label.pack(side="left", padx=8)

        tk.Button(frame_bottom, text="← Voltar ao Setup",
                  command=self._back_to_setup).pack(side="left", padx=4)

        tk.Button(frame_bottom,
                  text="✓  Aplicar Cópia de Arquivos",
                  bg="#107C10", fg="white",
                  font=("Arial", 11, "bold"),
                  command=self._apply_copies).pack(side="right", padx=8)

        # Popula a listbox imediatamente (leve)
        self._rebuild_trechos_list()
        # Cards são criados progressivamente para não travar
        if self._scene_names:
            self._build_scene_items_progressive(0)
        else:
            self._reveal_review()

    def _back_to_setup(self):
        if hasattr(self, "_review_frame"):
            self._review_frame.destroy()
            del self._review_frame
        # Limpa os widgets do painel direito para serem recriados na próxima visita
        self.scene_cards.clear()
        self.scene_vars.clear()
        self.video_labels.clear()
        self.thumbnails.clear()
        self._last_cols_count = 0
        self._setup_frame.pack(fill="both", expand=True, padx=16, pady=12)

    # -------- listbox (esquerda) --------

    def _rebuild_trechos_list(self):
        modo = self.filter_var.get()
        self.list_trechos.delete(0, "end")
        self.filtered_indices = []

        for idx, item in enumerate(self._assignments):
            has_scene = bool(item.get("assigned_scene"))
            if modo == "Sem cena" and has_scene:
                continue
            if modo == "Com cena" and not has_scene:
                continue

            frag = (item.get("script_fragment") or "").replace("\n", " ")
            if len(frag) > 75:
                frag = frag[:72] + "..."
            cena = item.get("assigned_scene") or SEM_CENA_LABEL
            display = f"{item['index']:03d}  {frag[:40]}…  →  {cena}"

            row = self.list_trechos.size()
            self.list_trechos.insert("end", display)
            self.list_trechos.itemconfig(row,
                                         fg="black" if has_scene else "#cc3300",
                                         bg="white" if has_scene else "#fff3f0")
            self.filtered_indices.append(idx)

        self._update_total_label()

    def _update_total_label(self):
        total = len(self._assignments)
        com = sum(1 for a in self._assignments if a.get("assigned_scene"))
        sem = total - com
        if hasattr(self, "_total_label"):
            self._total_label.config(text=f"{com} com cena  |  {sem} sem cena  |  {total} total")

    def _get_trecho_index(self) -> Optional[int]:
        sel = self.list_trechos.curselection()
        if not sel:
            return None
        row = sel[0]
        if row < 0 or row >= len(self.filtered_indices):
            return None
        return self.filtered_indices[row]

    def _on_select_trecho(self, event=None):
        idx = self._get_trecho_index()
        if idx is None:
            return
        item = self._assignments[idx]
        frag = (item.get("script_fragment") or "").strip()
        self.text_trecho.configure(state="normal")
        self.text_trecho.delete("1.0", "end")
        self.text_trecho.insert("1.0", frag)
        self.text_trecho.configure(state="disabled")

        cena = item.get("assigned_scene")
        if cena:
            self.lbl_cena_atual.config(text=f"Cena: {cena}", foreground="#107C10")
            self._scroll_to_scene(cena)
        else:
            self.lbl_cena_atual.config(text="Cena: (nenhuma)", foreground="#cc3300")

    # -------- cards de cenas (direita) --------

    def _build_scene_items_progressive(self, start: int, batch: int = 10):
        """Cria cards em lotes progressivos para não travar a UI."""
        if start == 0:
            self._pending_thumbs: list = []
            self._building_cards = True

        names = self._scene_names
        end = min(start + batch, len(names))

        for i in range(start, end):
            name = names[i]
            if name in self.scene_cards:
                continue

            card = tk.Frame(self.scenes_container, bd=1, relief="solid",
                            padx=4, pady=4, bg="white")
            self.scene_cards[name] = card

            var = tk.IntVar(value=0)
            self.scene_vars[name] = var

            top_row = tk.Frame(card, bg="white")
            top_row.pack(fill="x")
            chk = tk.Checkbutton(top_row, variable=var, bg="white",
                                 command=lambda n=name: self._on_scene_click(n))
            chk.pack(side="left")
            lbl_name = tk.Label(top_row, text=name, wraplength=280,
                                justify="left", bg="white", font=("Arial", 9))
            lbl_name.pack(side="left", padx=2)

            # Placeholder com tamanho fixo em pixels (16:9)
            thumb_frame = tk.Frame(card, width=THUMB_W, height=THUMB_H, bg="#e8e8e8")
            thumb_frame.pack_propagate(False)
            thumb_frame.pack(pady=4)
            lbl_thumb = tk.Label(thumb_frame, text="carregando...", bg="#e8e8e8", fg="#999")
            lbl_thumb.pack(fill="both", expand=True)
            lbl_thumb._thumb_frame = thumb_frame  # referência para manter o tamanho

            for w in (card, lbl_thumb, lbl_name, top_row):
                w.bind("<Button-1>", lambda e, n=name: self._on_scene_click(n))

            self._pending_thumbs.append((name, lbl_thumb))

        if end < len(names):
            self.top.after(5, lambda: self._build_scene_items_progressive(end, batch))
        else:
            # Todos os cards criados — agora faz o grid uma única vez e carrega thumbnails
            self._building_cards = False
            self._last_cols_count = None
            self._rebuild_scene_grid()
            self._load_thumbs_batch(0)

    def _load_thumbs_batch(self, start: int, batch_size: int = 6):
        """Carrega thumbnails em lotes de batch_size, cedendo controle à UI entre lotes."""
        if not hasattr(self, '_pending_thumbs') or start >= len(self._pending_thumbs):
            return

        end = min(start + batch_size, len(self._pending_thumbs))
        for i in range(start, end):
            name, lbl_thumb = self._pending_thumbs[i]
            path = self._scene_paths.get(name)
            ext = path.suffix.lower() if path else ""

            if path and ext in IMAGE_EXTS and PIL_AVAILABLE:
                thumb = self._get_img_thumbnail(name, path)
                if thumb:
                    lbl_thumb.configure(image=thumb, text="")
                    lbl_thumb.image = thumb
                else:
                    lbl_thumb.configure(text="sem prévia")

            elif path and ext in VIDEO_EXTS and CV2_AVAILABLE and PIL_AVAILABLE:
                thumb = self._get_vid_thumbnail(name, path)
                bg_color = "#3a3a3a"
                lbl_thumb.configure(bg=bg_color)
                if thumb:
                    lbl_thumb.configure(image=thumb, text="")
                    lbl_thumb.image = thumb
                else:
                    lbl_thumb.configure(text="Passe o mouse\npara prévia",
                                        fg="white")
                self.video_labels[name] = lbl_thumb
                lbl_thumb.bind("<Enter>",
                               lambda e, n=name, p=path, l=lbl_thumb: self._on_video_enter(n, p, l))
                lbl_thumb.bind("<Leave>", lambda e, n=name: self._on_video_leave(n))

            else:
                lbl_thumb.configure(text="sem prévia", bg="#ddd")

        if end < len(self._pending_thumbs):
            self.top.after(10, lambda: self._load_thumbs_batch(end, batch_size))
        else:
            self._reveal_review()

    def _reveal_review(self):
        """Remove tela de carregamento e mostra a revisão completa."""
        if hasattr(self, '_loading_frame') and self._loading_frame.winfo_exists():
            self._loading_frame.destroy()
            del self._loading_frame
        if not self._review_frame.winfo_ismapped():
            self._review_frame.pack(fill="both", expand=True)

    def _on_canvas_configure(self, event):
        self.canvas_cenas.itemconfig(self._scenes_window_id, width=event.width)
        if getattr(self, '_building_cards', False):
            return
        self._rebuild_scene_grid(event.width)

    def _rebuild_scene_grid(self, container_width: Optional[int] = None):
        if not self.scene_cards:
            return
        if not container_width or container_width <= 0:
            container_width = self.scenes_container.winfo_width() or 1
        cols = max(1, container_width // CARD_MIN_WIDTH)
        if self._last_cols_count == cols:
            return
        self._last_cols_count = cols
        for child in self.scenes_container.winfo_children():
            child.grid_forget()
        for i, name in enumerate(self._scene_names):
            card = self.scene_cards.get(name)
            if card:
                r, c = divmod(i, cols)
                card.grid(row=r, column=c, padx=4, pady=4, sticky="nsew")
        for c in range(cols):
            self.scenes_container.columnconfigure(c, weight=1)

    def _on_scene_click(self, scene_name: str):
        idx = self._get_trecho_index()
        if idx is None:
            messagebox.showinfo("Selecione um trecho",
                                "Selecione um trecho à esquerda antes de escolher a cena.",
                                parent=self.top)
            return
        # desmarca todos, marca o clicado
        for n, v in self.scene_vars.items():
            v.set(1 if n == scene_name else 0)
        self.selected_scene_var.set(scene_name)
        self._assignments[idx]["assigned_scene"] = scene_name
        self.lbl_cena_atual.config(text=f"Cena: {scene_name}", foreground="#107C10")
        self._rebuild_trechos_list()
        self._select_next_without_scene()

    def _select_next_without_scene(self):
        """Seleciona automaticamente a próxima frase sem cena na listbox."""
        for row, real_idx in enumerate(self.filtered_indices):
            if not self._assignments[real_idx].get("assigned_scene"):
                self.list_trechos.selection_clear(0, "end")
                self.list_trechos.selection_set(row)
                self.list_trechos.see(row)
                self.list_trechos.event_generate("<<ListboxSelect>>")
                return

    # -------- thumbnails --------

    def _get_img_thumbnail(self, name: str, path: Path):
        if not PIL_AVAILABLE:
            return None
        if name in self.thumbnails:
            return self.thumbnails[name]
        try:
            img = Image.open(str(path))
            img.thumbnail((THUMB_W, THUMB_H))
            photo = ImageTk.PhotoImage(img)
            self.thumbnails[name] = photo
            return photo
        except Exception:
            return None

    def _get_vid_thumbnail(self, name: str, path: Path):
        if not (CV2_AVAILABLE and PIL_AVAILABLE):
            return None
        if name in self.video_thumbs:
            return self.video_thumbs[name]
        cap = None
        try:
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                return None
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2 if total > 0 else 0)
            ret, frame = cap.read()
            if not ret or frame is None:
                return None
            h, w, _ = frame.shape
            scale = min(THUMB_W / w, THUMB_H / h)
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            photo = ImageTk.PhotoImage(Image.fromarray(frame))
            self.video_thumbs[name] = photo
            return photo
        except Exception:
            return None
        finally:
            if cap:
                try:
                    cap.release()
                except Exception:
                    pass

    # -------- hover video --------

    def _stop_hover_play(self):
        self.hover_running = False
        if self.hover_cap is not None:
            try:
                self.hover_cap.release()
            except Exception:
                pass
            self.hover_cap = None
        if self.hover_scene_name and self.hover_scene_name in self.video_labels:
            label = self.video_labels[self.hover_scene_name]
            thumb = self.video_thumbs.get(self.hover_scene_name)
            if thumb:
                label.configure(image=thumb, bg="#3a3a3a", fg="white", text="")
                label.image = thumb
            else:
                label.configure(text="Passe o mouse\npara prévia", image="",
                                bg="#3a3a3a", fg="white")
                label.image = None
        self.hover_scene_name = None

    def _on_video_enter(self, scene_name: str, path: Path, label: tk.Label):
        if not (CV2_AVAILABLE and PIL_AVAILABLE):
            return
        self._stop_hover_play()
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return
        self.hover_scene_name = scene_name
        self.hover_cap = cap
        self.hover_running = True
        delay_ms = 50

        def update_frame():
            if not self.hover_running or self.hover_cap is None or self.hover_scene_name != scene_name:
                return
            frame = None
            for _ in range(3):
                ret, f = self.hover_cap.read()
                if not ret:
                    self.hover_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, f = self.hover_cap.read()
                    if not ret:
                        return
                frame = f
            if frame is None:
                return
            try:
                h, w, _ = frame.shape
                scale = min(THUMB_W / w, THUMB_H / h)
                frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                photo = ImageTk.PhotoImage(Image.fromarray(frame))
            except Exception:
                return
            label.configure(image=photo, bg="#82B7EB", fg="black", text="")
            label.image = photo
            label.after(delay_ms, update_frame)

        update_frame()

    def _on_video_leave(self, scene_name: str):
        if self.hover_scene_name == scene_name:
            self._stop_hover_play()

    # -------- scroll --------

    def _scroll_to_scene(self, scene_name: str):
        card = self.scene_cards.get(scene_name)
        if not card:
            return
        self.canvas_cenas.update_idletasks()
        self.scenes_container.update_idletasks()
        try:
            y = card.winfo_y()
            total = max(self.scenes_container.winfo_height(), 1)
            self.canvas_cenas.yview_moveto(max(0.0, min(1.0, y / total)))
        except Exception:
            pass

    def _bind_mousewheel(self):
        self.canvas_cenas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas_cenas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas_cenas.bind_all("<Button-5>", self._on_mousewheel)

    def _unbind_mousewheel(self):
        self.canvas_cenas.unbind_all("<MouseWheel>")
        self.canvas_cenas.unbind_all("<Button-4>")
        self.canvas_cenas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event):
        if event.delta:
            self.canvas_cenas.yview_scroll(-1 * int(event.delta / 120), "units")
        elif event.num == 4:
            self.canvas_cenas.yview_scroll(-2, "units")
        elif event.num == 5:
            self.canvas_cenas.yview_scroll(2, "units")

    # ------------------------------------------------------------------ #
    # APLICAR CÓPIAS
    # ------------------------------------------------------------------ #

    def _apply_copies(self):
        output_dir = self._output_var.get().strip()
        if not output_dir:
            messagebox.showerror("Erro", "Selecione a pasta de saída.", parent=self.top)
            return
        os.makedirs(output_dir, exist_ok=True)

        sem_cena = [a for a in self._assignments if not a.get("assigned_scene")]
        if sem_cena:
            resp = messagebox.askyesno(
                "Frases sem cena",
                f"{len(sem_cena)} frase(s) ainda sem cena atribuída.\n"
                "Deseja continuar mesmo assim?",
                parent=self.top)
            if not resp:
                return

        n_words = max(2, self._n_words_var.get())
        erros = []
        copiados = 0
        copied_files = []

        for item in self._assignments:
            scene_name = item.get("assigned_scene")
            if not scene_name:
                continue
            src_path = self._scene_paths.get(scene_name)
            if not src_path or not src_path.exists():
                erros.append(f"Arquivo não encontrado: {scene_name}")
                continue
            phrase = (item.get("script_fragment") or "").strip()
            # nome de destino usando n_words
            import unicodedata, re as _re
            words = [w.strip() for w in phrase.split() if w.strip()]
            short = " ".join(words[:n_words])
            safe = unicodedata.normalize("NFD", short)
            safe = "".join(c for c in safe if not unicodedata.combining(c))
            safe = safe.lower()
            safe = _re.sub(r"[^a-z0-9 ]", "", safe)
            safe = _re.sub(r"\s+", " ", safe).strip()[:80]
            dest_name = f"{safe}{src_path.suffix}"
            dest_path = Path(output_dir) / dest_name
            try:
                shutil.copy2(str(src_path), str(dest_path))
                copiados += 1
                copied_files.append(str(dest_path))
            except Exception as e:
                erros.append(f"{scene_name}: {e}")

        # Salvar para undo
        self._last_copied_files = copied_files

        msg = f"{copiados} arquivo(s) copiado(s) para:\n{output_dir}"
        if erros:
            msg += f"\n\nErros ({len(erros)}):\n" + "\n".join(erros[:5])
        messagebox.showinfo("Concluído", msg, parent=self.top)

    # ------------------------------------------------------------------ #
    # LOG / POLL
    # ------------------------------------------------------------------ #

    def _log(self, msg: str):
        import threading
        thread = threading.current_thread()
        prefix = f"[{thread.name}]"
        self._log_queue.put(f"{prefix} {msg}")

    def _poll_log_queue(self):
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self._log_text.configure(state="normal")
                self._log_text.insert("end", msg + "\n")
                self._log_text.see("end")
                self._log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.top.after(100, self._poll_log_queue)

    # ------------------------------------------------------------------ #
    # FECHAR
    # ------------------------------------------------------------------ #

    def _on_close(self):
        self._stop_event.set()
        self._stop_hover_play()
        self.top.destroy()
