"""
Editor de Estilos para Frases Impactantes.
Permite configurar fonte, cor, borda, posicao, tamanho, animacao e preview em tempo real.
"""
import json
import os
import tkinter as tk
from tkinter import colorchooser, messagebox
from typing import Callable, Optional, Dict, Any

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


STYLES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "assets", "text_styles.json"
)

DEFAULT_STYLE = {
    "font_color": "#FFFFFF",
    "border_color": "#000000",
    "border_width": 4,
    "shadow_x": 2,
    "shadow_y": 2,
    "shadow_color": "#000000",
    "shadow_opacity": 0.55,
    "box_enabled": True,
    "box_color": "#000000",
    "box_opacity": 0.35,
    "caps_lock": False,
    "animation": "none",
    "anim_in_pct": 10,
    "anim_out_pct": 10,
    "font_size": 80,
    "position": "bottom",
}

ANIMATIONS = ["none", "fade", "pop"]
POSITIONS = [("Baixo", "bottom"), ("Centro", "center"), ("Topo", "top")]


def load_styles() -> dict:
    try:
        if os.path.exists(STYLES_PATH):
            with open(STYLES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"styles": {"Padrão": dict(DEFAULT_STYLE)}, "selected": "Padrão"}


def save_styles(data: dict):
    os.makedirs(os.path.dirname(STYLES_PATH), exist_ok=True)
    with open(STYLES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_selected_style() -> Dict[str, Any]:
    data = load_styles()
    name = data.get("selected", "Padrão")
    styles = data.get("styles", {})
    st = styles.get(name, styles.get("Padrão", DEFAULT_STYLE))
    merged = dict(DEFAULT_STYLE)
    merged.update(st)
    return merged


def get_selected_style_name() -> str:
    data = load_styles()
    return data.get("selected", "Padrão")


def render_preview_image(st: Dict[str, Any], W: int, H: int,
                         font_file: str = "", alpha: float = 1.0) -> 'Image.Image':
    """Renderiza preview do estilo em PIL Image. alpha=0..1 para simular fade."""
    merged = dict(DEFAULT_STYLE)
    merged.update(st)

    sample = "Texto de Exemplo"
    if merged["caps_lock"]:
        sample = sample.upper()

    font_sz = merged.get("font_size", 80)
    position = merged.get("position", "bottom")

    img = Image.new("RGBA", (W, H), (26, 26, 26, 255))
    draw = ImageDraw.Draw(img)

    # Escalar fonte para preview (proporcional a 1080p)
    scale = H / 1080.0
    preview_font_sz = max(12, int(font_sz * scale))

    pil_font = None
    try:
        if font_file and os.path.exists(font_file):
            pil_font = ImageFont.truetype(font_file, preview_font_sz)
    except Exception:
        pass
    if pil_font is None:
        try:
            pil_font = ImageFont.truetype("arial.ttf", preview_font_sz)
        except Exception:
            pil_font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), sample, font=pil_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (W - tw) // 2

    # Posicao vertical
    if position == "top":
        y = int(H * 0.18) - th // 2
    elif position == "center":
        y = (H - th) // 2
    else:
        y = int(H * 0.78) - th // 2
    y = max(0, min(H - th - 5, y))

    text_alpha = int(alpha * 255)

    # Box background
    if merged["box_enabled"]:
        box_a = int(merged["box_opacity"] * 255 * alpha)
        pad = int(18 * scale)
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        bc = _hex_to_rgb(merged.get("box_color", "#000000"))
        od.rectangle([x - pad, y - pad, x + tw + pad, y + th + pad],
                     fill=(bc[0], bc[1], bc[2], box_a))
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)

    # Sombra
    sx, sy = merged["shadow_x"], merged["shadow_y"]
    if (sx or sy) and text_alpha > 0:
        sc = _hex_to_rgb(merged.get("shadow_color", "#000000"))
        sa = int(merged.get("shadow_opacity", 0.55) * 255 * alpha)
        s_sx, s_sy = max(1, int(sx * scale)), max(1, int(sy * scale))
        draw.text((x + s_sx, y + s_sy), sample, font=pil_font,
                  fill=(sc[0], sc[1], sc[2], sa))

    # Borda + texto
    bw = max(1, int(merged["border_width"] * scale)) if merged["border_width"] > 0 else 0
    bc = _hex_to_rgb(merged["border_color"])
    fc = _hex_to_rgb(merged["font_color"])

    if bw > 0 and text_alpha > 0:
        draw.text((x, y), sample, font=pil_font,
                  fill=(fc[0], fc[1], fc[2], text_alpha),
                  stroke_width=bw,
                  stroke_fill=(bc[0], bc[1], bc[2], text_alpha))
    elif text_alpha > 0:
        draw.text((x, y), sample, font=pil_font,
                  fill=(fc[0], fc[1], fc[2], text_alpha))

    # Info
    info_font = ImageFont.load_default()
    pos_label = {"top": "Topo", "center": "Centro", "bottom": "Baixo"}.get(position, position)
    draw.text((5, 5), f"{font_sz}px | {pos_label}", fill=(100, 100, 100), font=info_font)

    return img


def _hex_to_rgb(hex_color: str):
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


class StyleEditorDialog:
    """Toplevel dialog para editar estilos de texto."""

    def __init__(self, parent: tk.Tk, on_style_changed: Optional[Callable] = None,
                 font_file: str = ""):
        self.on_style_changed = on_style_changed
        self.font_file = font_file
        self._anim_job = None  # after id para animacao

        self.top = tk.Toplevel(parent)
        self.top.title("Editor de Estilos - Frases Impactantes")
        self.top.geometry("760x700")
        self.top.resizable(False, False)
        self.top.grab_set()
        self.top.protocol("WM_DELETE_WINDOW", self._close)

        self.data = load_styles()
        self.styles = self.data.get("styles", {})
        self.current_name = self.data.get("selected", "Padrão")

        # --- Topo: seletor de estilo ---
        top_frame = tk.Frame(self.top)
        tk.Label(top_frame, text="Estilo:").pack(side="left", padx=(8, 4))
        self.style_var = tk.StringVar(value=self.current_name)
        style_names = list(self.styles.keys()) or ["Padrão"]
        self.style_menu = tk.OptionMenu(top_frame, self.style_var, *style_names,
                                         command=self._on_style_selected)
        self.style_menu.config(width=18)
        self.style_menu.pack(side="left", padx=4)
        tk.Button(top_frame, text="Novo", command=self._new_style).pack(side="left", padx=2)
        tk.Button(top_frame, text="Excluir", command=self._delete_style).pack(side="left", padx=2)
        top_frame.pack(fill="x", pady=(8, 4))

        # --- Editor ---
        editor = tk.Frame(self.top)

        # Cor da fonte + Cor da borda
        row1 = tk.Frame(editor)
        tk.Label(row1, text="Cor Fonte:", width=10, anchor="w").pack(side="left")
        self.font_color_var = tk.StringVar(value="#FFFFFF")
        self.font_color_btn = tk.Button(row1, text="     ", bg="#FFFFFF",
                                         command=lambda: self._pick_color(self.font_color_var, self.font_color_btn))
        self.font_color_btn.pack(side="left", padx=4)
        tk.Label(row1, textvariable=self.font_color_var, width=8).pack(side="left")

        tk.Label(row1, text="Cor Borda:", width=10, anchor="w").pack(side="left", padx=(12, 0))
        self.border_color_var = tk.StringVar(value="#000000")
        self.border_color_btn = tk.Button(row1, text="     ", bg="#000000",
                                           command=lambda: self._pick_color(self.border_color_var, self.border_color_btn))
        self.border_color_btn.pack(side="left", padx=4)
        tk.Label(row1, textvariable=self.border_color_var, width=8).pack(side="left")

        tk.Label(row1, text="Larg:").pack(side="left", padx=(8, 2))
        self.border_width_var = tk.IntVar(value=4)
        tk.Scale(row1, from_=0, to=15, orient="horizontal", variable=self.border_width_var,
                 command=lambda _: self._update_preview(), length=80).pack(side="left")
        row1.pack(fill="x", padx=12, pady=2)

        # Tamanho da fonte + Posicao
        row2 = tk.Frame(editor)
        tk.Label(row2, text="Tamanho:", width=10, anchor="w").pack(side="left")
        self.font_size_var = tk.IntVar(value=80)
        tk.Scale(row2, from_=20, to=200, orient="horizontal", variable=self.font_size_var,
                 command=lambda _: self._update_preview(), length=120).pack(side="left")
        tk.Label(row2, text="px").pack(side="left", padx=(2, 12))

        tk.Label(row2, text="Posicao:").pack(side="left", padx=(8, 4))
        self.position_var = tk.StringVar(value="bottom")
        for label, val in POSITIONS:
            tk.Radiobutton(row2, text=label, variable=self.position_var, value=val,
                           command=self._update_preview).pack(side="left", padx=3)
        row2.pack(fill="x", padx=12, pady=2)

        # Sombra + Box
        row3 = tk.Frame(editor)
        tk.Label(row3, text="Sombra:", width=10, anchor="w").pack(side="left")
        tk.Label(row3, text="X:").pack(side="left")
        self.shadow_x_var = tk.IntVar(value=2)
        tk.Spinbox(row3, from_=0, to=10, width=3, textvariable=self.shadow_x_var,
                   command=self._update_preview).pack(side="left", padx=(0, 6))
        tk.Label(row3, text="Y:").pack(side="left")
        self.shadow_y_var = tk.IntVar(value=2)
        tk.Spinbox(row3, from_=0, to=10, width=3, textvariable=self.shadow_y_var,
                   command=self._update_preview).pack(side="left", padx=(0, 12))

        self.box_enabled_var = tk.IntVar(value=1)
        tk.Checkbutton(row3, text="Fundo", variable=self.box_enabled_var,
                       command=self._update_preview).pack(side="left")
        tk.Label(row3, text="Opac:").pack(side="left", padx=(8, 2))
        self.box_opacity_var = tk.DoubleVar(value=0.35)
        tk.Scale(row3, from_=0.0, to=1.0, resolution=0.05, orient="horizontal",
                 variable=self.box_opacity_var, command=lambda _: self._update_preview(),
                 length=80).pack(side="left")
        row3.pack(fill="x", padx=12, pady=2)

        # Caps Lock + Animacao
        row4 = tk.Frame(editor)
        self.caps_var = tk.IntVar(value=0)
        tk.Checkbutton(row4, text="CAPS LOCK", variable=self.caps_var,
                       command=self._update_preview).pack(side="left", padx=(0, 16))

        tk.Label(row4, text="Animacao:").pack(side="left", padx=(0, 4))
        self.anim_var = tk.StringVar(value="none")
        for label, val in [("Nenhuma", "none"), ("Fade", "fade"), ("Pop", "pop")]:
            tk.Radiobutton(row4, text=label, variable=self.anim_var, value=val,
                           command=self._on_anim_changed).pack(side="left", padx=3)
        row4.pack(fill="x", padx=12, pady=2)

        # Animacao duração (% da cena)
        row5 = tk.Frame(editor)
        tk.Label(row5, text="Anim Entrada:", width=12, anchor="w").pack(side="left")
        self.anim_in_var = tk.IntVar(value=10)
        tk.Scale(row5, from_=5, to=40, orient="horizontal", variable=self.anim_in_var,
                 command=lambda _: self._on_anim_changed(), length=80).pack(side="left")
        tk.Label(row5, text="%").pack(side="left", padx=(0, 16))
        tk.Label(row5, text="Saida:").pack(side="left")
        self.anim_out_var = tk.IntVar(value=10)
        tk.Scale(row5, from_=5, to=40, orient="horizontal", variable=self.anim_out_var,
                 command=lambda _: self._on_anim_changed(), length=80).pack(side="left")
        tk.Label(row5, text="%").pack(side="left")
        row5.pack(fill="x", padx=12, pady=2)

        editor.pack(fill="x")

        # --- Preview (simula 16:9) ---
        preview_frame = tk.LabelFrame(self.top, text="Preview", padx=4, pady=4)
        self._pw, self._ph = 720, 230
        self.preview_canvas = tk.Canvas(preview_frame, width=self._pw, height=self._ph, bg="#1a1a1a")
        self.preview_canvas.pack()
        preview_frame.pack(fill="x", padx=12, pady=(6, 4))
        self._preview_photo = None

        # --- Botoes ---
        btn_frame = tk.Frame(self.top)
        tk.Button(btn_frame, text="Salvar Estilo", font=("Arial", 10, "bold"),
                  bg="#2d7d46", fg="white", padx=16, pady=4,
                  command=self._save_style).pack(side="left", padx=4)
        tk.Button(btn_frame, text="Preview 1080p", font=("Arial", 10),
                  padx=12, pady=4, command=self._preview_1080p).pack(side="left", padx=4)
        tk.Button(btn_frame, text="Fechar", padx=12, pady=4,
                  command=self._close).pack(side="right", padx=4)
        btn_frame.pack(fill="x", padx=12, pady=(4, 12))

        self._load_style_into_ui(self.current_name)

    # ── helpers ──

    def _pick_color(self, var: tk.StringVar, btn: tk.Button):
        color = colorchooser.askcolor(initialcolor=var.get(), parent=self.top)
        if color and color[1]:
            var.set(color[1])
            btn.config(bg=color[1])
            self._update_preview()

    def _on_style_selected(self, name: str):
        self.current_name = name
        self._load_style_into_ui(name)

    def _load_style_into_ui(self, name: str):
        st = dict(DEFAULT_STYLE)
        st.update(self.styles.get(name, {}))

        self.font_color_var.set(st["font_color"])
        self.font_color_btn.config(bg=st["font_color"])
        self.border_color_var.set(st["border_color"])
        self.border_color_btn.config(bg=st["border_color"])
        self.border_width_var.set(st["border_width"])
        self.font_size_var.set(st.get("font_size", 80))
        self.position_var.set(st.get("position", "bottom"))
        self.shadow_x_var.set(st["shadow_x"])
        self.shadow_y_var.set(st["shadow_y"])
        self.box_enabled_var.set(1 if st["box_enabled"] else 0)
        self.box_opacity_var.set(st["box_opacity"])
        self.caps_var.set(1 if st["caps_lock"] else 0)
        self.anim_var.set(st["animation"])
        self.anim_in_var.set(st.get("anim_in_pct", 10))
        self.anim_out_var.set(st.get("anim_out_pct", 10))

        self._on_anim_changed()

    def _get_style_from_ui(self) -> Dict[str, Any]:
        return {
            "font_color": self.font_color_var.get(),
            "border_color": self.border_color_var.get(),
            "border_width": self.border_width_var.get(),
            "shadow_x": self.shadow_x_var.get(),
            "shadow_y": self.shadow_y_var.get(),
            "shadow_color": "#000000",
            "shadow_opacity": 0.55,
            "box_enabled": bool(self.box_enabled_var.get()),
            "box_color": "#000000",
            "box_opacity": self.box_opacity_var.get(),
            "caps_lock": bool(self.caps_var.get()),
            "animation": self.anim_var.get(),
            "anim_in_pct": self.anim_in_var.get(),
            "anim_out_pct": self.anim_out_var.get(),
            "font_size": self.font_size_var.get(),
            "position": self.position_var.get(),
        }

    # ── preview ──

    def _update_preview(self, alpha: float = 1.0):
        if not HAS_PIL:
            self.preview_canvas.delete("all")
            self.preview_canvas.create_text(self._pw // 2, self._ph // 2,
                                             text="PIL nao instalado", fill="gray")
            return

        st = self._get_style_from_ui()
        img = render_preview_image(st, self._pw, self._ph, self.font_file, alpha=alpha)

        self._preview_photo = ImageTk.PhotoImage(img)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(0, 0, anchor="nw", image=self._preview_photo)

    # ── animacao em loop ──

    def _on_anim_changed(self):
        self._stop_anim()
        anim = self.anim_var.get()
        if anim in ("fade", "pop"):
            self._anim_frame = 0
            self._anim_loop()
        else:
            self._update_preview(alpha=1.0)

    def _anim_loop(self):
        anim = self.anim_var.get()
        if anim not in ("fade", "pop"):
            self._update_preview(alpha=1.0)
            return

        total = 60  # frames no ciclo
        in_pct = max(5, self.anim_in_var.get()) / 100.0
        out_pct = max(5, self.anim_out_var.get()) / 100.0
        in_frames = max(2, int(total * in_pct))
        out_frames = max(2, int(total * out_pct))

        f = self._anim_frame % total

        if f < in_frames:
            alpha = f / in_frames
        elif f >= (total - out_frames):
            alpha = (total - f) / out_frames
        else:
            alpha = 1.0

        if anim == "pop":
            # Pop: entrada mais rapida (metade dos frames de in)
            pop_in = max(1, in_frames // 2)
            if f < pop_in:
                alpha = f / pop_in
            elif f >= (total - out_frames):
                alpha = (total - f) / out_frames
            else:
                alpha = 1.0

        self._update_preview(alpha=max(0.0, min(1.0, alpha)))
        self._anim_frame += 1
        self._anim_job = self.top.after(50, self._anim_loop)

    def _stop_anim(self):
        if self._anim_job is not None:
            self.top.after_cancel(self._anim_job)
            self._anim_job = None

    # ── save/new/delete ──

    def _save_style(self):
        name = self.current_name
        self.styles[name] = self._get_style_from_ui()
        self.data["styles"] = self.styles
        self.data["selected"] = name
        save_styles(self.data)
        if self.on_style_changed:
            self.on_style_changed()
        messagebox.showinfo("Salvo", f'Estilo "{name}" salvo.', parent=self.top)

    def _new_style(self):
        dlg = tk.Toplevel(self.top)
        dlg.title("Novo Estilo")
        dlg.geometry("300x100")
        dlg.grab_set()
        tk.Label(dlg, text="Nome do estilo:").pack(pady=(12, 4))
        entry = tk.Entry(dlg, width=25)
        entry.pack()
        entry.focus_set()

        def _create():
            name = entry.get().strip()
            if not name:
                return
            if name in self.styles:
                messagebox.showwarning("Aviso", f'"{name}" já existe.', parent=dlg)
                return
            self.styles[name] = self._get_style_from_ui()
            self.current_name = name
            self.style_var.set(name)
            self._rebuild_menu()
            dlg.destroy()

        tk.Button(dlg, text="Criar", command=_create).pack(pady=8)

    def _delete_style(self):
        name = self.current_name
        if name == "Padrão":
            messagebox.showwarning("Aviso", '"Padrão" não pode ser excluído.', parent=self.top)
            return
        if messagebox.askyesno("Confirmar", f'Excluir "{name}"?', parent=self.top):
            del self.styles[name]
            self.data["styles"] = self.styles
            self.data["selected"] = "Padrão"
            self.current_name = "Padrão"
            self.style_var.set("Padrão")
            save_styles(self.data)
            self._rebuild_menu()
            self._load_style_into_ui("Padrão")
            if self.on_style_changed:
                self.on_style_changed()

    def _rebuild_menu(self):
        menu = self.style_menu["menu"]
        menu.delete(0, "end")
        for n in self.styles:
            menu.add_command(label=n, command=lambda v=n: (self.style_var.set(v), self._on_style_selected(v)))

    # ── preview 1080p ──

    def _preview_1080p(self):
        if not HAS_PIL:
            messagebox.showwarning("PIL", "Pillow nao instalado.", parent=self.top)
            return

        st = self._get_style_from_ui()
        W, H = 1920, 1080
        img = render_preview_image(st, W, H, self.font_file, alpha=1.0)

        preview_win = tk.Toplevel(self.top)
        preview_win.title(f"Preview 1080p - {self.current_name}")

        scale = 0.65
        disp_w, disp_h = int(W * scale), int(H * scale)
        disp_img = img.resize((disp_w, disp_h), Image.LANCZOS)
        photo = ImageTk.PhotoImage(disp_img)

        canvas = tk.Canvas(preview_win, width=disp_w, height=disp_h)
        canvas.pack()
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas._photo = photo

    # ── fechar ──

    def _close(self):
        self._stop_anim()
        self.data["selected"] = self.current_name
        save_styles(self.data)
        if self.on_style_changed:
            self.on_style_changed()
        self.top.destroy()
