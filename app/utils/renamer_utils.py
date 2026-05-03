# Utilitários puros extraídos de renomeador de cena1.1.py
import os
import re
import io
import json
import sys
import shutil
import hashlib
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont
from app.utils.ffmpeg_path import get_ffmpeg_bin, get_ffprobe_bin

# Oculta console preto do subprocess (ffmpeg/ffprobe) em Windows --windowed.
_NO_WINDOW_FLAGS = 0x08000000 if sys.platform == 'win32' else 0

# ---------------------------
# Extensões suportadas
# ---------------------------

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}

INVALID_FILENAME_CHARS = '<>:"/\\|?*\n\r\t'

# ---------------------------
# Constantes de placeholder
# ---------------------------

PLACEHOLDER_W = 1920
PLACEHOLDER_H = 1080
PLACEHOLDER_BG = (0, 255, 0)       # verde
PLACEHOLDER_TEXT = (255, 255, 255)  # branco
PLACEHOLDER_STROKE = (0, 0, 0)     # contorno preto

# ---------------------------
# Constantes Gemini / matching
# (importadas pelo SceneRenamerManager, centralizadas aqui)
# ---------------------------

GEMINI_IMAGE_MODEL_CHEAP = "gemini-2.5-flash-lite"
GEMINI_IMAGE_MODEL_MID   = "gemini-2.5-flash"
GEMINI_IMAGE_MODEL_PRO   = "gemini-2.5-pro"
GEMINI_VIDEO_MODEL       = "gemini-2.5-flash"
GEMINI_EMBEDDING_MODEL   = "gemini-embedding-001"

TOP_K_PER_SCENE                  = 25
MIN_SIMILARITY                   = 0.30
MIN_ASSIGN_SCORE                 = 0.82
REPROCESS_MIN_ASSIGN_SCORE_FACTOR = 0.95
SCENE_REUSE_SCORE_DECAY          = 0.92

DESC_MAX_SIDE                    = 1024
VIDEO_CENTER_CLIP_SECONDS        = 11.0
IMAGE_FALLBACK_CONFIDENCE_THRESHOLD = 0.60

DIFFICULTY_ENUM = [
    "texto_miudo",
    "muitos_elementos",
    "baixa_luz",
    "movimento_desfocado",
    "imagem_de_tela",
    "baixa_resolucao",
    "angulo_ruim",
]

DIFFICULTY_TRIGGERS_FOR_FALLBACK = {
    "texto_miudo",
    "muitos_elementos",
    "baixa_luz",
    "movimento_desfocado",
    "imagem_de_tela",
    "baixa_resolucao",
}

SCENE_DESC_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary":    {"type": "string"},
        "keywords":   {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "doubts":     {"type": "array", "items": {"type": "string"}},
        "difficulty": {"type": "array", "items": {"type": "string", "enum": DIFFICULTY_ENUM}},
    },
    "required": ["summary", "keywords", "confidence", "doubts", "difficulty"],
}

# ---------------------------
# Dataclasses
# ---------------------------

@dataclass
class SceneDesc:
    path: str
    desc_text: str
    confidence: float
    used_model: str
    doubts: List[str]
    difficulty: List[str]
    fallback_reason: str = ""
    content_id: str = ""
    from_cache: bool = False


@dataclass
class FallbackPolicy:
    allow_pro: bool = True
    pro_disabled: bool = False
    pro_disabled_reason: str = ""
    pro_fail_count: int = 0


@dataclass
class Assignment:
    phrase_idx: int
    scene_idx: int
    score: float


class ProcessingCancelled(Exception):
    """Usada para interromper o worker quando o usuário cancela o processamento."""
    pass

# ---------------------------
# Utilitários de arquivo
# ---------------------------

def is_video(path: str) -> bool:
    return os.path.splitext(path.lower())[1] in VIDEO_EXTS


def is_image(path: str) -> bool:
    return os.path.splitext(path.lower())[1] in IMAGE_EXTS


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(f"[{re.escape(INVALID_FILENAME_CHARS)}]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        name = "cena_sem_nome"

    if len(name) > max_len:
        name = name[:max_len].rstrip()

    name = name.rstrip(". ").strip()
    if not name:
        name = "cena_sem_nome"

    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
        f"LPT{i}" for i in range(1, 10)}
    if name.upper() in reserved:
        name = "_" + name

    return name


def unique_path(directory: str, base_name: str, ext: str) -> str:
    candidate = os.path.join(directory, base_name + ext)
    if not os.path.exists(candidate):
        return candidate

    i = 2
    while True:
        candidate = os.path.join(directory, f"{base_name}__{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def first_n_words(text: str, n: int) -> str:
    words = [w.strip() for w in text.split() if w.strip()]
    words = [w.strip('"""\'') for w in words]
    return " ".join(words[:n]).strip()


def first_n_words_filename(text: str, n: int) -> str:
    return sanitize_filename(first_n_words(text, n))

# ---------------------------
# Utilitários de texto / roteiro
# ---------------------------

def split_sentences_by_period_only(script: str) -> List[str]:
    s = script.replace("\r", "\n")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return []

    parts = s.split(".")
    sentences: List[str] = []

    ends_with_period = s.endswith(".")

    for idx, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue

        if idx < len(parts) - 1 or ends_with_period:
            sentences.append(part + ".")
        else:
            sentences.append(part)

    return sentences


def group_sentences(sentences: List[str], sentences_per_chunk: int) -> List[str]:
    if sentences_per_chunk < 1:
        sentences_per_chunk = 1

    chunks: List[str] = []
    for i in range(0, len(sentences), sentences_per_chunk):
        group = " ".join(sentences[i:i + sentences_per_chunk]).strip()
        if group:
            chunks.append(group)
    return chunks


def count_words(text: str) -> int:
    return len([w for w in (text or "").split() if w.strip()])


def expand_script_items_by_word_limit(
    script_items: List[str],
    word_limit: int,
) -> List[str]:
    """
    Divide itens longos em sub-itens para permitir MAIS de uma cena.
    """
    if word_limit < 2:
        return script_items

    min_tail = (word_limit + 1) // 2
    out: List[str] = []

    for text in script_items:
        words = [w for w in (text or "").split() if w.strip()]
        n = len(words)

        if n <= word_limit:
            out.append(text)
            continue

        k = n // word_limit
        r = n % word_limit

        if k <= 0:
            out.append(text)
            continue

        idx = 0
        for _ in range(max(0, k - 1)):
            part = " ".join(words[idx: idx + word_limit]).strip()
            if part:
                out.append(part)
            idx += word_limit

        remaining = words[idx:]

        if r >= min_tail:
            part1 = " ".join(remaining[:word_limit]).strip()
            part2 = " ".join(remaining[word_limit:]).strip()
            if part1:
                out.append(part1)
            if part2:
                out.append(part2)
        else:
            part = " ".join(remaining).strip()
            if part:
                out.append(part)

    return out


def build_script_items(
    roteiro_text: str,
    sentences_per_chunk: int = 1,
    split_long_phrases: bool = False,
    split_words_threshold: int = 10,
) -> List[str]:
    sentences = split_sentences_by_period_only(roteiro_text)
    items = group_sentences(sentences, sentences_per_chunk)

    if split_long_phrases:
        items = expand_script_items_by_word_limit(
            items,
            word_limit=split_words_threshold,
        )

    return [x.strip() for x in items if x and x.strip()]

# ---------------------------
# Placeholder (imagem verde) para frases sem cena
# ---------------------------

def _try_load_font(size: int):
    try:
        return ImageFont.truetype("arial.ttf", size=size)
    except Exception:
        try:
            return ImageFont.truetype("DejaVuSans.ttf", size=size)
        except Exception:
            return ImageFont.load_default()


def _wrap_text_lines(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> List[str]:
    words = text.split()
    lines: List[str] = []
    current = ""

    for w in words:
        test = (current + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = w

    if current:
        lines.append(current)

    return lines


def create_green_placeholder_image(text: str, out_path: str):
    img = Image.new("RGB", (PLACEHOLDER_W, PLACEHOLDER_H), PLACEHOLDER_BG)
    draw = ImageDraw.Draw(img)

    margin = 120
    max_w = PLACEHOLDER_W - (margin * 2)
    max_h = PLACEHOLDER_H - (margin * 2)

    header = "SEM CENA"
    body = text.strip()

    font_size = 72
    header_size = 84

    while font_size >= 18:
        header_font = _try_load_font(header_size)
        body_font = _try_load_font(font_size)

        header_bbox = draw.textbbox((0, 0), header, font=header_font)
        header_h = header_bbox[3] - header_bbox[1]

        body_lines = _wrap_text_lines(draw, body, body_font, max_w)
        line_heights = []
        for ln in body_lines:
            b = draw.textbbox((0, 0), ln, font=body_font)
            line_heights.append(b[3] - b[1])

        body_h = sum(line_heights) + (len(body_lines) - 1) * 12
        total_h = header_h + 30 + body_h

        if total_h <= max_h:
            break

        font_size -= 4
        header_size = max(24, header_size - 2)

    header_font = _try_load_font(header_size)
    body_font = _try_load_font(font_size)

    header_bbox = draw.textbbox((0, 0), header, font=header_font)
    header_w = header_bbox[2] - header_bbox[0]
    header_h = header_bbox[3] - header_bbox[1]

    body_lines = _wrap_text_lines(draw, body, body_font, max_w)
    line_boxes = [draw.textbbox((0, 0), ln, font=body_font) for ln in body_lines]
    line_ws = [(b[2] - b[0]) for b in line_boxes]
    line_hs = [(b[3] - b[1]) for b in line_boxes]
    body_h = sum(line_hs) + (len(body_lines) - 1) * 12

    total_h = header_h + 30 + body_h
    start_y = (PLACEHOLDER_H - total_h) // 2

    header_x = (PLACEHOLDER_W - header_w) // 2
    draw.text(
        (header_x, start_y),
        header,
        font=header_font,
        fill=PLACEHOLDER_TEXT,
        stroke_width=3,
        stroke_fill=PLACEHOLDER_STROKE,
    )

    y = start_y + header_h + 30
    for ln, w, h in zip(body_lines, line_ws, line_hs):
        x = (PLACEHOLDER_W - w) // 2
        draw.text(
            (x, y),
            ln,
            font=body_font,
            fill=PLACEHOLDER_TEXT,
            stroke_width=2,
            stroke_fill=PLACEHOLDER_STROKE,
        )
        y += h + 12

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, format="PNG")

# ---------------------------
# Utilitários Gemini (bytes/imagem)
# ---------------------------

def _pil_to_jpeg_bytes(img: Image.Image, max_side: int = 1024, quality: int = 85) -> bytes:
    img = img.convert("RGB")
    w, h = img.size
    scale = min(max_side / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _prepare_image_bytes_for_gemini(path: str, max_side: int = 1024) -> Tuple[bytes, str]:
    img = Image.open(path)
    jpeg_bytes = _pil_to_jpeg_bytes(img, max_side=max_side, quality=85)
    return jpeg_bytes, "image/jpeg"

# ---------------------------
# Utilitários JSON / hash
# ---------------------------

def _safe_json_loads(s: str) -> dict:
    s = (s or "").strip()
    if not s:
        raise ValueError("Resposta vazia.")
    try:
        return json.loads(s)
    except Exception:
        i = s.find("{")
        j = s.rfind("}")
        if i != -1 and j != -1 and j > i:
            return json.loads(s[i:j+1])
        raise


def _compute_content_id(path: str, chunk_size: int = 1024 * 1024) -> str:
    """
    ID do conteúdo do arquivo (rápido e bem confiável):
    usa tamanho + 1MB do começo + 1MB do fim.
    """
    st = os.stat(path)
    size = st.st_size

    h = hashlib.sha256()
    h.update(str(size).encode("utf-8"))
    h.update(b"|")

    with open(path, "rb") as f:
        first = f.read(min(chunk_size, size))
        h.update(first)

        if size > chunk_size:
            read_tail = min(chunk_size, size)
            f.seek(max(0, size - read_tail))
            tail = f.read(read_tail)
            h.update(tail)

    return h.hexdigest()

# ---------------------------
# Normalização para matching estável
# ---------------------------

def _normalize_text_for_stable_match(text: str) -> str:
    text = (text or "").replace("\r", "\n").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _phrase_key(text: str) -> str:
    return _normalize_text_for_stable_match(text)

# ---------------------------
# Utilitários ffprobe / ffmpeg
# ---------------------------

def _ffprobe_duration_seconds(path: str) -> Optional[float]:
    """Retorna a duração do vídeo em segundos usando ffprobe."""
    ffprobe = get_ffprobe_bin()
    if not os.path.isabs(ffprobe) and shutil.which(ffprobe) is None:
        return None

    try:
        cmd = [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=_NO_WINDOW_FLAGS)
        if r.returncode != 0:
            return None
        s = (r.stdout or "").strip()
        return float(s) if s else None
    except Exception:
        return None


def _compute_center_clip_window(duration_s: float, clip_len_s: float) -> Tuple[float, float]:
    """Retorna (start, length). Se o vídeo for <= clip_len_s, retorna (0, duration_s)."""
    if duration_s <= clip_len_s:
        return 0.0, float(duration_s)
    start = (duration_s - clip_len_s) / 2.0
    return max(0.0, float(start)), float(clip_len_s)


def _format_ffmpeg_error(
    cmd: List[str],
    result: Optional[subprocess.CompletedProcess] = None,
    exc: Optional[Exception] = None,
) -> str:
    cmd_str = " ".join(f'"{p}"' if " " in str(p) else str(p) for p in cmd)

    if exc is not None:
        return f"Falha ao executar ffmpeg: {exc} | comando: {cmd_str}"

    details: List[str] = []
    if result is not None:
        if result.returncode not in (None, 0):
            details.append(f"returncode={result.returncode}")

        stderr_text = (result.stderr or "").strip()
        stdout_text = (result.stdout or "").strip()
        raw_text = stderr_text or stdout_text

        if raw_text:
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            if lines:
                tail = " | ".join(lines[-3:])
                details.append(f"detalhe={tail}")

    if not details:
        details.append("ffmpeg falhou sem detalhe no stdout/stderr")

    return f"{' ; '.join(details)} | comando: {cmd_str}"


def _ffmpeg_extract_clip_to_temp(
    in_path: str,
    start_s: float,
    duration_s: float,
    include_audio: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Recorta um trecho do vídeo com ffmpeg e salva num arquivo temporário (.mp4).
    Retorna (caminho_temp, None) em caso de sucesso ou (None, mensagem_erro) em falha.
    """
    ffmpeg = get_ffmpeg_bin()
    if not os.path.isabs(ffmpeg) and shutil.which(ffmpeg) is None:
        return None, "ffmpeg não foi encontrado no sistema."

    cmd = []
    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        tmp_path = tmp.name
        tmp.close()

        cmd = [
            ffmpeg, "-y",
            "-ss", f"{start_s:.3f}",
            "-i", in_path,
            "-t", f"{duration_s:.3f}",
            "-map", "0:v:0?",
        ]

        if include_audio:
            cmd += ["-map", "0:a:0?", "-c:a", "aac"]
        else:
            cmd += ["-an"]

        cmd += [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-movflags", "+faststart",
            tmp_path,
        ]

        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           creationflags=_NO_WINDOW_FLAGS)
        if r.returncode != 0:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return None, _format_ffmpeg_error(cmd, result=r)

        if (not os.path.exists(tmp_path)) or os.path.getsize(tmp_path) == 0:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return None, "ffmpeg terminou sem gerar arquivo válido."

        return tmp_path, None
    except Exception as exc:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return None, _format_ffmpeg_error(cmd, exc=exc)


def _ffmpeg_remove_audio_to_temp(in_path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Remove o áudio do vídeo e salva num arquivo temporário (.mp4).
    Retorna (caminho_temp, None) ou (None, mensagem_erro).
    """
    ffmpeg = get_ffmpeg_bin()
    if not os.path.isabs(ffmpeg) and shutil.which(ffmpeg) is None:
        return None, "ffmpeg não foi encontrado no sistema."

    cmd = []
    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        tmp_path = tmp.name
        tmp.close()

        cmd = [
            ffmpeg, "-y",
            "-i", in_path,
            "-map", "0:v:0?",
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-movflags", "+faststart",
            tmp_path,
        ]

        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           creationflags=_NO_WINDOW_FLAGS)
        if r.returncode != 0:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return None, _format_ffmpeg_error(cmd, result=r)

        if (not os.path.exists(tmp_path)) or os.path.getsize(tmp_path) == 0:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return None, "ffmpeg terminou sem gerar arquivo válido."

        return tmp_path, None
    except Exception as exc:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return None, _format_ffmpeg_error(cmd, exc=exc)
