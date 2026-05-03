# Extraído e modularizado de renomeador de cena1.1.py
import os
import re
import io
import sys
import json
import time
import random
import shutil
import threading
import hashlib
import subprocess
import tempfile
import mimetypes
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dataclasses import dataclass
from typing import List, Optional, Tuple, Set

import numpy as np
from PIL import Image
from app.utils.ffmpeg_path import get_ffmpeg_bin, get_ffprobe_bin

# Oculta o console preto do subprocess (ffmpeg/ffprobe) no Windows quando
# rodando como .exe --windowed. Sem isso, cada chamada abre um cmd visivel.
_NO_WINDOW_FLAGS = 0x08000000 if sys.platform == 'win32' else 0

from google import genai
from google.genai import types


# ---------------------------
# Config geral (extensões)
# ---------------------------

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


def get_app_base_dir() -> str:
    """
    Retorna a pasta "base" do app:
    - Rodando como .exe (PyInstaller): pasta do executável
    - Rodando como .py: pasta do arquivo .py
    """
    if getattr(sys, "frozen", False):
        # .exe empacotado
        return os.path.dirname(os.path.abspath(sys.executable))
    # script normal
    return os.path.dirname(os.path.abspath(__file__))


APP_BASE_DIR = get_app_base_dir()

# ✅ NOVO: tudo vai para uma pasta "cache" ao lado do script/exe
CACHE_DIR = os.path.join(APP_BASE_DIR, "cache")

CONFIG_PATH = os.path.join(CACHE_DIR, "scene_renamer_config.json")
SCENE_CACHE_PATH = os.path.join(CACHE_DIR, "scene_renamer_scene_cache.json")
SCENE_CACHE_VERSION = 1

STABLE_MATCH_CACHE_PATH = os.path.join(
    CACHE_DIR, "scene_renamer_stable_matches.json"
)
STABLE_MATCH_CACHE_VERSION = 1

LAST_RUN_STATE_PATH = os.path.join(
    CACHE_DIR, "scene_renamer_last_run.json"
)
LAST_RUN_STATE_VERSION = 1


UNDO_LAST_RUN_PATH = os.path.join(
    CACHE_DIR, "scene_renamer_undo_last_run.json"
)
UNDO_LAST_RUN_VERSION = 1


# ---------------------------
# Modelos Gemini
# ---------------------------
GEMINI_IMAGE_MODEL_CHEAP = "gemini-2.5-flash-lite"   # padrão (barato)
# fallback intermediário (se PRO indisponível)
GEMINI_IMAGE_MODEL_MID = "gemini-2.5-flash"
# fallback premium (quando disponível)
GEMINI_IMAGE_MODEL_PRO = "gemini-2.5-pro"
GEMINI_VIDEO_MODEL = "gemini-2.5-flash"              # Standard p/ vídeo
GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"

# Embeddings / matching
TOP_K_PER_SCENE = 25
MIN_SIMILARITY = 0.30
MIN_ASSIGN_SCORE = 0.82

# fator multiplicativo para o score mínimo ao reprocessar pendências
REPROCESS_MIN_ASSIGN_SCORE_FACTOR = 0.95

SCENE_REUSE_SCORE_DECAY = 0.92

# Imagem enviada para descrição (economiza custo/tempo)
DESC_MAX_SIDE = 1024

VIDEO_CENTER_CLIP_SECONDS = 11.0

# Fallback (imagem): critérios
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
        "summary": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "doubts": {"type": "array", "items": {"type": "string"}},
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
    content_id: str = ""     # ID do conteúdo do arquivo
    from_cache: bool = False  # veio do cache?


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
    """Usada para interromper o worker quando o usuário fecha a janela de log."""
    pass


# ---------------------------
# Utilidades (arquivos)
# ---------------------------

def is_video(path: str) -> bool:
    return os.path.splitext(path.lower())[1] in VIDEO_EXTS


def is_image(path: str) -> bool:
    return os.path.splitext(path.lower())[1] in IMAGE_EXTS


def sanitize_filename(name: str, max_len: int = 120) -> str:
    INVALID_FILENAME_CHARS = '<>:"/\\|?*\n\r\t'
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


def first_n_words(text: str, n: int) -> str:
    words = [w.strip() for w in text.split() if w.strip()]
    words = [w.strip('"""\'') for w in words]
    return " ".join(words[:n]).strip()


def first_n_words_filename(text: str, n: int) -> str:
    return sanitize_filename(first_n_words(text, n))


# ---------------------------
# Config
# ---------------------------

def load_config() -> dict:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_config(data: dict) -> None:
    try:
        # ✅ cria /cache se não existir
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------
# Cache de cenas
# ---------------------------

def load_scene_cache() -> dict:
    """
    Estrutura:
    {
      "version": 1,
      "items": {
         "<content_id>": {
            "desc_text": "...",
            "confidence": 0.9,
            "used_model": "...",
            "doubts": [...],
            "difficulty": [...],
            "fallback_reason": "...",
            "created_at": "...",
            "last_seen_at": "...",
            "last_seen_path": "...",
            "seen_count": 3
         }
      }
    }
    """
    try:
        if os.path.exists(SCENE_CACHE_PATH):
            with open(SCENE_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("version") == SCENE_CACHE_VERSION:
                    if "items" not in data or not isinstance(data["items"], dict):
                        data["items"] = {}
                    return data
    except Exception:
        pass

    return {"version": SCENE_CACHE_VERSION, "items": {}}


def save_scene_cache(cache: dict) -> None:
    try:
        # ✅ cria /cache se não existir
        os.makedirs(os.path.dirname(SCENE_CACHE_PATH), exist_ok=True)

        tmp = SCENE_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SCENE_CACHE_PATH)  # troca atômica
    except Exception:
        pass


def _build_scene_cache_key(
    path: str,
    content_id: str,
    include_video_audio: bool,
    clip_duration_s: Optional[float] = None,
) -> str:
    """
    Monta uma chave de cache mais inteligente.

    Para vídeo, a chave diferencia:
    - conteúdo do arquivo
    - duração do recorte usado
    - se o envio foi com áudio ou sem áudio

    Para imagem, a chave diferencia:
    - conteúdo do arquivo
    - tamanho máximo usado no preparo da imagem
    """
    if is_video(path):
        if clip_duration_s is not None:
            clip_tag = f"center@{clip_duration_s:.3f}"
        else:
            clip_tag = "full"

        audio_tag = "aud1" if include_video_audio else "aud0"
        return f"vid|{content_id}|{clip_tag}|{audio_tag}"

    return f"img|{content_id}|side={DESC_MAX_SIDE}"


def _find_compatible_scene_cache_entry(
    items: dict,
    path: str,
    content_id: str,
    include_video_audio: bool,
    clip_duration_s: Optional[float] = None,
):
    """
    Procura cache compatível nesta ordem:

    1) formato novo ideal (chave rica)
    2) formato simples legado (somente para imagem)
    3) formato antigo do cache antigo: hash|...|aud0 / aud1

    Para vídeo, só reaproveita o cache antigo se o modo de áudio combinar.
    """
    # 1) formato novo ideal
    preferred_key = _build_scene_cache_key(
        path=path,
        content_id=content_id,
        include_video_audio=include_video_audio,
        clip_duration_s=clip_duration_s,
    )
    direct = items.get(preferred_key)
    if isinstance(direct, dict):
        return preferred_key, direct

    # 2) legado do "cache simples" atual
    # Para imagem, pode reaproveitar sem medo.
    if not is_video(path):
        simple = items.get(content_id)
        if isinstance(simple, dict):
            return content_id, simple

    # 3) formato antigo: hash|modo|audX
    if is_video(path):
        preferred_audio_suffix = "|aud1" if include_video_audio else "|aud0"
        legacy_prefix = content_id + "|"

        for key, value in items.items():
            if not isinstance(value, dict):
                continue
            if key.startswith(legacy_prefix) and key.endswith(preferred_audio_suffix):
                return key, value

    return None, None


def _scene_desc_to_cache_entry(desc: SceneDesc) -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    return {
        "desc_text": desc.desc_text,
        "confidence": float(desc.confidence),
        "used_model": desc.used_model,
        "doubts": list(desc.doubts) if desc.doubts else [],
        "difficulty": list(desc.difficulty) if desc.difficulty else [],
        "fallback_reason": desc.fallback_reason or "",
        "created_at": now,
        "last_seen_at": now,
        "last_seen_path": desc.path,
        "seen_count": 1,
    }


def _scene_desc_from_cache_entry(path: str, content_id: str, entry: dict) -> SceneDesc:
    # Atualiza metadados de "última vez visto" (fazemos isso no worker)
    return SceneDesc(
        path=path,
        desc_text=str(entry.get("desc_text", "")).strip(),
        confidence=float(entry.get("confidence", 0.0) or 0.0),
        used_model=str(entry.get("used_model", "")).strip(),
        doubts=[str(x).strip()
                for x in (entry.get("doubts") or []) if str(x).strip()],
        difficulty=[str(x).strip() for x in (
            entry.get("difficulty") or []) if str(x).strip()],
        fallback_reason=str(entry.get("fallback_reason", "") or "").strip(),
        content_id=content_id,
        from_cache=True,
    )


# ---------------------------
# Utilitários de conteúdo
# ---------------------------

def _compute_content_id(path: str, chunk_size: int = 1024 * 1024) -> str:
    """
    ID do conteúdo do arquivo (rápido e bem confiável):
    - usa tamanho + 1MB do começo + 1MB do fim
    - para arquivos pequenos, usa tudo
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
            # lê o final
            read_tail = min(chunk_size, size)
            f.seek(max(0, size - read_tail))
            tail = f.read(read_tail)
            h.update(tail)

    return h.hexdigest()


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
# ffmpeg / ffprobe
# ---------------------------

def _format_ffmpeg_error(cmd: List[str], result: Optional[subprocess.CompletedProcess] = None, exc: Optional[Exception] = None) -> str:
    """
    Monta uma mensagem curta e útil para logar o motivo da falha do ffmpeg.

    stdout = saída padrão do programa.
    stderr = saída de erro do programa (geralmente onde o ffmpeg explica a falha).
    returncode = código de saída; 0 normalmente significa sucesso.
    """
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
            lines = [line.strip()
                     for line in raw_text.splitlines() if line.strip()]
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

    Retorna:
      - caminho do arquivo temporário, se der certo
      - mensagem de erro amigável para log, se der errado
    """
    ffmpeg = get_ffmpeg_bin()
    if not os.path.isabs(ffmpeg) and shutil.which(ffmpeg) is None:
        return None, "ffmpeg não foi encontrado no sistema (não está instalado ou não está no PATH)."

    cmd = []
    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        tmp_path = tmp.name
        tmp.close()

        # Re-encode para ser robusto (cópia direta pode falhar por keyframe)
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

        r = subprocess.run(cmd, capture_output=True,
                           text=True, errors="replace",
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
            return None, "ffmpeg terminou sem gerar arquivo válido (arquivo ausente ou vazio)."

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
    Re-encode do vídeo (libx264) pra ser mais robusto entre formatos.

    Retorna:
      - caminho do arquivo temporário, se der certo
      - mensagem de erro amigável para log, se der errado
    """
    ffmpeg = get_ffmpeg_bin()
    if not os.path.isabs(ffmpeg) and shutil.which(ffmpeg) is None:
        return None, "ffmpeg não foi encontrado no sistema (não está instalado ou não está no PATH)."

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

        r = subprocess.run(cmd, capture_output=True,
                           text=True, errors="replace",
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
            return None, "ffmpeg terminou sem gerar arquivo válido (arquivo ausente ou vazio)."

        return tmp_path, None
    except Exception as exc:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return None, _format_ffmpeg_error(cmd, exc=exc)


def _ffprobe_duration_seconds(path: str) -> Optional[float]:
    """
    Retorna a duração do vídeo em segundos usando ffprobe.
    Se não tiver ffprobe, retorna None (e aí não recorta).
    """
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
    """
    Retorna (start, length). Se o vídeo for <= clip_len_s, retorna (0, duration_s).
    """
    if duration_s <= clip_len_s:
        return 0.0, float(duration_s)
    start = (duration_s - clip_len_s) / 2.0
    return max(0.0, float(start)), float(clip_len_s)


# ---------------------------
# Gemini: reparo e geração estruturada
# ---------------------------

def _repair_to_schema(
    client: genai.Client,
    model_for_repair: str,
    broken_text: str,
    retries: int = 2,
    max_output_tokens: int = 260,
) -> dict:
    """
    Repara uma resposta que veio como "JSON quebrado" (ou nem JSON era),
    pedindo ao modelo para reescrever SOMENTE um JSON válido no schema.

    Usa um modelo barato (ex.: flash-lite) porque aqui é só texto.
    """
    last_err = None
    prompt = (
        "O texto abaixo deveria ser um JSON, mas veio inválido.\n"
        "Reescreva SOMENTE um JSON válido seguindo EXATAMENTE este schema.\n"
        "Não inclua markdown, não inclua explicações, não inclua texto fora do JSON.\n\n"
        "TEXTO PARA REPARAR:\n"
        f"{broken_text}"
    )

    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=model_for_repair,
                contents=[prompt],
                config={
                    "temperature": 0,
                    "max_output_tokens": max_output_tokens,
                    "response_mime_type": "application/json",
                    "response_json_schema": SCENE_DESC_JSON_SCHEMA,
                },
            )

            parsed = getattr(resp, "parsed", None)
            if parsed is not None:
                if hasattr(parsed, "model_dump"):
                    out = parsed.model_dump()
                    if isinstance(out, dict):
                        return out
                if isinstance(parsed, dict):
                    return parsed

            return _safe_json_loads(getattr(resp, "text", "") or "")

        except Exception as e:
            last_err = e
            time.sleep(0.4 * (attempt + 1))

    raise RuntimeError(f"Falha ao reparar JSON: {last_err}")


def _extract_retry_seconds_from_message(msg: str) -> Optional[float]:
    """
    Alguns erros vêm com: 'Please retry in 37.857191072s.'
    """
    if not msg:
        return None
    m = re.search(r"Please retry in\s+([0-9]+(?:\.[0-9]+)?)s", msg)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    # Às vezes vem em ms: 'Please retry in 862.571829ms.'
    m2 = re.search(r"Please retry in\s+([0-9]+(?:\.[0-9]+)?)ms", msg)
    if m2:
        try:
            return float(m2.group(1)) / 1000.0
        except Exception:
            return None
    return None


def _is_quota_pro_zero_error(err: Exception) -> bool:
    """
    Detecta o padrão do seu log:
    - 429 RESOURCE_EXHAUSTED
    - menciona free_tier e model gemini-2.5-pro
    - ou 'limit: 0'
    """
    msg = str(err)
    msg_low = msg.lower()
    if "429" in msg_low and "resource_exhausted" in msg_low:
        if "gemini-2.5-pro" in msg_low and ("limit: 0" in msg_low or "free_tier" in msg_low):
            return True
    return False


class QuotaExhaustedError(RuntimeError):
    """Gemini recusou repetidamente por cota/rate limit temporario."""
    pass


def _is_rate_limit_error(err: Exception) -> bool:
    """
    Reconhece rate limit / quota temporaria do Gemini em varias formas:
    - 429 RESOURCE_EXHAUSTED
    - mensagem contem 'rate limit' / 'rate_limit' / 'quota'
    - nome da excecao parece com ResourceExhausted
    """
    if err is None:
        return False
    msg = str(err).lower()
    cls = type(err).__name__.lower()
    if "resourceexhausted" in cls or "ratelimit" in cls or "toomanyrequests" in cls:
        return True
    if "429" in msg and ("resource_exhausted" in msg or "rate" in msg or "quota" in msg):
        return True
    if "rate limit" in msg or "rate_limit" in msg:
        return True
    if "quota" in msg and ("exceed" in msg or "exhaust" in msg):
        return True
    return False


def _gemini_generate_structured(
    client: genai.Client,
    model: str,
    contents,
    retries: int = 3,
    max_output_tokens: int = 280,
    base_sleep_s: float = 0.25,
) -> dict:
    last_err = None

    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config={
                    "temperature": 0,
                    "max_output_tokens": max_output_tokens,
                    "response_mime_type": "application/json",
                    "response_json_schema": SCENE_DESC_JSON_SCHEMA,
                },
            )

            # 1) Preferir "parsed" (quando o SDK conseguiu validar/parsing)
            parsed = getattr(resp, "parsed", None)
            if parsed is not None:
                if hasattr(parsed, "model_dump"):
                    out = parsed.model_dump()
                    if isinstance(out, dict):
                        return out
                if isinstance(parsed, dict):
                    return parsed

            # 2) Senão, tenta ler o texto como JSON
            raw = getattr(resp, "text", "") or ""
            try:
                return _safe_json_loads(raw)
            except Exception:
                # 3) Se o texto veio quebrado, faz "reparo" automático (barato)
                return _repair_to_schema(
                    client=client,
                    # flash-lite (barato)
                    model_for_repair=GEMINI_IMAGE_MODEL_CHEAP,
                    broken_text=raw,
                    retries=2,
                    max_output_tokens=260,
                )

        except Exception as e:
            last_err = e

            retry_s = _extract_retry_seconds_from_message(str(e))
            if retry_s is not None:
                time.sleep(max(0.0, retry_s))
            else:
                time.sleep(base_sleep_s * (attempt + 1))

    raise RuntimeError(
        f"Falha ao gerar descrição no Gemini ({model}): {last_err}")


def _should_fallback_image(data: dict) -> Tuple[bool, str]:
    conf = float(data.get("confidence", 0.0) or 0.0)
    doubts = [str(x).strip()
              for x in (data.get("doubts") or []) if str(x).strip()]
    diff = [str(x).strip()
            for x in (data.get("difficulty") or []) if str(x).strip()]

    if conf < IMAGE_FALLBACK_CONFIDENCE_THRESHOLD:
        return True, f"confidence baixa ({conf:.2f} < {IMAGE_FALLBACK_CONFIDENCE_THRESHOLD:.2f})"
    if any(d in DIFFICULTY_TRIGGERS_FOR_FALLBACK for d in diff) and conf < 0.80:
        return True, f"dificuldade marcada ({', '.join(diff)})"
    if len(doubts) > 0:
        return True, f"dúvidas retornadas ({len(doubts)} item(ns))"
    return False, ""


def _build_scene_desc_from_data(path: str, data: dict, used_model: str, fallback_reason: str = "") -> SceneDesc:
    summary = (data.get("summary") or "").strip()
    keywords = [str(k).strip()
                for k in (data.get("keywords") or []) if str(k).strip()]
    conf = float(data.get("confidence", 0.0) or 0.0)
    doubts = [str(x).strip()
              for x in (data.get("doubts") or []) if str(x).strip()]
    difficulty = [str(x).strip()
                  for x in (data.get("difficulty") or []) if str(x).strip()]

    desc_text = summary
    if keywords:
        desc_text += " Palavras-chave: " + ", ".join(keywords)

    return SceneDesc(
        path=path,
        desc_text=desc_text,
        confidence=conf,
        used_model=used_model,
        doubts=doubts,
        difficulty=difficulty,
        fallback_reason=fallback_reason,
    )


# ---------------------------
# Gemini: descrição de imagem
# ---------------------------

def describe_image_with_fallback(
    client: genai.Client,
    image_path: str,
    policy: FallbackPolicy,
    log_fn=None,
) -> SceneDesc:
    img_bytes, mime = _prepare_image_bytes_for_gemini(
        image_path, max_side=DESC_MAX_SIDE)

    prompt = (
        "Você é um assistente que descreve CENAS VISUAIS em português.\n"
        "Analise a imagem e retorne SOMENTE um JSON seguindo o schema.\n"
        "Regras:\n"
        "- summary: 1 a 2 frases curtas e objetivas.\n"
        "- keywords: 5 a 12 palavras-chave curtas.\n"
        "- confidence: 0 a 1.\n"
        "- doubts: liste rapidamente o que pode estar incerto (se nada, lista vazia).\n"
        "- difficulty: marque dificuldades percebidas (ex.: texto miúdo, muita coisa acontecendo, baixa luz...).\n"
    )

    contents = [
        prompt,
        types.Part.from_bytes(data=img_bytes, mime_type=mime),
    ]

    # Passo 1 (barato) — se isso der certo, a gente nunca perde.
    cheap_data = _gemini_generate_structured(
        client, GEMINI_IMAGE_MODEL_CHEAP, contents, max_output_tokens=280)
    need_fallback, reason = _should_fallback_image(cheap_data)
    cheap_desc = _build_scene_desc_from_data(
        image_path, cheap_data, GEMINI_IMAGE_MODEL_CHEAP)

    if not need_fallback:
        return cheap_desc

    if log_fn:
        log_fn(f"Fallback sugerido para esta imagem: {reason}")

    # Passo 2 (premium) — PRO, mas só se:
    # - usuário permitiu
    # - e ainda não foi desabilitado por quota
    if policy.allow_pro and (not policy.pro_disabled):
        try:
            pro_data = _gemini_generate_structured(
                client, GEMINI_IMAGE_MODEL_PRO, contents, max_output_tokens=320)
            return _build_scene_desc_from_data(image_path, pro_data, GEMINI_IMAGE_MODEL_PRO, fallback_reason=reason)
        except Exception as e:
            policy.pro_fail_count += 1
            if _is_quota_pro_zero_error(e):
                policy.pro_disabled = True
                policy.pro_disabled_reason = "Quota do gemini-2.5-pro no free tier está 0 (429)."
                if log_fn:
                    log_fn(
                        "PRO indisponível (quota=0). Vou desativar o PRO e usar fallback alternativo / barato.")
            else:
                if log_fn:
                    log_fn(
                        f"Fallback PRO falhou. Vou tentar fallback alternativo / barato. Motivo: {e}")

            if policy.pro_fail_count >= 3 and not policy.pro_disabled:
                policy.pro_disabled = True
                policy.pro_disabled_reason = f"PRO falhou {policy.pro_fail_count}x (resposta vazia/JSON inválido)."
                if log_fn:
                    log_fn(
                        f"Desativando PRO nesta execução: {policy.pro_disabled_reason}")

    # Fallback alternativo (melhor que lite): gemini-2.5-flash
    try:
        mid_data = _gemini_generate_structured(
            client, GEMINI_IMAGE_MODEL_MID, contents, max_output_tokens=320)
        return _build_scene_desc_from_data(image_path, mid_data, GEMINI_IMAGE_MODEL_MID, fallback_reason=reason)
    except Exception as e:
        if log_fn:
            log_fn(
                f"Fallback alternativo (flash) falhou. Vou manter o resultado do flash-lite. Motivo: {e}")
        return cheap_desc


# ---------------------------
# Gemini: descrição de vídeo
# ---------------------------

def _prepare_safe_video_upload_path(src_path: str) -> Tuple[str, bool]:
    """
    Garante um caminho ASCII simples para upload do vídeo.

    Isso evita erro de encoding em alguns ambientes/SDKs quando o nome do
    arquivo tem caracteres como ã, é, ç etc.

    Retorna:
      - caminho que deve ser enviado
      - True se criou cópia temporária e precisa apagar no final
    """
    try:
        src_path.encode("ascii")
        return src_path, False
    except UnicodeEncodeError:
        pass

    ext = os.path.splitext(src_path)[1] or ".mp4"
    tmp = tempfile.NamedTemporaryFile(
        delete=False,
        prefix="video_upload_safe_",
        suffix=ext,
    )
    tmp_path = tmp.name
    tmp.close()

    shutil.copy2(src_path, tmp_path)
    return tmp_path, True


def _wait_file_active(client: genai.Client, uploaded_file, timeout_s: int = 600) -> None:
    t0 = time.time()
    f = uploaded_file
    while getattr(f, "state", None) is not None and getattr(f.state, "name", "") == "PROCESSING":
        if time.time() - t0 > timeout_s:
            raise RuntimeError(
                "Timeout esperando o vídeo ficar ACTIVE no File API.")
        time.sleep(2)
        f = client.files.get(name=f.name)

    if getattr(f, "state", None) is not None and getattr(f.state, "name", "") == "FAILED":
        raise RuntimeError("Upload do vídeo falhou (state=FAILED).")


def describe_video(
    client: genai.Client,
    video_path: str,
    delete_after: bool = True,
    clip_start_s: Optional[float] = None,
    clip_duration_s: Optional[float] = None,
    include_audio: bool = True,
    log_fn=None,
) -> SceneDesc:
    prompt = (
        "Você é um assistente que descreve VÍDEOS em português.\n"
        + ("Analise o vídeo (visual + áudio) e retorne SOMENTE um JSON seguindo o schema.\n"
           if include_audio else
           "Analise o vídeo APENAS pelo visual (sem áudio) e retorne SOMENTE um JSON seguindo o schema.\n")
        + "Regras:\n"
        "- summary: 1 a 2 frases curtas descrevendo o que mais importa no vídeo.\n"
        "- keywords: 5 a 12 palavras-chave.\n"
        "- confidence: 0 a 1.\n"
        "- doubts: liste rapidamente o que pode estar incerto (se nada, lista vazia).\n"
        "- difficulty: marque dificuldades percebidas.\n"
    )

    temp_clip_path = None
    temp_upload_copy_path = None
    upload_path = video_path
    uploaded = None

    if clip_start_s is not None and clip_duration_s is not None:
        temp_clip_path, ffmpeg_error = _ffmpeg_extract_clip_to_temp(
            video_path,
            clip_start_s,
            clip_duration_s,
            include_audio=include_audio
        )
        if temp_clip_path:
            upload_path = temp_clip_path
            if log_fn:
                self_audio = "com áudio" if include_audio else "sem áudio"
                log_fn(f"Usando trecho recortado ({self_audio}).")
        else:
            if log_fn:
                log_fn(
                    "[WARN] Não consegui recortar o vídeo. Vou enviar o vídeo inteiro.")
                if ffmpeg_error:
                    log_fn(f"[FFMPEG] {ffmpeg_error}")
    else:
        if not include_audio:
            temp_clip_path, ffmpeg_error = _ffmpeg_remove_audio_to_temp(
                video_path)
            if temp_clip_path:
                upload_path = temp_clip_path
                if log_fn:
                    log_fn("Enviando vídeo SEM áudio (áudio removido via ffmpeg).")
            else:
                if log_fn:
                    log_fn(
                        "[WARN] Não consegui remover o áudio. Vou enviar com áudio mesmo.")
                    if ffmpeg_error:
                        log_fn(f"[FFMPEG] {ffmpeg_error}")

    upload_path, created_ascii_copy = _prepare_safe_video_upload_path(
        upload_path)
    if created_ascii_copy:
        temp_upload_copy_path = upload_path
        if log_fn:
            log_fn(
                "[UPLOAD] Nome/caminho com acento detectado. "
                "Criei uma cópia temporária com nome simples para evitar erro de encoding."
            )

    mime_type = mimetypes.guess_type(video_path)[0] or "video/mp4"

    try:
        with open(upload_path, "rb") as f:
            uploaded = client.files.upload(
                file=f,
                config={"mime_type": mime_type},
            )

        _wait_file_active(client, uploaded)

        contents = [prompt, uploaded]
        data = _gemini_generate_structured(
            client, GEMINI_VIDEO_MODEL, contents,
            retries=5,
            max_output_tokens=520
        )
        return _build_scene_desc_from_data(video_path, data, GEMINI_VIDEO_MODEL)
    finally:
        if delete_after and uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
        if temp_clip_path:
            try:
                os.remove(temp_clip_path)
            except Exception:
                pass
        if temp_upload_copy_path:
            try:
                os.remove(temp_upload_copy_path)
            except Exception:
                pass


# ---------------------------
# Descrição de cenas (individual e em lote)
# ---------------------------

def describe_single_scene(
    path: str,
    client: genai.Client,
    log_fn=None,
    allow_pro_fallback: bool = True,
    include_video_audio: bool = True,
    stop_event=None,
    scene_cache: Optional[dict] = None,
) -> SceneDesc:
    if stop_event is not None and stop_event.is_set():
        raise ProcessingCancelled("Processamento cancelado pelo usuário.")

    own_cache = scene_cache is None
    cache = scene_cache if scene_cache is not None else load_scene_cache()
    items = cache.setdefault("items", {})

    content_id = _compute_content_id(path)

    # Calcula o perfil da análise do vídeo ANTES de consultar o cache,
    # para que a chave do cache reflita o contexto real.
    clip_start_s = None
    clip_duration_s = None

    if is_video(path):
        duration_s = _ffprobe_duration_seconds(path)
        if duration_s is not None:
            clip_start_s, clip_duration_s = _compute_center_clip_window(
                duration_s,
                VIDEO_CENTER_CLIP_SECONDS,
            )

    cache_key_used, cached_entry = _find_compatible_scene_cache_entry(
        items=items,
        path=path,
        content_id=content_id,
        include_video_audio=include_video_audio,
        clip_duration_s=clip_duration_s,
    )

    if log_fn:
        preferred_cache_key = _build_scene_cache_key(
            path=path,
            content_id=content_id,
            include_video_audio=include_video_audio,
            clip_duration_s=clip_duration_s,
        )
        log_fn(f"[CACHE DEBUG] SCENE_CACHE_PATH = {SCENE_CACHE_PATH}")
        log_fn(f"[CACHE DEBUG] itens no cache = {len(items)}")
        log_fn(f"[CACHE DEBUG] content_id = {content_id}")
        log_fn(f"[CACHE DEBUG] preferred_cache_key = {preferred_cache_key}")
        log_fn(
            f"[CACHE DEBUG] cache_hit = {'SIM' if isinstance(cached_entry, dict) else 'NAO'}"
        )

    if isinstance(cached_entry, dict):
        now = datetime.utcnow().isoformat() + "Z"
        cached_entry["last_seen_at"] = now
        cached_entry["last_seen_path"] = path
        cached_entry["seen_count"] = int(
            cached_entry.get("seen_count", 0) or 0
        ) + 1

        if own_cache:
            save_scene_cache(cache)

        desc = _scene_desc_from_cache_entry(path, content_id, cached_entry)
        if log_fn:
            log_fn(
                f"[CACHE] Reaproveitado: {os.path.basename(path)} | "
                f"cache_key={cache_key_used} | "
                f"modelo={desc.used_model} | conf={desc.confidence:.2f}"
            )
        return desc

    if log_fn:
        tipo = "vídeo" if is_video(path) else "imagem"
        log_fn(f"[ANALISANDO] {os.path.basename(path)} ({tipo})")

    if is_video(path):
        desc = describe_video(
            client=client,
            video_path=path,
            delete_after=True,
            clip_start_s=clip_start_s,
            clip_duration_s=clip_duration_s,
            include_audio=include_video_audio,
            log_fn=log_fn,
        )
    else:
        policy = FallbackPolicy(allow_pro=allow_pro_fallback)
        desc = describe_image_with_fallback(
            client=client,
            image_path=path,
            policy=policy,
            log_fn=log_fn,
        )

    desc.path = path
    desc.content_id = content_id
    desc.from_cache = False

    cache_key_to_save = _build_scene_cache_key(
        path=path,
        content_id=content_id,
        include_video_audio=include_video_audio,
        clip_duration_s=clip_duration_s,
    )

    items[cache_key_to_save] = _scene_desc_to_cache_entry(desc)
    items[cache_key_to_save]["last_seen_path"] = path

    if log_fn:
        log_fn(
            f"[CACHE DEBUG] cache salvo para cache_key = {cache_key_to_save}")

    if own_cache:
        save_scene_cache(cache)

    if log_fn:
        extra = f" | fallback={desc.fallback_reason}" if desc.fallback_reason else ""
        log_fn(
            f"[OK] {os.path.basename(path)} | "
            f"modelo={desc.used_model} | conf={desc.confidence:.2f}{extra}"
        )

    return desc


_DESCRIBE_MAX_WORKERS = 4  # respeita rate-limit da API Gemini


def describe_all_scenes(
    selected_files: List[str],
    client: genai.Client,
    log_fn,
    progress_fn=None,
    allow_pro_fallback: bool = True,
    include_video_audio: bool = True,
    stop_event=None,
) -> List[SceneDesc]:
    cache = load_scene_cache()
    total = len(selected_files)
    results: List[Optional[SceneDesc]] = [None] * total

    progress_lock = threading.Lock()
    completed_count = [0]

    def _process_one(idx: int, file_path: str):
        if stop_event is not None and stop_event.is_set():
            raise ProcessingCancelled("Processamento cancelado pelo usuário.")

        if log_fn:
            log_fn("-" * 60)
            log_fn(f"[{idx}/{total}] {os.path.basename(file_path)}")

        desc = describe_single_scene(
            path=file_path,
            client=client,
            log_fn=log_fn,
            allow_pro_fallback=allow_pro_fallback,
            include_video_audio=include_video_audio,
            stop_event=stop_event,
            scene_cache=cache,
        )

        with progress_lock:
            completed_count[0] += 1
            if progress_fn:
                progress_fn(completed_count[0])

        return idx - 1, desc  # posição 0-indexada

    with ThreadPoolExecutor(max_workers=_DESCRIBE_MAX_WORKERS) as executor:
        futures = {
            executor.submit(_process_one, idx, file_path): idx
            for idx, file_path in enumerate(selected_files, start=1)
        }

        for future in as_completed(futures):
            pos, desc = future.result()  # propaga ProcessingCancelled e outros erros
            results[pos] = desc

    save_scene_cache(cache)
    return [r for r in results if r is not None]


# ---------------------------
# Gemini: Embeddings (texto -> vetor)
# ---------------------------

_EMBEDDING_BACKOFF_SECONDS = (5.0, 10.0, 20.0, 40.0, 60.0)


def get_embeddings_batched(
    client: genai.Client,
    model: str,
    texts: List[str],
    batch_size: int = 100,
    log_fn=None,
    max_attempts: int = 5,
) -> List[List[float]]:
    """
    Gera embeddings em lotes com retry/backoff para rate limit do Gemini.

    Quando o Gemini retorna 429/RESOURCE_EXHAUSTED, tenta novamente ate
    max_attempts vezes por lote, respeitando 'Please retry in Xs/ms' quando
    vier na mensagem; caso contrario usa backoff progressivo com jitter.

    Se esgotar tentativas em um erro de rate limit, levanta
    QuotaExhaustedError para o chamador distinguir de erros definitivos.
    """
    batch_size = min(int(batch_size), 100)  # limite da API
    if batch_size <= 0:
        batch_size = 100
    vectors: List[List[float]] = []
    total = len(texts)
    total_batches = (total + batch_size - 1) // batch_size if total else 0

    for batch_idx, i in enumerate(range(0, total, batch_size), start=1):
        chunk = texts[i:i + batch_size]
        end = i + len(chunk) - 1

        for attempt in range(1, max_attempts + 1):
            try:
                resp = client.models.embed_content(
                    model=model,
                    contents=chunk,
                    config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY"),
                )
                vectors.extend(list(emb.values) for emb in resp.embeddings)
                break
            except Exception as exc:
                is_rate = _is_rate_limit_error(exc)

                if not is_rate:
                    raise

                if attempt >= max_attempts:
                    raise QuotaExhaustedError(
                        f"Gemini recusou embeddings por cota/rate limit "
                        f"(lote {batch_idx}/{total_batches}, itens {i}-{end}, "
                        f"{attempt} tentativa(s)): {exc}"
                    ) from exc

                retry_s = _extract_retry_seconds_from_message(str(exc))
                if retry_s is None:
                    base_idx = min(attempt - 1, len(_EMBEDDING_BACKOFF_SECONDS) - 1)
                    retry_s = _EMBEDDING_BACKOFF_SECONDS[base_idx]
                retry_s = max(1.0, retry_s) + random.uniform(0.0, 1.5)

                if log_fn:
                    log_fn(
                        f"[Gemini-embedding] Lote {batch_idx}/{total_batches} "
                        f"(itens {i}-{end}) recebeu rate limit "
                        f"(tentativa {attempt}/{max_attempts}). "
                        f"Aguardando {retry_s:.1f}s antes de tentar novamente..."
                    )
                time.sleep(retry_s)

    return vectors


def normalize_rows(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


# ---------------------------
# Matching global (cena <-> frase)
# ---------------------------

def _normalize_text_for_stable_match(text: str) -> str:
    text = (text or "").replace("\r", "\n").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _phrase_key(text: str) -> str:
    return _normalize_text_for_stable_match(text)


def load_stable_match_cache() -> dict:
    try:
        if os.path.exists(STABLE_MATCH_CACHE_PATH):
            with open(STABLE_MATCH_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("version") == STABLE_MATCH_CACHE_VERSION:
                    if "items" not in data or not isinstance(data["items"], dict):
                        data["items"] = {}
                    return data
    except Exception:
        pass

    return {"version": STABLE_MATCH_CACHE_VERSION, "items": {}}


def save_stable_match_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STABLE_MATCH_CACHE_PATH), exist_ok=True)

        tmp = STABLE_MATCH_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STABLE_MATCH_CACHE_PATH)
    except Exception:
        pass


def recover_stable_assignments(
    script_items: List[str],
    scene_descs: List[SceneDesc],
    stable_cache: dict,
    log_fn,
) -> Tuple[List[Assignment], Set[int], Set[int]]:
    items = stable_cache.get("items", {})
    if not isinstance(items, dict) or not items:
        return [], set(), set()

    phrase_positions = {}
    for pi, phrase in enumerate(script_items):
        key = _phrase_key(phrase)
        phrase_positions.setdefault(key, []).append(pi)

    assignments: List[Assignment] = []
    used_phrases: Set[int] = set()
    used_scenes: Set[int] = set()

    for si, scene in enumerate(scene_descs):
        if not scene.content_id:
            continue

        entry = items.get(scene.content_id)
        if not isinstance(entry, dict):
            continue

        phrase_key = str(entry.get("phrase_key", "") or "").strip()
        if not phrase_key:
            continue

        candidate_phrase_idxs = phrase_positions.get(phrase_key, [])
        phrase_idx = next(
            (pi for pi in candidate_phrase_idxs if pi not in used_phrases),
            None,
        )
        if phrase_idx is None:
            continue

        score = float(entry.get("last_score", 1.0) or 1.0)

        assignments.append(
            Assignment(
                phrase_idx=phrase_idx,
                scene_idx=si,
                score=score,
            )
        )
        used_phrases.add(phrase_idx)
        used_scenes.add(si)

        log_fn(
            f"[ESTÁVEL] Reaproveitado match antigo: "
            f"{os.path.basename(scene.path)} -> frase #{phrase_idx + 1}"
        )

    assignments.sort(key=lambda a: a.phrase_idx)
    return assignments, used_phrases, used_scenes


def update_stable_match_cache(
    stable_cache: dict,
    assignments: List[Assignment],
    script_items: List[str],
    scene_descs: List[SceneDesc],
    n_words_for_filename: int,
) -> None:
    items = stable_cache.setdefault("items", {})
    now = datetime.utcnow().isoformat() + "Z"

    best_assignment_by_content_id = {}

    for a in assignments:
        scene = scene_descs[a.scene_idx]
        if not scene.content_id:
            continue

        prev = best_assignment_by_content_id.get(scene.content_id)
        if (prev is None) or (a.score > prev.score):
            best_assignment_by_content_id[scene.content_id] = a

    for content_id, a in best_assignment_by_content_id.items():
        scene = scene_descs[a.scene_idx]
        phrase = script_items[a.phrase_idx]

        items[scene.content_id] = {
            "content_id": scene.content_id,
            "phrase_key": _phrase_key(phrase),
            "phrase_preview": phrase[:500],
            "target_base_name": sanitize_filename(
                first_n_words(phrase, n_words_for_filename),
                max_len=140,
            ),
            "last_score": float(a.score),
            "used_model": scene.used_model,
            "updated_at": now,
            "scene_path": scene.path,
        }


def resolve_stable_output_base_name(
    content_id: str,
    phrase: str,
    stable_cache: dict,
    n_words_for_filename: int,
) -> str:
    items = stable_cache.get("items", {})
    entry = items.get(content_id, {}) if isinstance(items, dict) else {}

    current_phrase_key = _phrase_key(phrase)
    saved_phrase_key = str(entry.get("phrase_key", "") or "").strip()
    saved = str(entry.get("target_base_name", "") or "").strip()

    # Só reutiliza o nome estável salvo se a frase atual for exatamente
    # a mesma frase principal registrada no histórico estável.
    if saved and saved_phrase_key and saved_phrase_key == current_phrase_key:
        return sanitize_filename(saved, max_len=140)

    # Se esta cena estiver sendo usada em outra frase diferente,
    # gera o nome com base na frase atual para não colapsar tudo
    # no mesmo nome de arquivo.
    return sanitize_filename(
        first_n_words(phrase, n_words_for_filename),
        max_len=140,
    )


# ---------------------------
# Estado da última execução
# ---------------------------

def load_last_run_state() -> dict:
    try:
        if os.path.exists(LAST_RUN_STATE_PATH):
            with open(LAST_RUN_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("version") == LAST_RUN_STATE_VERSION:
                    return data
    except Exception:
        pass

    return {
        "version": LAST_RUN_STATE_VERSION,
        "use_script": False,
        "roteiro_text": "",
        "all_scene_paths": [],
        "pending_scene_content_ids": [],
        "pending_scene_paths": [],
        "pending_phrase_indices": [],
        "reprocess_pending_attempts": 0,
    }


def save_last_run_state(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LAST_RUN_STATE_PATH), exist_ok=True)

        payload = dict(data)
        payload["version"] = LAST_RUN_STATE_VERSION

        tmp = LAST_RUN_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        os.replace(tmp, LAST_RUN_STATE_PATH)
    except Exception:
        pass


# ---------------------------
# Undo da última execução
# ---------------------------

def load_undo_last_run() -> dict:
    try:
        if os.path.exists(UNDO_LAST_RUN_PATH):
            with open(UNDO_LAST_RUN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("version") == UNDO_LAST_RUN_VERSION:
                    if "operations" not in data or not isinstance(data["operations"], list):
                        data["operations"] = []
                    return data
    except Exception:
        pass

    return {
        "version": UNDO_LAST_RUN_VERSION,
        "created_at": "",
        "status": "empty",
        "operations": [],
    }


def save_undo_last_run(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(UNDO_LAST_RUN_PATH), exist_ok=True)

        payload = dict(data)
        payload["version"] = UNDO_LAST_RUN_VERSION

        tmp = UNDO_LAST_RUN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        os.replace(tmp, UNDO_LAST_RUN_PATH)
    except Exception:
        pass


def clear_undo_last_run() -> None:
    save_undo_last_run({
        "created_at": datetime.utcnow().isoformat() + "Z",
        "status": "empty",
        "operations": [],
    })


def clear_stable_match_cache_file() -> None:
    """
    Remove o cache estável de vínculo entre frase e cena.
    Mantém intactos:
    - config
    - cache de descrição de cenas
    """
    try:
        if os.path.exists(STABLE_MATCH_CACHE_PATH):
            os.remove(STABLE_MATCH_CACHE_PATH)
    except Exception:
        pass


# ---------------------------
# Assignment global
# ---------------------------

def build_global_assignment(
    script_items: List[str],
    scene_descs: List[SceneDesc],
    client: genai.Client,
    log_fn,
    max_uses_per_scene: int = 1,
    initial_scene_use_counts: Optional[dict] = None,
    min_similarity: Optional[float] = None,
    min_assign_score: Optional[float] = None,
) -> Tuple[List[Assignment], Set[int], Set[int]]:
    """
    Faz casamento global exato entre frases e cenas.

    O que muda em relação à versão antiga:
    - não usa mais escolha gulosa (greedy)
    - calcula a matriz completa frase x cena
    - permite repetir cena criando "slots" de uso
    - permite deixar frase sem cena quando nenhuma opção passa do limiar final
    - tenta primeiro um resolvedor exato rápido (SciPy)
    - se SciPy não existir, cai para um resolvedor exato em Python puro
    """
    if not script_items or not scene_descs:
        return [], set(), set()

    max_uses_per_scene = max(1, int(max_uses_per_scene))
    initial_scene_use_counts = {
        int(k): max(0, int(v))
        for k, v in dict(initial_scene_use_counts or {}).items()
    }

    effective_min_similarity = float(
        MIN_SIMILARITY if min_similarity is None else min_similarity
    )
    effective_min_assign_score = float(
        MIN_ASSIGN_SCORE if min_assign_score is None else min_assign_score
    )

    # Escala inteira para o otimizador trabalhar com custo estável.
    # Ex.: score 0.75321 -> 75321
    SCORE_SCALE = 100000

    log_fn(f"Gerando embeddings do roteiro ({len(script_items)} itens)...")
    script_vecs = get_embeddings_batched(
        client, GEMINI_EMBEDDING_MODEL, script_items, log_fn=log_fn
    )
    script_mat = normalize_rows(np.array(script_vecs, dtype=np.float32))

    log_fn(f"Gerando embeddings das cenas ({len(scene_descs)} descrições)...")
    scene_texts = [s.desc_text for s in scene_descs]
    scene_vecs = get_embeddings_batched(
        client, GEMINI_EMBEDDING_MODEL, scene_texts, log_fn=log_fn
    )
    scene_mat = normalize_rows(np.array(scene_vecs, dtype=np.float32))

    log_fn("Calculando matriz completa de similaridade frase x cena...")
    base_score_matrix = script_mat @ scene_mat.T

    # Ajuste pela confiança da descrição da cena.
    scene_confidence_factors = np.array(
        [(0.6 + 0.4 * float(s.confidence)) for s in scene_descs],
        dtype=np.float32,
    )
    base_score_matrix = base_score_matrix * \
        scene_confidence_factors[np.newaxis, :]

    slot_scene_indices: List[int] = []
    slot_use_numbers: List[int] = []

    for scene_idx in range(len(scene_descs)):
        already_used = initial_scene_use_counts.get(scene_idx, 0)
        if already_used >= max_uses_per_scene:
            continue

        for use_no in range(already_used, max_uses_per_scene):
            slot_scene_indices.append(scene_idx)
            slot_use_numbers.append(use_no)

    if not slot_scene_indices:
        log_fn("Nenhum slot de cena disponível para casar com as frases.")
        return [], set(), set()

    slot_scene_indices_arr = np.array(slot_scene_indices, dtype=np.int32)
    slot_use_numbers_arr = np.array(slot_use_numbers, dtype=np.int32)
    slot_decay_factors = np.power(
        np.float32(SCENE_REUSE_SCORE_DECAY),
        slot_use_numbers_arr,
    ).astype(np.float32)

    # Cada slot representa um possível uso daquela cena.
    # Se max_uses_per_scene=3, a mesma cena pode virar 3 colunas diferentes.
    score_matrix = (
        base_score_matrix[:, slot_scene_indices_arr]
        * slot_decay_factors[np.newaxis, :]
    ).astype(np.float32)

    valid_mask = score_matrix >= effective_min_similarity
    valid_edges = int(valid_mask.sum())

    if valid_edges <= 0:
        log_fn(
            f"Nenhuma combinação frase/cena passou do limiar {effective_min_similarity:.3f}."
        )
        return [], set(), set()

    log_fn(
        f"Otimizando globalmente {len(script_items)} frase(s), "
        f"{len(scene_descs)} cena(s), {len(slot_scene_indices)} slot(s) de uso e "
        f"{valid_edges} combinação(ões) válidas..."
    )

    score_int_matrix = np.rint(score_matrix * SCORE_SCALE).astype(np.int64)
    max_score_int = int(score_int_matrix[valid_mask].max())

    # Nota mínima para valer mais a pena usar uma cena
    # do que deixar a frase sem cena.
    min_assign_score_int = int(round(effective_min_assign_score * SCORE_SCALE))

    # "Âncora" para manter todos os custos não negativos.
    cost_anchor_int = max(max_score_int, min_assign_score_int)

    # Custo da opção "sem cena".
    # Se a cena tiver score abaixo de MIN_ASSIGN_SCORE,
    # o dummy ("sem cena") fica melhor.
    unmatched_cost = cost_anchor_int - min_assign_score_int

    # Custo bem alto para combinações inválidas.
    invalid_cost = (cost_anchor_int + SCORE_SCALE) * 1000

    n_phrases = len(script_items)
    n_slots = len(slot_scene_indices)

    # Matriz retangular:
    # - colunas [0:n_slots]      -> slots reais de cena
    # - colunas [n_slots:...]    -> colunas dummy (frase sem cena)
    cost_matrix = np.full(
        (n_phrases, n_slots + n_phrases),
        unmatched_cost,
        dtype=np.int64,
    )

    # Quanto maior o score, menor o custo.
    real_slot_costs = cost_anchor_int - score_int_matrix
    cost_matrix[:, :n_slots] = np.where(
        valid_mask, real_slot_costs, invalid_cost
    )

    min_accept_score = max(effective_min_similarity,
                           effective_min_assign_score)

    def _collect_assignments_from_columns(
        chosen_cols: List[int],
    ) -> Tuple[List[Assignment], Set[int], Set[int]]:
        assignments: List[Assignment] = []
        used_phrases: Set[int] = set()
        scene_use_counts = {
            int(k): int(v)
            for k, v in initial_scene_use_counts.items()
            if int(v) > 0
        }

        for phrase_idx, col_idx in enumerate(chosen_cols):
            if col_idx is None:
                continue

            if col_idx < 0 or col_idx >= n_slots:
                # coluna dummy = frase fica sem cena
                continue

            if not valid_mask[phrase_idx, col_idx]:
                continue

            score = float(score_matrix[phrase_idx, col_idx])
            if score < min_accept_score:
                continue

            scene_idx = int(slot_scene_indices_arr[col_idx])
            assignments.append(
                Assignment(
                    phrase_idx=phrase_idx,
                    scene_idx=scene_idx,
                    score=score,
                )
            )
            used_phrases.add(phrase_idx)
            scene_use_counts[scene_idx] = scene_use_counts.get(
                scene_idx, 0) + 1

        assignments.sort(key=lambda a: a.phrase_idx)
        used_scenes = {
            si for si, count in scene_use_counts.items()
            if count > 0
        }
        return assignments, used_phrases, used_scenes

    # -----------------------------
    # Tentativa 1: resolvedor exato rápido (SciPy)
    # -----------------------------
    try:
        from scipy.optimize import linear_sum_assignment

        row_ind, col_ind = linear_sum_assignment(cost_matrix)
        chosen_cols = [None] * n_phrases
        for r, c in zip(row_ind.tolist(), col_ind.tolist()):
            chosen_cols[int(r)] = int(c)

        log_fn("Otimização global exata concluída com SciPy.")
        return _collect_assignments_from_columns(chosen_cols)

    except Exception as scipy_err:
        log_fn(
            "SciPy não disponível (ou falhou). "
            "Vou usar fallback exato em Python puro. "
            f"Motivo: {scipy_err}"
        )

    # -----------------------------
    # Tentativa 2: min-cost max-flow exato em Python puro
    # -----------------------------
    import heapq

    def _add_edge(graph, u: int, v: int, cap: int, cost: int):
        graph[u].append([v, len(graph[v]), cap, cost])
        graph[v].append([u, len(graph[u]) - 1, 0, -cost])
        return graph[u][-1]

    source = 0
    phrase_offset = 1
    slot_offset = phrase_offset + n_phrases
    sink = slot_offset + n_slots
    node_count = sink + 1

    graph = [[] for _ in range(node_count)]
    phrase_slot_edges = [[] for _ in range(n_phrases)]

    for phrase_idx in range(n_phrases):
        phrase_node = phrase_offset + phrase_idx
        _add_edge(graph, source, phrase_node, 1, 0)

        # opção dummy: frase pode terminar sem cena
        _add_edge(graph, phrase_node, sink, 1, unmatched_cost)

        valid_slot_cols = np.flatnonzero(valid_mask[phrase_idx])
        for col_idx in valid_slot_cols.tolist():
            slot_node = slot_offset + col_idx
            edge = _add_edge(
                graph,
                phrase_node,
                slot_node,
                1,
                int(real_slot_costs[phrase_idx, col_idx]),
            )
            phrase_slot_edges[phrase_idx].append((col_idx, edge))

    for col_idx in range(n_slots):
        slot_node = slot_offset + col_idx
        _add_edge(graph, slot_node, sink, 1, 0)

    target_flow = n_phrases
    flow = 0
    potentials = [0] * node_count
    total_cost = 0

    while flow < target_flow:
        dist = [10**18] * node_count
        parent = [None] * node_count
        dist[source] = 0
        pq = [(0, source)]

        while pq:
            cur_dist, u = heapq.heappop(pq)
            if cur_dist != dist[u]:
                continue
            if u == sink:
                break

            for edge_idx, edge in enumerate(graph[u]):
                v, rev, cap, cost = edge
                if cap <= 0:
                    continue

                reduced_cost = cost + potentials[u] - potentials[v]
                nd = cur_dist + reduced_cost
                if nd < dist[v]:
                    dist[v] = nd
                    parent[v] = (u, edge_idx)
                    heapq.heappush(pq, (nd, v))

        if parent[sink] is None:
            break

        for i in range(node_count):
            if dist[i] < 10**18:
                potentials[i] += dist[i]

        add_flow = target_flow - flow
        v = sink
        while v != source:
            u, edge_idx = parent[v]
            add_flow = min(add_flow, graph[u][edge_idx][2])
            v = u

        v = sink
        while v != source:
            u, edge_idx = parent[v]
            edge = graph[u][edge_idx]
            rev_idx = edge[1]
            edge[2] -= add_flow
            graph[v][rev_idx][2] += add_flow
            total_cost += edge[3] * add_flow
            v = u

        flow += add_flow

    chosen_cols = [None] * n_phrases
    for phrase_idx in range(n_phrases):
        for col_idx, edge in phrase_slot_edges[phrase_idx]:
            # Quando a aresta é usada, a capacidade vai de 1 para 0.
            if edge[2] == 0:
                chosen_cols[phrase_idx] = col_idx
                break

    log_fn("Otimização global exata concluída com fallback Python puro.")
    return _collect_assignments_from_columns(chosen_cols)


# ---------------------------
# Classe principal
# ---------------------------

class SceneRenamerManager:
    def __init__(self, gemini_api_key: str, log_fn=None):
        self.client = genai.Client(api_key=gemini_api_key)
        self.log_fn = log_fn or (lambda x: None)

    def describe_scenes(self, selected_files, allow_pro_fallback=True, include_video_audio=True, stop_event=None, progress_fn=None):
        # chama describe_all_scenes com self.client e self.log_fn
        return describe_all_scenes(selected_files, self.client, self.log_fn, progress_fn=progress_fn, allow_pro_fallback=allow_pro_fallback, include_video_audio=include_video_audio, stop_event=stop_event)

    def compute_assignments(self, script_items, scene_descs, max_uses_per_scene=1, initial_scene_use_counts=None, min_similarity=None, min_assign_score=None):
        return build_global_assignment(script_items, scene_descs, self.client, self.log_fn, max_uses_per_scene=max_uses_per_scene, initial_scene_use_counts=initial_scene_use_counts, min_similarity=min_similarity or MIN_SIMILARITY, min_assign_score=min_assign_score or MIN_ASSIGN_SCORE)

    def get_embeddings(self, texts):
        return get_embeddings_batched(self.client, GEMINI_EMBEDDING_MODEL, texts, log_fn=self.log_fn)

    def load_config(self):
        return load_config()

    def save_config(self, data):
        save_config(data)
