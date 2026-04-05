import os
import re
import json
import time
import textwrap
import subprocess
import unicodedata
from typing import Optional, Any, List, Tuple, Dict

import requests
from requests.adapters import HTTPAdapter

from ..entities import Result, Dimensions, Transcription, TranscriptionWord


class TextOnScreenManager:
    """
    Gera vídeos de texto com fundo transparente (alpha) para colocar por cima do vídeo no Premiere.

    Fluxo:
    1) A partir da Transcription (lista de palavras com timestamps), cria "segmentos" (frases candidatas).
    2) Pede para o GPT escolher só as frases mais impactantes (limitadas por max_phrases).
    3) Renderiza essas frases em .mov com transparência via FFmpeg.
    4) (Opcional) Insere os .mov na timeline do Premiere em uma trilha de vídeo específica.
    """

    OPENAI_API_KEY: str = ''
    OPENAI_BASE_URL: str = 'https://api.openai.com/v1'
    # Modelo default seguro p/ Structured Outputs; você pode trocar depois no settings
    OPENAI_MODEL: str = 'gpt-4o-mini'

    def __init__(self, openai_api_key: str = '', ffmpeg_bin: str = 'ffmpeg'):
        self.OPENAI_API_KEY = openai_api_key or ''
        self.FFMPEG_BIN = ffmpeg_bin or 'ffmpeg'

        # Session HTTP com keep-alive (evita abrir conexão nova toda hora)
        self._sess = requests.Session()
        self._sess.headers.update({"Connection": "keep-alive"})
        try:
            self._sess.mount(
                "https://", HTTPAdapter(pool_connections=10, pool_maxsize=10))
        except Exception:
            pass

    def set_api_key(self, openai_api_key: str):
        self.OPENAI_API_KEY = openai_api_key or ''

    # -----------------------------
    # Utils: texto / normalização
    # -----------------------------
    def __clean_text(self, s: str) -> str:
        s = (s or '').strip()

        # remove control/invisíveis (inclui \r)
        s = self.__strip_control_chars(s)
        s = s.replace('\r', '')

        # normaliza espaços
        s = re.sub(r'\s+', ' ', s)
        return s

    def __format_overlay_phrase(self, s: str) -> str:
        """
        Deixa a frase "bonita" para overlay:
        - remove muletas no começo (opcional)
        - primeira letra maiúscula
        - adiciona pontuação no final se faltar
        """
        s = (s or "").strip()
        s = re.sub(r"\s+", " ", s)

        # remove algumas muletas comuns no começo (sem exagerar)
        fillers = ("e ", "aí ", "ai ", "então ", "tipo ", "assim ")
        low = s.lower()
        for f in fillers:
            if low.startswith(f) and len(s.split()) >= 5:
                s = s[len(f):].lstrip()
                break

        # primeira letra maiúscula (sem destruir o resto)
        for i, ch in enumerate(s):
            if ch.isalpha():
                s = s[:i] + ch.upper() + s[i + 1:]
                break

        # pontuação final
        if s and s[-1] not in ".!?…":
            s += "."

        return s

    def __sanitize_overlay_text(self, s: str) -> str:
        """
        Remove caracteres invisíveis/estranhos que o FFmpeg pode desenhar como "quadradinho".
        Importante: NÃO mantém "Marks" (categoria Unicode 'M'), porque eles podem virar □ no fim da linha.
        """
        s = unicodedata.normalize("NFKC", (s or ""))

        # normaliza quebras e remove CR
        s = s.replace("\r", "")
        s = s.replace("\u2028", "\n").replace(
            "\u2029", "\n")  # line/paragraph sep -> \n
        s = s.replace("\u00A0", " ")  # NBSP -> espaço normal

        # remove variation selectors (podem virar quadrado no fim de linha)
        s = re.sub(r"[\uFE0E\uFE0F]", "", s)

        out = []
        for ch in s:
            if ch == "\n":
                out.append("\n")
                continue

            if ch.isspace():
                out.append(" ")
                continue

            cat = unicodedata.category(ch)
            head = cat[0]  # L/M/N/P/S/Z/C

            # Permite só: letras, números e pontuação
            # (não permite M = marks, nem S = symbols)
            if head in ("L", "N", "P"):
                out.append(ch)
                continue

            # ignora qualquer outra coisa (M/S/C/etc)
            continue

        s = "".join(out)

        # colapsa espaços
        s = re.sub(r"[ \t]{2,}", " ", s)

        # remove espaços no começo/fim de cada linha
        s = "\n".join([line.strip() for line in s.split("\n")])

        # evita excesso de linhas vazias
        s = re.sub(r"\n{3,}", "\n\n", s)

        return s.strip()

    def __strip_control_chars(self, s: str) -> str:
        """
        Remove caracteres invisíveis/controle que o FFmpeg pode renderizar como 'quadradinhos'.
        Ex.: '\r' (carriage return), zero-width spaces, etc.
        """
        if not s:
            return s

        out = []
        for ch in s:
            if ch == '\n':
                out.append(ch)
                continue

            cat = unicodedata.category(ch)
            # C* = Control/Format/Surrogate/Private/Unassigned
            if cat.startswith('C'):
                continue

            out.append(ch)

        return ''.join(out)

    def __wrap_text(self, text: str, max_chars: int) -> str:
        """
        Quebra o texto em múltiplas linhas (aprox) para não estourar a largura do vídeo.
        Obs: é uma aproximação por caracteres (não por pixels).
        """
        text = self.__clean_text(text)
        if max_chars <= 0:
            return text
        lines = textwrap.wrap(text, width=max_chars)
        return "\n".join(lines) if lines else text

    def __safe_slug(self, s: str, max_len: int = 40) -> str:
        s = self.__clean_text(s).lower()
        s = re.sub(r'[^a-z0-9]+', '_', s)
        s = s.strip('_')
        if len(s) > max_len:
            s = s[:max_len].rstrip('_')
        return s or 'clip'

    def __ffmpeg_escape_drawtext_path(self, p: str) -> str:
        """
        drawtext usa ":" como separador. Em Windows, caminhos têm "C:\", então precisamos escapar ":".
        A forma mais segura é usar / e escapar ":" como "\:".
        """
        p = os.path.abspath(p)
        p = p.replace('\\', '/')
        p = p.replace(':', r'\:')
        p = p.replace("'", r"\'")
        return p

    def __ffmpeg_escape_drawtext_text_oneline(self, s: str) -> str:
        """
        Escapa texto para drawtext=text='...'
        (sem \n, porque cada linha será um drawtext separado)
        """
        s = unicodedata.normalize("NFC", (s or ""))
        s = s.replace("\r", "")
        s = s.replace("\\", "\\\\")
        s = s.replace(":", r"\:")
        s = s.replace(",", r"\,")   # importante (vírgula separa filtros)
        s = s.replace("[", r"\[")
        s = s.replace("]", r"\]")
        s = s.replace("'", r"\'")
        return s

    def __estimate_y_center_px(self, y_pos_expr: str, H: int) -> float:
        """
        Você passa y_pos como expressão tipo:
          (h*0.78)-text_h/2   ou   (h-text_h)/2
        Aqui a gente estima o "centro" em pixels só pra posicionar várias linhas.
        """
        yp = (y_pos_expr or "").replace(" ", "")
        if "0.18" in yp:
            return 0.18 * H
        if "0.78" in yp:
            return 0.78 * H
        if "(h-text_h)/2" in yp or "h-text_h" in yp:
            return 0.50 * H
        return 0.78 * H

    def __ffmpeg_escape_drawtext_text(self, s: str) -> str:
        """
        Escapa texto para drawtext=text='...'
        Importante no FFmpeg:
        - ',' separa filtros na filtergraph -> precisa virar '\,'
        - ':' separa opções do drawtext -> precisa virar '\:'
        - '[' e ']' podem ser interpretados pela filtergraph -> escape também
        - '\' e "'" precisam de escape
        - quebra de linha: queremos virar '\n' (no drawtext), mas na filtergraph precisa ser '\\n'
        """
        s = s or ""
        s = unicodedata.normalize("NFC", s)

        # normaliza newlines e remove CR
        s = s.replace("\r", "")

        # ordem importa: primeiro backslash
        s = s.replace("\\", "\\\\")      # \  -> \\
        s = s.replace(":", r"\:")        # :  -> \:
        s = s.replace(",", r"\,")        # ,  -> \,
        s = s.replace("[", r"\[")        # [  -> \[
        s = s.replace("]", r"\]")        # ]  -> \]
        s = s.replace("'", r"\'")        # '  -> \'

        # newline: drawtext entende \n como quebra de linha
        s = s.replace("\n", r"\n")

        return s

    def __find_fontfile(self) -> Optional[str]:
        """
        Procura uma fonte TTF comum em Windows/Mac/Linux.
        Se não achar, FFmpeg pode tentar fontconfig (font='Arial'), mas nem sempre funciona.
        """
        candidates = []

        windir = os.environ.get('WINDIR', r'C:\Windows')
        candidates += [
            os.path.join(windir, 'Fonts', 'arial.ttf'),
            os.path.join(windir, 'Fonts', 'Arial.ttf'),
            os.path.join(windir, 'Fonts', 'segoeui.ttf'),
            os.path.join(windir, 'Fonts', 'SegoeUI.ttf'),
        ]

        candidates += [
            '/System/Library/Fonts/Supplemental/Arial.ttf',
            '/Library/Fonts/Arial.ttf',
            '/System/Library/Fonts/Supplemental/Helvetica.ttf',
        ]

        candidates += [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        ]

        for p in candidates:
            try:
                if p and os.path.exists(p):
                    return p
            except Exception:
                pass
        return None

    # -----------------------------
    # 1) Criar "segmentos" a partir da transcrição
    # -----------------------------
    @staticmethod
    def __ends_sentence(word_text: str) -> bool:
        """Verifica se a palavra termina uma frase (pontuação final)."""
        t = (word_text or "").strip()
        return t.endswith(('.', '!', '?', '...', '."', '!"', '?"'))

    def __build_segments_from_words(
        self,
        words: List[TranscriptionWord],
        *,
        gap_ms: int = 700,
        min_words: int = 5,
        max_words: int = 30
    ) -> List[Dict[str, Any]]:
        """
        Monta segmentos a partir de limites de frase (pontuação) e pausas (silêncios).
        Prioridade: cortar em pontuação final (.!?) para gerar frases completas.
        Fallback: cortar em pausas longas se não houver pontuação.
        """
        if not words:
            return []

        segments: List[Dict[str, Any]] = []
        cur: List[TranscriptionWord] = []

        split_gap_ms = max(250, gap_ms // 2)

        def add_chunk(chunk: List[TranscriptionWord]):
            if len(chunk) < min_words:
                return
            text = " ".join([(w.text or "").strip()
                            for w in chunk if w and w.text])
            text = self.__clean_text(text)
            if not text:
                return
            segments.append({
                "text": text,
                "start_ms": int(chunk[0].start),
                "end_ms": int(chunk[-1].end),
                "words": chunk
            })

        def flush():
            nonlocal cur
            if not cur:
                return

            remaining = cur

            while remaining:
                if len(remaining) <= max_words:
                    add_chunk(remaining)
                    break

                # 1) Procura pontuação final dentro do range max_words (de trás pra frente)
                cut = None
                search_end = min(max_words - 1, len(remaining) - 2)
                for i in range(search_end, min_words - 1, -1):
                    if self.__ends_sentence((remaining[i].text or "")):
                        cut = i + 1
                        break

                # 2) Fallback: procura pausa longa
                if cut is None:
                    for i in range(search_end, -1, -1):
                        gap = int(remaining[i + 1].start) - int(remaining[i].end)
                        if gap >= split_gap_ms:
                            cut = i + 1
                            break

                if cut is None:
                    add_chunk(remaining[:max_words])
                    break

                add_chunk(remaining[:cut])
                remaining = remaining[cut:]

            cur = []

        last_end = None
        for w in words:
            if w is None or w.text is None or w.start is None or w.end is None:
                continue

            # Flush no limite de frase (pontuação) se já temos palavras suficientes
            if cur and self.__ends_sentence((cur[-1].text or "")) and len(cur) >= min_words:
                flush()

            # Flush em pausas longas
            if last_end is not None and int(w.start) - int(last_end) >= gap_ms:
                flush()

            cur.append(w)
            last_end = int(w.end)

        flush()
        return segments

    def __filter_segments(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Remove segmentos ruins:
        - repetidos
        - muito curtos
        - metadados (cena 1, parte 2...)
        - começa com muletas muito comuns (quando der pra cortar)
        """
        out: List[Dict[str, Any]] = []
        seen = set()

        bad_patterns = [
            r'^\s*cena\s*\d+\s*$',
            r'^\s*parte\s*\d+\s*$',
            r'^\s*cap[ií]tulo\s*\d+\s*$',
        ]

        for seg in segments:
            t = self.__clean_text(seg.get("text", ""))
            if not t:
                continue

            # evita duplicados
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)

            # remove "cena 1", "parte 2" etc
            if any(re.match(p, key) for p in bad_patterns):
                continue

            # evita segmentos muito curtos (1 palavra isolada)
            if len(t.split()) < 2:
                continue

            # remove linhas com muitos dígitos (geralmente nomes de arquivo / timestamps)
            digits = sum(ch.isdigit() for ch in t)
            if len(t) > 0 and (digits / max(1, len(t))) > 0.18:
                continue

            out.append(seg)

        return out

    # -----------------------------
    # 2) Selecionar frases impactantes (GPT ou fallback)
    # -----------------------------
    def __openai_select_segments(
        self,
        segments: List[Dict[str, Any]],
        *,
        max_phrases: int,
        min_gap_ms: int,
        language: str = "pt-BR",
        timeout_s: int = 25
    ) -> Optional[List[Dict[str, Any]]]:
        """
        GPT escolhe índices + devolve overlay_text formatado (maiúscula + pontuação),
        sem inventar palavras.
        """
        if not self.OPENAI_API_KEY:
            return None
        if not segments:
            return []

        cap = min(len(segments), 150)
        segs = segments[:cap]

        items = []
        for i, s in enumerate(segs):
            start_s = float(s["start_ms"]) / 1000.0
            end_s = float(s["end_ms"]) / 1000.0
            wc = len((s["text"] or "").split())
            items.append(
                f"{i}. [{start_s:.2f}s–{end_s:.2f}s] ({wc} palavras) {s['text']}")

        system = (
            "Você escolhe trechos de fala para virar TEXTO NA TELA em um vídeo.\n"
            "Regras:\n"
            f"- Idioma: {language}\n"
            f"- Escolha exatamente {max_phrases} trechos (ou o máximo disponível se houver menos candidatos).\n"
            f"- Evite escolher trechos a menos de {min_gap_ms/1000:.1f}s um do outro.\n"
            "- Escolha somente trechos que sejam uma FRASE COMPLETA (do início ao ponto final).\n"
            "- NUNCA escolha fragmentos que comecem ou terminem no meio de uma frase.\n"
            "- Evite muletas/genericões (ex: 'e aí galera', 'vamos lá', 'tipo assim').\n"
            "- IMPORTANTE: tente preencher o máximo de trechos pedido, distribuindo ao longo do vídeo inteiro.\n"
            "\n"
            "Para cada item escolhido, gere 'overlay_text' assim:\n"
            "- Mesmas palavras (não invente), mas pode ajustar:\n"
            "  * primeira letra maiúscula\n"
            "  * pontuação final (., ! ou ?)\n"
            "  * pode remover no máximo 1 muleta no começo (ex: 'e', 'aí', 'então') se ficar melhor.\n"
            "- overlay_text deve parecer uma frase completa.\n"
        )

        user = "Candidatos:\n" + "\n".join(items)

        schema = {
            "type": "object",
            "properties": {
                "selected": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "overlay_text": {"type": "string"}
                        },
                        "required": ["index", "overlay_text"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["selected"],
            "additionalProperties": False
        }

        body = {
            "model": self.OPENAI_MODEL,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "impact_phrases",
                    "strict": True,
                    "schema": schema
                }
            },
            "temperature": 0.2,
            "max_output_tokens": 2000,
            "store": False
        }

        url = f"{self.OPENAI_BASE_URL}/responses"
        headers = {
            "Authorization": f"Bearer {self.OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }

        for _ in range(2):
            try:
                resp = self._sess.post(
                    url, headers=headers, json=body, timeout=timeout_s)
                if resp.status_code >= 400:
                    time.sleep(0.35)
                    continue

                data = resp.json()

                out_texts: List[str] = []
                for item in data.get("output", []) or []:
                    if item.get("type") != "message":
                        continue
                    for c in item.get("content", []) or []:
                        if c.get("type") == "output_text" and isinstance(c.get("text"), str):
                            out_texts.append(c["text"])

                if not out_texts:
                    return None

                raw = out_texts[-1].strip()
                parsed = json.loads(raw)
                selected = parsed.get("selected", [])

                # valida índices e normaliza overlay_text
                cleaned = []
                used = set()
                for it in selected:
                    try:
                        idx = int(it.get("index"))
                    except Exception:
                        continue
                    if idx < 0 or idx >= len(segs):
                        continue
                    if idx in used:
                        continue
                    used.add(idx)

                    ot = (it.get("overlay_text") or "").strip()
                    ot = self.__clean_text(ot)
                    ot = self.__format_overlay_phrase(ot)
                    cleaned.append({"index": idx, "overlay_text": ot})

                # Se o GPT devolveu vazio, força fallback (heurística)
                if not cleaned:
                    return None

                return cleaned[:max_phrases]
            except Exception:
                time.sleep(0.35)

        return None

    def __fallback_select(
        self,
        segments: List[Dict[str, Any]],
        *,
        max_phrases: int,
        min_gap_ms: int
    ) -> List[int]:
        """
        Caso não tenha GPT (sem chave/erro), escolhe por heurística.
        Estratégia: prefere segmentos de tamanho médio e espaça no tempo.
        """
        if not segments:
            return []

        scored: List[Tuple[float, int]] = []
        for i, s in enumerate(segments):
            words = self.__clean_text(s["text"]).split(" ")
            n = len([w for w in words if w])
            score = -abs(n - 10)  # ideal ~ 10 palavras

            strong = ["verdade", "segredo", "importante", "nunca",
                      "sempre", "mudar", "transformar", "agora", "atenção"]
            low = s["text"].lower()
            score += sum(0.6 for k in strong if k in low)

            scored.append((score, i))

        scored.sort(reverse=True, key=lambda x: x[0])

        picked: List[int] = []
        last_end = -10**18
        for _, idx in scored:
            s = segments[idx]
            if s["start_ms"] - last_end < min_gap_ms:
                continue
            picked.append(idx)
            last_end = s["end_ms"]
            if len(picked) >= max_phrases:
                break

        picked.sort(key=lambda i: segments[i]["start_ms"])
        return picked

    def select_impact_phrases(
        self,
        segments: List[Dict[str, Any]],
        *,
        max_phrases: int = 5,
        min_gap_seconds: float = 8.0,
        language: str = "pt-BR"
    ) -> List[Dict[str, Any]]:
        min_gap_ms = int(max(0.0, min_gap_seconds) * 1000)

        # Se o gap minimo x max_phrases > duracao total, reduz o gap automaticamente
        if segments:
            total_dur_ms = segments[-1]["end_ms"] - segments[0]["start_ms"]
            needed_ms = min_gap_ms * max_phrases
            if needed_ms > total_dur_ms and max_phrases > 1:
                min_gap_ms = max(2000, int(total_dur_ms / (max_phrases + 1)))
                print(f"[impact] gap ajustado para {min_gap_ms/1000:.1f}s (duracao total: {total_dur_ms/1000:.0f}s, frases: {max_phrases})")

        chosen = self.__openai_select_segments(
            segments,
            max_phrases=max_phrases,
            min_gap_ms=min_gap_ms,
            language=language
        )

        # Se veio lista vazia, trata como falha e usa fallback
        if not chosen:
            chosen = None

        picked: List[Dict[str, Any]] = []
        last_end = -10**18

        if chosen is None:
            # fallback antigo (heurística)
            idxs = self.__fallback_select(
                segments, max_phrases=max_phrases, min_gap_ms=min_gap_ms)

            for i in idxs:
                s = segments[i]
                if s["start_ms"] - last_end < min_gap_ms:
                    continue

                s2 = dict(s)
                s2["overlay_text"] = self.__format_overlay_phrase(
                    s2.get("text", ""))
                picked.append(s2)

                last_end = s["end_ms"]
                if len(picked) >= max_phrases:
                    break

            return picked

        # GPT trouxe index + overlay_text
        # ordena por tempo
        chosen_sorted = sorted(
            chosen, key=lambda it: segments[it["index"]]["start_ms"])

        for it in chosen_sorted:
            i = it["index"]
            s = segments[i]
            if s["start_ms"] - last_end < min_gap_ms:
                continue

            s2 = dict(s)
            s2["overlay_text"] = self.__format_overlay_phrase(
                it.get("overlay_text", s2.get("text", "")))
            picked.append(s2)

            last_end = s["end_ms"]
            if len(picked) >= max_phrases:
                break

        return picked

    # -----------------------------
    # 3) Renderizar vídeos transparentes com FFmpeg
    # -----------------------------
    def __write_ffmpeg_debug(self, out_dir: str, cmd: List[str], proc: subprocess.CompletedProcess, tag: str):
        try:
            os.makedirs(out_dir, exist_ok=True)
            p = os.path.join(out_dir, f'ffmpeg_debug_{tag}.txt')
            with open(p, 'w', encoding='utf-8') as f:
                f.write("CMD:\n" + " ".join(cmd) + "\n\n")
                f.write("STDOUT:\n" + (proc.stdout or "") + "\n\n")
                f.write("STDERR:\n" + (proc.stderr or "") + "\n")
        except Exception:
            pass

    def __run_ffmpeg(self, cmd: List[str], debug_dir: Optional[str] = None, tag: str = "run") -> bool:
        try:
            proc = subprocess.run(
                cmd, check=False, capture_output=True, text=True)
            if debug_dir:
                self.__write_ffmpeg_debug(debug_dir, cmd, proc, tag)
            return proc.returncode == 0
        except Exception as e:
            if debug_dir:
                fake = subprocess.CompletedProcess(
                    cmd, returncode=1, stdout='', stderr=str(e))
                self.__write_ffmpeg_debug(
                    debug_dir, cmd, fake, f'exception_{tag}')
            return False

    def render_text_clip_alpha(
        self,
        *,
        text: str,
        out_path: str,
        duration_s: float,
        dims: Dimensions,
        fps: str = "60000/1001",
        font_size: Optional[int] = None,
        font_name: str = "",
        font_file: str = "",
        y_pos: str = "(h-text_h)/2",
        box: bool = True,
        debug_dir: Optional[str] = None,
        style: Optional[Dict[str, Any]] = None
    ) -> bool:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

        W, H = int(dims.width), int(dims.height)
        duration_s = max(0.05, float(duration_s))

        # Estilo (defaults seguros)
        st = style or {}
        font_color = st.get("font_color", "#FFFFFF")
        border_color = st.get("border_color", "#000000")
        border_width = int(st.get("border_width", 4))
        shadow_x = int(st.get("shadow_x", 2))
        shadow_y = int(st.get("shadow_y", 2))
        shadow_color = st.get("shadow_color", "#000000")
        shadow_opacity = float(st.get("shadow_opacity", 0.55))
        box_enabled = st.get("box_enabled", box)
        box_color = st.get("box_color", "#000000")
        box_opacity = float(st.get("box_opacity", 0.35))
        caps_lock = st.get("caps_lock", False)
        animation = st.get("animation", "none")

        if caps_lock:
            text = text.upper()

        if font_size is None:
            font_size = max(28, int(H * 0.07))
        else:
            font_size = max(12, min(400, int(font_size)))

        max_chars = max(18, int(W / 75))
        wrapped = self.__wrap_text(text, max_chars=max_chars)
        wrapped = self.__sanitize_overlay_text(wrapped)

        txt_dir = os.path.join(os.path.dirname(out_path), "_txt")
        os.makedirs(txt_dir, exist_ok=True)
        txt_file = os.path.join(txt_dir, f"{self.__safe_slug(text)}.txt")
        clean = (wrapped or "").replace("\r", "")
        if not clean.endswith("\n"):
            clean += "\n"
        with open(txt_file, "wb") as f:
            f.write(clean.encode("utf-8"))

        # prioridade de fonte: arquivo -> nome -> auto
        chosen_fontfile = None
        try:
            if font_file and os.path.exists(font_file):
                chosen_fontfile = font_file
        except Exception:
            chosen_fontfile = None

        auto_fontfile = self.__find_fontfile()

        lines = [ln.strip()
                 for ln in (wrapped or "").split("\n") if ln.strip()]
        if not lines:
            lines = [""]

        line_spacing = 10
        line_h = int(font_size) + line_spacing
        block_h = int(font_size) * len(lines) + line_spacing * (len(lines) - 1)

        center_y = self.__estimate_y_center_px(y_pos, H)
        top_y = int(round(center_y - (block_h / 2)))
        if top_y < 0:
            top_y = 0
        if top_y > (H - int(font_size)):
            top_y = max(0, H - int(font_size))

        def _base_draw_opts():
            opts = []

            if chosen_fontfile:
                opts.append(
                    f"fontfile='{self.__ffmpeg_escape_drawtext_path(chosen_fontfile)}'")
            elif (font_name or "").strip():
                fn = (font_name or "").strip()
                opts.append(f"font='{fn}'")
            elif auto_fontfile:
                opts.append(
                    f"fontfile='{self.__ffmpeg_escape_drawtext_path(auto_fontfile)}'")
            else:
                opts.append("font='Arial'")

            opts += [
                "expansion=none",
                f"fontsize={int(font_size)}",
                f"fontcolor={font_color}@1.0",
                f"borderw={border_width}",
                f"bordercolor={border_color}@0.85",
                f"shadowx={shadow_x}",
                f"shadowy={shadow_y}",
                f"shadowcolor={shadow_color}@{shadow_opacity:.2f}",
                "x=(w-text_w)/2",
            ]

            if box_enabled:
                opts += [
                    "box=1",
                    f"boxcolor={box_color}@{box_opacity:.2f}",
                    "boxborderw=18",
                ]

            return opts

        filters = []
        for i, line in enumerate(lines):
            y_line = top_y + i * line_h
            esc = self.__ffmpeg_escape_drawtext_text_oneline(line)
            opts = _base_draw_opts() + [f"text='{esc}'", f"y={y_line}"]
            filters.append("drawtext=" + ":".join(opts))

        draw_filter = ",".join(filters)

        # Animação fade in/out (% da duração total)
        anim_in_pct = max(5, int(st.get("anim_in_pct", 10))) / 100.0
        anim_out_pct = max(5, int(st.get("anim_out_pct", 10))) / 100.0

        if animation == "fade":
            d_in = max(0.05, duration_s * anim_in_pct)
            d_out = max(0.05, duration_s * anim_out_pct)
            out_start = max(0.05, duration_s - d_out)
            draw_filter += f",fade=t=in:st=0:d={d_in:.3f}:alpha=1,fade=t=out:st={out_start:.3f}:d={d_out:.3f}:alpha=1"
        elif animation == "pop":
            d_in = max(0.05, duration_s * anim_in_pct * 0.5)  # pop entra mais rapido
            d_out = max(0.05, duration_s * anim_out_pct)
            out_start = max(0.05, duration_s - d_out)
            draw_filter += f",fade=t=in:st=0:d={d_in:.3f}:alpha=1,fade=t=out:st={out_start:.3f}:d={d_out:.3f}:alpha=1"

        size = f"{W}x{H}"
        base_input = f"color=c=black@0.0:s={size}:r={fps}:d={duration_s:.3f}"

        # PNG codec em MOV — melhor compatibilidade com Premiere no Windows
        cmd1 = [
            self.FFMPEG_BIN, "-y",
            "-f", "lavfi", "-i", base_input,
            "-vf", draw_filter,
            "-c:v", "png",
            "-pix_fmt", "rgba",
            "-t", f"{duration_s:.3f}",
            out_path
        ]
        ok = self.__run_ffmpeg(cmd1, debug_dir=debug_dir, tag="png_rgba")
        if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True

        # Fallback qtrle
        cmd2 = [
            self.FFMPEG_BIN, "-y",
            "-f", "lavfi", "-i", base_input,
            "-vf", draw_filter,
            "-c:v", "qtrle",
            "-pix_fmt", "argb",
            "-t", f"{duration_s:.3f}",
            out_path
        ]
        ok2 = self.__run_ffmpeg(cmd2, debug_dir=debug_dir, tag="qtrle_argb")
        return bool(ok2 and os.path.exists(out_path) and os.path.getsize(out_path) > 0)

    def render_overlays(
        self,
        selected_segments: List[Dict[str, Any]],
        *,
        mode: str,
        dims: Dimensions,
        fps: str,
        output_dir: str,
        position: str = "bottom",
        # NOVO
        font_name: str = "",
        font_file: str = "",
        font_size_px: Optional[int] = None,
        text_style: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        os.makedirs(output_dir, exist_ok=True)
        debug_dir = os.path.join(output_dir, "_ffmpeg_logs")
        os.makedirs(debug_dir, exist_ok=True)

        overlays: List[Dict[str, Any]] = []

        mode = (mode or "phrase").strip().lower()
        if mode not in ("phrase", "word"):
            mode = "phrase"

        position = (position or "bottom").strip().lower()
        if position == "top":
            y_pos = "(h*0.18)-text_h/2"
        elif position == "center":
            y_pos = "(h-text_h)/2"
        else:
            y_pos = "(h*0.78)-text_h/2"

        for seg_i, seg in enumerate(selected_segments):
            words: List[TranscriptionWord] = seg.get("words") or []
            if not words:
                continue

            if mode == "phrase":
                duration_s = max(
                    0.05, (seg["end_ms"] - seg["start_ms"]) / 1000.0)
                out_name = f"impact_{seg['start_ms']}_{seg_i}.mov"
                out_path = os.path.join(output_dir, out_name)

                self.render_text_clip_alpha(
                    text=seg.get("overlay_text", seg["text"]),
                    out_path=out_path,
                    duration_s=duration_s,
                    dims=dims,
                    fps=fps,
                    font_size=font_size_px,
                    font_name=font_name,
                    font_file=font_file,
                    y_pos=y_pos,
                    box=True,
                    debug_dir=debug_dir,
                    style=text_style,
                )

                overlays.append({
                    "path": out_path,
                    "start_ms": int(seg["start_ms"]),
                    "end_ms": int(seg["end_ms"]),
                    "text": seg.get("overlay_text", seg["text"])
                })
            else:
                for wi, w in enumerate(words):
                    w_text = (w.text or '').strip()
                    if not w_text:
                        continue
                    # Primeira letra maiúscula em cada palavra
                    w_text = w_text[0].upper() + w_text[1:] if len(w_text) > 1 else w_text.upper()

                    start_ms = int(w.start)
                    end_ms = int(w.end)
                    duration_s = max(0.05, (end_ms - start_ms) / 1000.0)

                    out_name = f"impact_{seg['start_ms']}_{seg_i}_w{wi}_{start_ms}.mov"
                    out_path = os.path.join(output_dir, out_name)

                    self.render_text_clip_alpha(
                        text=w_text,
                        out_path=out_path,
                        duration_s=duration_s,
                        dims=dims,
                        fps=fps,
                        font_size=font_size_px,
                        font_name=font_name,
                        font_file=font_file,
                        y_pos=y_pos,
                        box=True,
                        debug_dir=debug_dir,
                        style=text_style,
                    )

                    overlays.append({
                        "path": out_path,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "text": w_text
                    })

        overlays.sort(key=lambda o: (o["start_ms"], o["end_ms"]))
        return overlays

    # -----------------------------
    # API pública: do começo ao fim
    # -----------------------------
    def build_text_overlays(
        self,
        transcriptions: List[Transcription],
        offsets_seconds: List[float],
        *,
        dims: Dimensions,
        output_dir: str,
        mode: str = "phrase",
        max_phrases_total: int = 5,
        min_gap_seconds: float = 8.0,
        segment_gap_ms: int = 700,
        segment_min_words: int = 4,
        segment_max_words: int = 18,
        fps: str = "60000/1001",
        language: str = "pt-BR",
        position: str = "bottom",
        # NOVO
        font_name: str = "",
        font_file: str = "",
        font_size_px: Optional[int] = None,
        text_style: Optional[Dict[str, Any]] = None,
        use_cache: bool = False,
    ) -> Result[List[Dict[str, Any]]]:
        try:
            if len(transcriptions) != len(offsets_seconds):
                return Result(success=False, error='transcriptions e offsets_seconds precisam ter o mesmo tamanho.')

            # Caminho do cache de frases selecionadas
            cache_path = os.path.join(output_dir, "_impact_cache.json")

            # Tentar carregar cache se habilitado
            selected = None
            if use_cache and os.path.exists(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cached = json.load(f)
                    if isinstance(cached, list) and cached:
                        # Reconstituir words como TranscriptionWord para render_overlays
                        for seg in cached:
                            if "words" in seg:
                                seg["words"] = [
                                    TranscriptionWord(text=w["text"], start=w["start"], end=w["end"])
                                    for w in seg["words"]
                                ]
                        selected = cached
                        print(f"[impact] cache carregado: {len(selected)} frases de {cache_path}")
                except Exception as e:
                    print(f"[impact] cache invalido, recriando: {e}")

            if selected is None:
                all_words_abs: List[TranscriptionWord] = []
                for t, off_s in zip(transcriptions, offsets_seconds):
                    if t is None or not t.words:
                        continue
                    off_ms = int(float(off_s) * 1000)
                    for w in t.words:
                        if w is None or w.start is None or w.end is None:
                            continue
                        all_words_abs.append(TranscriptionWord(
                            text=w.text,
                            start=int(w.start) + off_ms,
                            end=int(w.end) + off_ms
                        ))

                all_words_abs.sort(key=lambda w: (w.start, w.end))

                effective_gap_ms = segment_gap_ms
                effective_min_words = 2 if mode == "word" else segment_min_words
                effective_max_words = max(segment_max_words, 30)

                has_punctuation = any(
                    self.__ends_sentence((w.text or ""))
                    for w in all_words_abs[:200]
                )
                if not has_punctuation:
                    print("[impact] AVISO: transcrição sem pontuação (cache antigo). "
                          "Apague o .json da transcrição para re-transcrever com pontuação.")

                segments = self.__build_segments_from_words(
                    all_words_abs,
                    gap_ms=effective_gap_ms,
                    min_words=effective_min_words,
                    max_words=effective_max_words
                )
                segments_before_filter = len(segments)
                segments = self.__filter_segments(segments)
                print(f"[impact] modo={mode} | {len(all_words_abs)} palavras -> {segments_before_filter} segmentos brutos -> {len(segments)} apos filtro")

                selected = self.select_impact_phrases(
                    segments,
                    max_phrases=max_phrases_total,
                    min_gap_seconds=min_gap_seconds,
                    language=language
                )
                print(f"[impact] {len(selected)} frases selecionadas de {len(segments)} candidatos")

                # Salvar cache (serializar words)
                try:
                    cache_data = []
                    for seg in selected:
                        seg_copy = dict(seg)
                        if "words" in seg_copy:
                            seg_copy["words"] = [
                                {"text": w.text, "start": w.start, "end": w.end}
                                for w in seg_copy["words"]
                            ]
                        cache_data.append(seg_copy)
                    os.makedirs(output_dir, exist_ok=True)
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(cache_data, f, indent=2, ensure_ascii=False)
                    print(f"[impact] cache salvo: {cache_path}")
                except Exception as e:
                    print(f"[impact] erro ao salvar cache: {e}")

            overlays_ms = self.render_overlays(
                selected,
                mode=mode,
                dims=dims,
                fps=fps,
                output_dir=output_dir,
                position=position,
                font_name=font_name,
                font_file=font_file,
                font_size_px=font_size_px,
                text_style=text_style,
            )

            overlays = []
            for o in overlays_ms:
                overlays.append({
                    "path": o["path"],
                    "start_seconds": float(o["start_ms"]) / 1000.0,
                    "end_seconds": float(o["end_ms"]) / 1000.0,
                    "text": o["text"]
                })

            return Result(success=True, data=overlays)
        except Exception as e:
            return Result(success=False, error=str(e))

    # -----------------------------
    # (Opcional) Inserção no Premiere
    # -----------------------------

    def insert_overlays_into_premiere(
        self,
        premiere_mgr,
        overlays: List[Dict[str, Any]],
        *,
        track_index: int = 3
    ) -> Result[None]:
        """
        Insere overlays na timeline do Premiere.

        EXTRA (novo):
        - Assim que cada clipe é inserido, aplica Opacity + Blend Mode (mesclagem).
        - A mesclagem é lida automaticamente do overlay.txt do roteiro (quando possível),
        reaproveitando o mesmo padrão do overlay principal.

        Por padrão usa V3 (track_index=3) porque no seu projeto:
        V0 = cenas
        V1 = logo
        V2 = overlay
        V3 = textos (seguro p/ não sobrescrever o overlay)
        """
        try:
            if not overlays:
                return Result(success=True)

            try:
                premiere_mgr._PremiereManager__ensure_video_track_index(
                    track_index)
            except Exception:
                pass

            import pymiere

            opacity = 100.0
            blend_mode = "Normal"  # Alpha real via ProRes 4444 — sem precisar de Screen

            cache = {}

            for o in overlays:
                media_path = o["path"]
                start_s = float(o["start_seconds"])

                project_item = premiere_mgr._PremiereManager__get_or_import_project_item(
                    media_path, cache
                )
                if project_item is None or project_item == getattr(premiere_mgr, "PYMIERE_UNDEFINED", None):
                    continue

                # use wrapper para gerar Time com ticks corretos (mais compatível com overwriteClip)
                start_time = pymiere.wrappers.time_from_seconds(start_s)

                track_item = premiere_mgr._PremiereManager__insert_clip_with_retry(
                    track_type="video",
                    track_index=track_index,
                    project_item=project_item,
                    start_time=start_time,
                    dedupe_last=False
                )

                # ✅ APLICA MESCLAGEM IMEDIATAMENTE (no clipe recém inserido)
                if track_item is not None and track_item != getattr(premiere_mgr, "PYMIERE_UNDEFINED", None):
                    try:
                        premiere_mgr._PremiereManager__set_opacity_and_blend(
                            track_item,
                            opacity=opacity,
                            blend_mode=blend_mode
                        )
                    except Exception:
                        pass

            return Result(success=True)
        except Exception as e:
            return Result(success=False, error=str(e))
