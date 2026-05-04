import json
import math
import os
import subprocess
import time
import random
import textwrap
import pymiere
import pymiere.wrappers
from concurrent.futures import ThreadPoolExecutor, as_completed
from ..entities import EXTENSIONS, Dimensions, Part, Result
from contextlib import contextmanager
from typing import Callable, Optional, Dict, Any
import os
import sys
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from .SettingsManager import get_runtime_root as _settings_runtime_root

# Flag para ocultar janela de console do FFmpeg no Windows
_FFMPEG_CREATE_FLAGS = 0x08000000 if sys.platform == 'win32' else 0
from ..utils import debug_print
from ..utils.debug_print import debug_print


# --- Pool/keep-alive p/ pymiere: força uma única Session do requests ---
def _patch_pymiere_keepalive():

    sess = requests.Session()
    sess.headers.update({"Connection": "keep-alive"})
    # 1 conexão no pool e sem retries automáticos (deixamos nosso retry cuidar)
    adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)

    # Troca qualquer 'requests' usado dentro de submódulos do pymiere por esta Session
    try:
        import pymiere as _pm
        # 1) Submódulos já importados
        for name, mod in list(sys.modules.items()):
            if name.startswith("pymiere") and hasattr(mod, "requests"):
                setattr(mod, "requests", sess)
        # 2) Guarda para debugging/uso futuro
        setattr(_pm, "_http_session", sess)
    except Exception as e:
        print("[keepalive] aviso ao patchar pymiere:", e)

    return sess


def get_runtime_root() -> str:
    # Mantém compatibilidade (se alguém importar daqui),
    # mas evita duplicar a lógica em vários arquivos.
    return _settings_runtime_root()


class PremiereManager():
    CWD = get_runtime_root()
    PYMIERE_UNDEFINED = 'undefined'
    PREMIERE_MOVEMENT_EFFECT_NAME = 'Movimento'
    PREMIERE_SCALE_PROPERTY_NAME = 'Escala'
    # ── Trilhas de audio ──
    # A1(idx 0) = audio cenas COERENTES (casadas com a fala)
    # A2(idx 1) = audio cenas NAO-COERENTES (filler / duplicacoes)
    # A3(idx 2) = narracao   A4(idx 3) = som CTA inscreva-se (linkado)
    # A5..A7  reservadas (overlay com som / logo com som / texto sem audio)
    # A8(idx 7) = musica
    SCENE_TRACK_INDEX = 0
    FILLER_SCENE_TRACK_INDEX = 1
    NARRATION_TRACK_INDEX = 2
    CTA_AUDIO_TRACK_INDEX = 3
    MUSIC_TRACK_INDEX = 7

    FRAME_W = 1920
    FRAME_H = 1080

    # ── Trilhas de video ──
    # V1(0)=Cenas COERENTES  V2(1)=Cenas NAO-COERENTES (filler/dup)
    # V3(2)=(vazio por design)  V4(3)=CTA  V5(4)=Overlay  V6(5)=Logo
    # V7(6)=Frases impactantes
    CTA_TRACK_INDEX = 3           # V4: Botao Inscreva-se
    OVERLAY_TRACK_INDEX = 4       # V5: Overlay
    LOGO_TRACK_INDEX = 5          # V6: Logo
    IMPACT_TEXT_TRACK_INDEX = 6   # V7: Frases impactantes / legendas
    LOGO_MARGIN_PX = 30           # margem do logo nas bordas
    # LOGO_TARGET_HEIGHT_PX = 120   # altura padrão do logo (ajuste se quiser)
    LOGO_FIXED_SCALE_PERCENT = 12.0  # escala fixa do logo (Motion > Scale)

    ULTRA_KEY_ENABLE = False              # ativa/desativa o Ultra Key
    ULTRA_KEY_COLOR_HEX = "#000000"      # cor a ser "chaveada"

    BLEND_SCREEN_ENUM = 22  # ajuste para o valor do modo de mesclagem desejado
    # 7 = exclusão | 2 = Sobexposição de cor | 3 = Escurecer | 4 = cor mais escura | 5 = Diferença | 6 = Dissolver | 8 = Luz intensa | 9 =
    # 10 = matriz | 11 = clarear | 21 = saturação | 22 = tela | 23 = luz suave
    # === DEBUG do modo de mesclagem (overlay) === Aqui ele fica mudando o modo de mesclagem no overlay ai você deve ver qual numero representa oq vc quer usar
    BLEND_DEBUG_SWEEP_ENABLE = False      # mude para True para rodar o teste
    BLEND_DEBUG_RANGE = (1, 30)            # início e fim (inclusive)
    BLEND_DEBUG_WAIT_SECONDS = 5.0        # segundos de espera por número

    # ====== Resiliência p/ chamadas pymiere (timeouts, ECONNRESET etc.) ======
    MAX_RETRIES = 4
    RETRY_BACKOFF_BASE = 0.3
    RETRY_BACKOFF_CAP = 2.5
    RETRY_JITTER = (0.0, 0.2)
    HEARTBEAT_EVERY = 10000
    AUTOSAVE_EVERY_OPS = 10000
    # ~4 ms entre chamadas: seguro com keep-alive ativo
    REQUEST_THROTTLE_SECONDS = 0.004
    # ⛔ REMOVIDO: HARD_RESET_EVERY_CALLS / HARD_RESET_SLEEP_SECONDS

    _ops_since_save = 0
    _calls_counter = 0

    def __init__(self, frame_size: tuple[int, int] = (1920, 1080)):
        # ⇩⇩ NOVO: guarda a resolução escolhida
        try:
            self.FRAME_W, self.FRAME_H = int(frame_size[0]), int(frame_size[1])
        except Exception:
            self.FRAME_W, self.FRAME_H = 1920, 1080

        # contadores...
        self._ops_since_save = 0
        self._calls_counter = 0
        # 1) Força keep-alive...
        try:
            self._session = _patch_pymiere_keepalive()
            print("[keepalive] requests.Session ativa para pymiere.")
        except Exception as e:
            print("[keepalive] falhou ao inicializar:", e)
            self._session = None

    # ⇩⇩ NOVO MÉTODO
    def set_frame_size(self, w: int, h: int):
        """Atualiza a resolução alvo (usado pela UI antes de montar)."""
        try:
            self.FRAME_W, self.FRAME_H = int(w), int(h)
        except Exception:
            pass

    def __is_transient(self, e: Exception) -> bool:
        msg = (str(e) or '').lower()
        tname = type(e).__name__.lower()
        hints = (
            'econnreset', 'connection reset', 'timed out', 'timeout',
            'econnrefused', 'connection refused',
            'failed to establish', 'max retries exceeded',
            'bad gateway', '503', 'socket', 'soquete', 'broken pipe',
            'eaddrinuse', 'address already in use', 'winerror 10048'
        )
        return isinstance(e, (TimeoutError, OSError)) \
            or 'request' in tname or 'connectionerror' in tname \
            or any(h in msg for h in hints)

    def __backoff(self, attempt: int):
        # exponencial com teto + jitter
        base = min(self.RETRY_BACKOFF_CAP,
                   self.RETRY_BACKOFF_BASE * (2 ** attempt))
        time.sleep(base + random.uniform(*self.RETRY_JITTER))

    def __heartbeat(self) -> bool:
        try:
            _ = pymiere.objects.app.isDocumentOpen()
            return True
        except Exception:
            return False

    # ⬇️ COLE ESTE MÉTODO AQUI
    def __wait_panel_ready(self, timeout: float = 20.0, tick: float = 0.3) -> bool:
        """
        Aguarda o painel do Premiere (porta 3000) voltar a responder.
        Retorna True se conseguir antes do timeout.
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                _ = pymiere.objects.app.isDocumentOpen()
                return True
            except Exception:
                time.sleep(tick)
        return False
    # ⬆️ FIM

    def __classify_err(self, e: Exception) -> str:
        msg = (str(e) or '').lower()
        if ('10048' in msg) or ('eaddrinuse' in msg) or ('address already in use' in msg):
            return 'ADDR_IN_USE'
        if ('timed out' in msg) or ('timeout' in msg):
            return 'TIMEOUT'
        if ('econnrefused' in msg) or ('connection refused' in msg):
            return 'REFUSED'
        if ('econnreset' in msg) or ('connection reset' in msg) or ('broken pipe' in msg):
            return 'RESET'
        return 'UNKNOWN'

    def __soft_reset_connection(self) -> bool:
        """
        Reinício suave da ponte com o painel (porta 3000):
        - zera contadores locais,
        - espera RESET_SLEEP_SECONDS,
        - tenta "pingar" o app,
        - como último recurso, recarrega o módulo pymiere.
        Retorna True se, ao final, o painel estiver respondendo.
        """
        try:
            # zera contadores locais que poderiam enfileirar saves/heartbeats
            self._ops_since_save = 0

            # 1) tenta apenas "pingar" o painel
            try:
                _ = pymiere.objects.app.isDocumentOpen()
                return True
            except Exception:
                pass

            # 2) último recurso: recarregar o módulo pymiere (derruba sessões internas)
            try:
                import importlib
                import gc
                gc.collect()
                importlib.reload(pymiere)
                # "acorda" objetos
                _ = pymiere.objects.app.isDocumentOpen()
                return True
            except Exception:
                return False

        except Exception:
            return False

    def __retry(self, fn: Callable, desc: str = 'call'):
        last_exc = None
        for i in range(self.MAX_RETRIES):
            try:
                # contador só para batimento cardíaco e métricas
                self._calls_counter += 1
                if self._calls_counter % self.HEARTBEAT_EVERY == 0:
                    self.__heartbeat()

                # chamada alvo
                out = fn()

                # autosave eventual
                self._ops_since_save += 1
                if self._ops_since_save >= self.AUTOSAVE_EVERY_OPS:
                    try:
                        pymiere.objects.app.project.save()
                    except Exception:
                        pass
                    self._ops_since_save = 0

                return out

            except Exception as e:
                last_exc = e

                # erros transitórios: tenta reconectar e reexecutar com backoff
                if self.__is_transient(e):
                    kind = self.__classify_err(e)
                    print(
                        f"[retry:{desc}] {kind}: {e} (tentativa {i+1}/{self.MAX_RETRIES})")

                    # reconexão suave apenas quando faz sentido
                    if kind in ('ADDR_IN_USE', 'RESET', 'REFUSED', 'TIMEOUT'):
                        try:
                            self.__soft_reset_connection()
                        except Exception:
                            pass
                        self.__wait_panel_ready(timeout=12.0)
                        time.sleep(0.2)  # pequeno respiro

                    self.__backoff(i)
                    continue  # próxima tentativa

                # erro não transitório: dá 1 chance extra e sai
                if i >= 1:
                    break
                self.__backoff(i)

        # estourou as tentativas
        raise last_exc

    # ---------- Wrappers seguros (1:1) ----------

    def __find_item_with_retry(self, media_path: str):
        def _do():
            items = pymiere.objects.app.project.rootItem \
                .findItemsMatchingMediaPath(media_path, ignoreSubclips=False)
            return items[0] if items else self.PYMIERE_UNDEFINED
        return self.__retry(_do, desc=f'find {media_path}')

    def __import_files_with_retry(self, paths: list[str]):
        return self.__retry(
            lambda: pymiere.objects.app.project.importFiles(
                paths, True, pymiere.objects.app.project.getInsertionBin(), False
            ),
            desc='importFiles'
        )

    def __insert_clip_with_retry(
        self, *, track_type, track_index, project_item, start_time,
        dedupe_last: bool = False, cached_track=None
    ):
        def _get_track():
            if cached_track is not None:
                return cached_track
            return (pymiere.objects.app.project.activeSequence.videoTracks[track_index]
                    if track_type == 'video'
                    else pymiere.objects.app.project.activeSequence.audioTracks[track_index])

        def _maybe_existing_last(track):
            if not dedupe_last:
                return None
            try:
                # Usa len + indice ao inves de list() para evitar O(N) IPC
                n = len(track.clips)
                if n == 0:
                    return None
                s_target = float(getattr(start_time, 'seconds', 0.0))
                # Checa apenas os ultimos 2 clips (indice direto, sem list)
                for idx in range(max(0, n - 2), n):
                    c = track.clips[idx]
                    s = float(getattr(c.start, 'seconds', 0.0))
                    if abs(s - s_target) <= 0.08:
                        pi = getattr(c, 'projectItem', None)
                        if pi and getattr(pi, 'name', None) == getattr(project_item, 'name', None):
                            return c
            except Exception:
                pass
            return None

        def _best_match_clip(track):
            """Acha o clipe mais provável que acabou de entrar (por start_time e nome)."""
            try:
                s_target = float(getattr(start_time, 'seconds', 0.0))
                target_name = getattr(project_item, 'name', None)
                best = None
                best_d = 1e9

                for c in list(track.clips):
                    try:
                        s = float(getattr(c.start, 'seconds', 0.0))
                        d = abs(s - s_target)

                        # se der pra comparar pelo nome do ProjectItem, melhor ainda
                        if target_name:
                            pi = getattr(c, 'projectItem', None)
                            if pi and getattr(pi, 'name', None) != target_name:
                                continue

                        if d < best_d:
                            best = c
                            best_d = d
                    except Exception:
                        continue

                # tolerância maior porque o Premiere pode arredondar pra frame/timebase
                if best is not None and best_d <= 0.25:
                    return best
            except Exception:
                pass
            return None

        def _do():
            t = _get_track()

            ex = _maybe_existing_last(t)
            if ex:
                return ex

            # Preferir OVERWRITE para não "empurrar" o que já existe na timeline
            if hasattr(t, 'overwriteClip'):
                t.overwriteClip(project_item, start_time)
            else:
                t.insertClip(project_item, start_time)

            # Pega o último clipe pelo índice: O(2) IPC (len + index) em vez de
            # O(N) do scan anterior — elimina a degradação quadrática na timeline.
            try:
                n = len(t.clips)
                if n > 0:
                    return t.clips[n - 1]
            except Exception:
                pass

            # Fallback: scan completo (lento, raramente acionado)
            m = _best_match_clip(t)
            return m

        return self.__retry(_do, desc=f'insert/overwrite:{track_type}[{track_index}]')

    def __qe_razor_with_retry(self, *, track_type: str, track_index: int, timecode):
        return self.__retry(
            lambda: (
                pymiere.objects.qe.project.getActiveSequence()
                .getVideoTrackAt(track_index).razor(timecode)
            ) if track_type == 'video' else (
                pymiere.objects.qe.project.getActiveSequence()
                .getAudioTrackAt(track_index).razor(timecode)
            ),
            desc=f'razor:{track_type}[{track_index}]'
        )

    def __set_speed_with_retry(self, track_item, speed_percent: float) -> bool:
        # tenta API normal com retry
        try:
            self.__retry(lambda: track_item.setSpeed(
                float(speed_percent), False, True), desc='setSpeed')
            return True
        except Exception:
            # fallbacks QE (mantém sua ideia original)
            # TODO(robustez V1/V2): hoje so e chamado de mount_mass_project
            # (que insere todas as cenas em V1). Se for usado para clipes em
            # V2 (FILLER) no futuro, este fallback pega o ultimo clip de V1
            # (errado). Receber track_index como parametro quando ampliar.
            try:
                qe_seq = pymiere.objects.qe.project.getActiveSequence()
                qe_v = qe_seq.getVideoTrackAt(self.SCENE_TRACK_INDEX)
                qe_item = qe_v.getItemAt(qe_v.numItems - 1)
                try:
                    self.__retry(lambda: qe_item.setSpeed(
                        float(speed_percent), False, True), desc='qe.setSpeed')
                    return True
                except Exception:
                    self.__retry(lambda: qe_item.changeSpeed(
                        float(speed_percent), False, True), desc='qe.changeSpeed')
                    return True
            except Exception:
                return False

    def get_status(self):
        debug_print('Premiere', 'Verificando status (get_status)')
        try:
            is_project_open = pymiere.objects.app.isDocumentOpen()
            status = 'READY' if is_project_open else 'PROJECT_NOT_OPEN'
            debug_print('Premiere', 'Status do Premiere', status=status)
            return status
        except Exception as error:
            if isinstance(error, OSError):
                return 'PLUGIN_NOT_INSTALLED'

            return 'PREMIERE_NOT_OPEN'

    def ensure_sequence(self, script_name: str):
        # delega para o submódulo editing (evita imports no topo)
        from .premiere import editing
        return editing.ensure_sequence(self, script_name)

    def get_files_paths(self, script_name: str, music_style: str) -> Result[dict[str, str | list[str]]]:
        # delega para o submódulo media
        from .premiere import media
        return media.get_files_paths(self, script_name, music_style)

    def import_files(self, files_paths: list[str]) -> dict[str, bool]:
        # delega para o submódulo media
        from .premiere import media
        return media.import_files(self, files_paths)

    def mount_sequence(
        self,
        narrations_files: list[str],
        narration_base_path: str,
        scenes_base_path: str,
        musics_files: list[str],
        musics_base_path: str,
        paths_map: dict[str, str],
        narrations_map: dict[str, list[Part]],
        zoom_min_scale_multiplier,
        zoom_max_scale_multiplier,
        fade_percentage: float,
        apply_fade_immediately: bool = False,
        duplicate_scenes_until_next: bool = False,
        fill_gaps_with_random_scenes: bool = True,
        max_fill_scene_duration: float = 12.0,

        narrations_transcriptions: Optional[list[Any]] = None,
        impact_phrases_config: Optional[dict] = None,
        openai_api_key: str = '',

        # Recursos visuais
        logo_path: str = '',
        logo_position: str = 'bottom_right',
        overlay_path: str = '',
        cta_enabled: bool = False,
        cta_anim_path: str = '',
        cta_chroma_key: bool = True,

        # Mixer (dB)
        vol_scene_db: float = 0.0,
        vol_narration_db: float = 0.0,
        vol_cta_db: float = -9.0,
        vol_music_db: float = -12.0,
    ) -> Result[None]:
        from .premiere import editing
        return editing.mount_sequence(
            self,
            narrations_files,
            narration_base_path,
            scenes_base_path,
            musics_files,
            musics_base_path,
            paths_map,
            narrations_map,
            zoom_min_scale_multiplier,
            zoom_max_scale_multiplier,
            fade_percentage,
            apply_fade_immediately=apply_fade_immediately,
            duplicate_scenes_until_next=duplicate_scenes_until_next,
            fill_gaps_with_random_scenes=fill_gaps_with_random_scenes,
            max_fill_scene_duration=max_fill_scene_duration,

            narrations_transcriptions=narrations_transcriptions,
            impact_phrases_config=impact_phrases_config,
            openai_api_key=openai_api_key,

            logo_path=logo_path,
            logo_position=logo_position,
            overlay_path=overlay_path,
            cta_enabled=cta_enabled,
            cta_anim_path=cta_anim_path,
            cta_chroma_key=cta_chroma_key,

            vol_scene_db=vol_scene_db,
            vol_narration_db=vol_narration_db,
            vol_cta_db=vol_cta_db,
            vol_music_db=vol_music_db,
        )

    @contextmanager
    def fast_ops(self):
        orig_throttle = self.REQUEST_THROTTLE_SECONDS
        orig_hb = self.HEARTBEAT_EVERY
        orig_save = self.AUTOSAVE_EVERY_OPS
        try:
            # 4 ms em lote (evita pico de TIME_WAIT)
            self.REQUEST_THROTTLE_SECONDS = 0.004
            self.HEARTBEAT_EVERY = 10**9
            self.AUTOSAVE_EVERY_OPS = 10**9
            yield
        finally:
            self.REQUEST_THROTTLE_SECONDS = orig_throttle
            self.HEARTBEAT_EVERY = orig_hb
            self.AUTOSAVE_EVERY_OPS = orig_save

    def mount_mass_project(
        self,
        mass_structure: dict,
        musics_files: list[str],
        musics_base_path: str,
        paths_map: dict[str, str],
        zoom_min_scale_multiplier: float,
        zoom_max_scale_multiplier: float,
        order_mode: str = 'asc',
        min_scene_seconds: int = 5,
        max_scene_seconds: int = 7,
        titlecard_seconds: Optional[float] = 3.0,
        fade_percentage: float = 10.0,
        apply_fade_immediately: bool = False
    ) -> Result[None]:
        from .premiere import editing
        return editing.mount_mass_project(
            self,
            mass_structure,
            musics_files,
            musics_base_path,
            paths_map,
            zoom_min_scale_multiplier,
            zoom_max_scale_multiplier,
            order_mode,
            min_scene_seconds,
            max_scene_seconds,
            titlecard_seconds,
            fade_percentage,
            apply_fade_immediately=apply_fade_immediately
        )

    def __get_active_sequence_fps_or(self, fallback: int = 25) -> int:
        return int(fallback)

    def __render_logo_minute_plate_mp4(self, logo_path: str, duration_s: float = 60.0) -> Optional[str]:
        """
        Gera um MP4 (sem alpha) com o logo já no canto (escala fixa) sobre base preta,
        com a duração desejada. Retorna o caminho do arquivo.
        """
        try:
            out_dir = os.path.dirname(logo_path)
            out_path = os.path.join(out_dir, 'logo_60s.mp4')

            # Se já existe e tem tamanho > 0, reaproveita
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path

            ffmpeg = self.__get_ffmpeg_bin()
            ext = os.path.splitext(logo_path)[1].lower()
            is_image = ext in EXTENSIONS['IMAGE']

            if is_image:
                src_args = ['-loop', '1', '-t',
                            f'{duration_s:.3f}', '-i', logo_path]
            else:
                src_args = ['-stream_loop', '-1', '-i',
                            logo_path, '-t', f'{duration_s:.3f}']

            size = f"{self.FRAME_W}x{self.FRAME_H}"
            base = ['-f', 'lavfi', '-t',
                    f'{duration_s:.3f}', '-i', f'color=c=black:s={size}']

            margin = int(self.LOGO_MARGIN_PX)
            f = float(getattr(self, 'LOGO_FIXED_SCALE_PERCENT', 12.0)) / 100.0
            fc = (
                f"[0:v]scale=iw*{f}:ih*{f}:flags=bicubic,format=rgba[lg];"
                f"[1:v][lg]overlay=x={margin}:y=main_h-overlay_h-{margin}:format=auto"
            )

            fps = self.__get_active_sequence_fps_or(25)

            cmd = [
                ffmpeg, '-y',
                *src_args,
                *base,
                '-filter_complex', fc,
                '-r', str(fps),
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-preset', 'medium', '-crf', '18',
                '-movflags', '+faststart',
                '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                '-an',
                out_path
            ]
            print(f"[logo60] ffmpeg start (mp4, {fps}fps) -> {out_path}")
            proc = subprocess.run(
                cmd, check=False, capture_output=True, text=True, creationflags=_FFMPEG_CREATE_FLAGS)
            self.__write_ffmpeg_debug(
                out_dir, cmd, proc, f'logo_60s_mp4_{fps}fps')
            if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
        except Exception as e:
            print(f"[logo60-mp4] render error: {e}")

        return None

    def __set_motion(self, clip, *, scale: Optional[float] = None, position: Optional[tuple[float, float]] = None):
        """Define Movimento (Escala/Posição) com defensivas anti-32767 e retries."""
        try:
            # localizar componente "Movimento/Motion"
            motion = None
            for comp in clip.components:
                dn = (getattr(comp, 'displayName', '') or '').lower()
                if dn in ('movimento', 'motion') or 'motion' in dn or 'movim' in dn:
                    motion = comp
                    break
            if motion is None:
                return

            def _find_prop_contains(subs: tuple[str, ...]):
                for p in motion.properties:
                    dn = (getattr(p, 'displayName', '') or '').lower()
                    # tolera "posição", "posicao", "position", etc.
                    dn_norm = dn.replace('ã', 'a').replace('ç', 'c').replace('õ', 'o').replace(
                        'á', 'a').replace('ó', 'o').replace('í', 'i').replace('é', 'e')
                    for s in subs:
                        s_norm = s.replace('ã', 'a').replace('ç', 'c').replace('õ', 'o').replace(
                            'á', 'a').replace('ó', 'o').replace('í', 'i').replace('é', 'e')
                        if s_norm in dn_norm:
                            return p
                return None

            # --- ESCALA ---
            if scale is not None:
                p_scale = _find_prop_contains(('escala', 'scale'))
                if p_scale is not None:
                    try:
                        if p_scale.isTimeVarying():
                            t0 = clip.inPoint.seconds
                            t1 = clip.outPoint.seconds
                            try:
                                p_scale.addKey(t0)
                            except:
                                pass
                            p_scale.setValueAtKey(t0, float(scale), True)
                            try:
                                p_scale.addKey(t1)
                            except:
                                pass
                            p_scale.setValueAtKey(t1, float(scale), True)
                        else:
                            p_scale.setValue(float(scale), True)
                    except Exception:
                        pass

            # --- POSIÇÃO ---
            if position is None:
                return
            x, y = float(position[0]), float(position[1])
            p_pos = _find_prop_contains(
                ('posicao', 'posição', 'position', 'posic'))
            if p_pos is None:
                return

            def _ok(val):
                if isinstance(val, (list, tuple)) and len(val) >= 2:
                    return max(abs(val[0]), abs(val[1])) < 10000
                return False

            # Passo 1: manda pro centro (limpa 32767)
            try:
                center_x = float(self.FRAME_W) / 2.0
                center_y = float(self.FRAME_H) / 2.0
                if p_pos.isTimeVarying():
                    # garante 2 keyframes fixos
                    t0 = clip.inPoint.seconds
                    t1 = clip.outPoint.seconds
                    for t in (t0, t0 + 0.001):
                        try:
                            p_pos.addKey(t)
                        except:
                            pass
                        p_pos.setValueAtKey(
                            t, [center_x, center_y], True)  # <-- DINÂMICO
                else:
                    p_pos.setValue([center_x, center_y], True)
                time.sleep(0.01)
            except Exception:
                pass

            # Passo 2: seta posição destino
            ok = False
            try:
                if p_pos.isTimeVarying():
                    t0 = clip.inPoint.seconds
                    t1 = clip.outPoint.seconds
                    for t in (t0, t1):
                        try:
                            p_pos.addKey(t)
                        except:
                            pass
                        p_pos.setValueAtKey(t, [x, y], True)
                else:
                    p_pos.setValue([x, y], True)
                v = p_pos.getValue()
                ok = _ok(v)
            except Exception:
                ok = False

            # Passo 3: clamp se necessário
            if not ok:
                FRAME_W, FRAME_H = float(self.FRAME_W), float(self.FRAME_H)
                cx = max(0.0, min(FRAME_W, x))
                cy = max(0.0, min(FRAME_H, y))
                try:
                    p_pos.setValue([cx, cy], True)
                except Exception:
                    pass
        except Exception:
            pass

    def __disable_scale_to_frame_size(self, clip) -> bool:
        """
        Desativa/neutraliza 'Dimensionar para o tamanho do quadro' no clipe.
        Algumas builds expõem setScaleToFrameSize(False); tentamos variações.
        Se a API não expor, seguimos (o próximo passo força Motion > Scale).
        """
        # tentativa direta (builds modernas)
        try:
            if hasattr(clip, 'setScaleToFrameSize'):
                for val in (False, 0, 'false'):
                    try:
                        clip.setScaleToFrameSize(val)
                        return True
                    except Exception:
                        pass
        except Exception:
            pass

        # outras variantes vistas em builds antigas
        try:
            if hasattr(clip, 'scaleToFrameSize'):
                try:
                    setattr(clip, 'scaleToFrameSize', False)
                    return True
                except Exception:
                    pass
        except Exception:
            pass

        # fallback: seguimos adiante; a próxima etapa (Motion > Scale) resolve na prática
        return False

    # def __normalize_clip_scale(self, clip, scene_dimensions: Dimensions):
    #     """
    #     Reproduz o seu clique manual: neutraliza a flag de dimensionar,
    #     centraliza e aplica SCALE em modo 'cover' p/ eliminar barras.
    #     """
    #     try:
    #         self.__disable_scale_to_frame_size(clip)
    #     except Exception:
    #         pass

    #     # centraliza e aplica COVER
    #     try:
    #         cx = float(self.FRAME_W) / 2.0
    #         cy = float(self.FRAME_H) / 2.0
    #         cover = float(self.__get_new_initial_scale(scene_dimensions))
    #         self.__set_motion(clip, scale=cover, position=(cx, cy))
    #     except Exception:
    #         pass

    def __set_opacity_and_blend(self, clip, *, opacity: Optional[float] = None, blend_mode: Optional[str] = None):
        """
        Define Opacidade (%) e Modo de Mesclagem. Tenta por rótulo (PT/EN)
        e cai para enum numérico (Screen=self.BLEND_SCREEN_ENUM, padrão 22) se necessário.
        """
        try:
            for comp in clip.components:
                cname = (getattr(comp, 'displayName', '') or '').lower()
                if cname not in ('opacidade', 'opacity'):
                    continue

                # Opacidade
                if opacity is not None:
                    for prop in comp.properties:
                        pn = (getattr(prop, 'displayName', '') or '').lower()
                        if pn in ('opacidade', 'opacity'):
                            try:
                                prop.setValue(float(opacity), True)
                            except Exception:
                                pass
                            break

                # Blend mode
                blend_prop = None
                for prop in comp.properties:
                    pn = (getattr(prop, 'displayName', '') or '').lower()
                    if ('blend' in pn) or ('mescl' in pn) or ('mescla' in pn) or ('modo de mesclagem' in pn):
                        blend_prop = prop
                        break
                if not blend_prop:
                    continue

                # 1) por rótulo
                want = (blend_mode or '').strip()
                labels = [want] if want else []
                labels += ['Tela', 'Screen']  # fallback PT/EN
                ok = False
                for label in labels:
                    if not label:
                        continue
                    try:
                        blend_prop.setValue(label, True)
                        ok = True
                        break
                    except Exception:
                        pass

                # 2) fallback numérico: Screen
                if not ok:
                    screen_id = getattr(self, 'BLEND_SCREEN_ENUM', 8)
                    for v in (screen_id, str(screen_id)):
                        try:
                            blend_prop.setValue(v, True)
                            ok = True
                            break
                        except Exception:
                            pass

        except Exception:
            pass

    def __apply_overlay_settings(self, clip, roteiro_name: str, overlay_cover_scale: float | None):
        """Aplica neutralização de Scale-to-Frame, escala COVER, Ultra Key e Opacidade/Blend no clipe do overlay."""
        # 1) Neutraliza Scale-to-Frame
        try:
            self.__disable_scale_to_frame_size(clip)
        except Exception:
            pass

        # 2) Dimensionamento (cover) – elimina barras pretas
        if overlay_cover_scale is not None:
            try:
                self.__set_motion(clip, scale=float(overlay_cover_scale))
            except Exception:
                pass

        # 3) Ultra Key (opcional)
        if getattr(self, 'ULTRA_KEY_ENABLE', False):
            try:
                self.__apply_ultra_key(clip, key_hex=getattr(
                    self, 'ULTRA_KEY_COLOR_HEX', '#000000'))
            except Exception:
                pass

        # 4) Opacidade e Modo de Mesclagem (lidos do overlay.txt)
        try:
            opacity, blend = self.__parse_overlay_cfg(roteiro_name)
            self.__set_opacity_and_blend(
                clip, opacity=opacity, blend_mode=blend)
        except Exception:
            pass

        # 5) (Opcional) Varredura de debug de blend
        try:
            if getattr(self, 'BLEND_DEBUG_SWEEP_ENABLE', False):
                self.__debug_cycle_blend_modes(
                    clip,
                    start=self.BLEND_DEBUG_RANGE[0],
                    end=self.BLEND_DEBUG_RANGE[1],
                    wait_seconds=self.BLEND_DEBUG_WAIT_SECONDS
                )
        except Exception:
            pass

    def __debug_cycle_blend_modes(self, clip, start: int = 1, end: int = 6, wait_seconds: float = 3.0):
        """
        Varre os valores numéricos do 'Modo de Mesclagem' no clipe:
        tenta setar  start..end , aguardando wait_seconds entre cada.
        Imprime no terminal o número tentado e o valor atual da propriedade.
        """
        try:
            # localizar a propriedade de 'Modo de Mesclagem' dentro do componente Opacidade
            blend_prop = None
            for comp in clip.components:
                cname = (getattr(comp, 'displayName', '') or '').lower()
                if cname not in ('opacidade', 'opacity'):
                    continue
                for prop in comp.properties:
                    pn = (getattr(prop, 'displayName', '') or '').lower()
                    if ('blend' in pn) or ('mescl' in pn) or ('mescla' in pn) or ('modo de mesclagem' in pn):
                        blend_prop = prop
                        break
                if blend_prop:
                    break

            if not blend_prop:
                print(
                    '[blend-sweep] propriedade de modo de mesclagem não encontrada.')
                return

            try:
                original = blend_prop.getValue()
            except Exception:
                original = None

            s = int(start)
            e = int(end)
            for n in range(s, e + 1):
                ok = False
                # algumas builds aceitam int, outras string
                for v in (n, str(n)):
                    try:
                        blend_prop.setValue(v, True)
                        ok = True
                        break
                    except Exception:
                        pass
                try:
                    cur = blend_prop.getValue()
                except Exception:
                    cur = None
                print(f'[blend-sweep] tentei {n} -> valor agora: {cur}')
                time.sleep(max(0.0, float(wait_seconds)))

            # NÃO restauro de propósito (você vê o último aplicado)
            # Se quiser restaurar ao final, descomente abaixo.
            # try:
            #     if original is not None:
            #         blend_prop.setValue(original, True)
            # except Exception:
            #     pass

        except Exception as e:
            print(f'[blend-sweep] erro: {e}')

    def __hex_to_rgb(self, s: str) -> tuple[int, int, int]:
        """Converte '#RRGGBB' / '0xRRGGBB' / 'RRGGBB' em (r,g,b) 0-255."""
        s = (s or '').strip().lower()
        if s.startswith('#'):
            s = s[1:]
        if s.startswith('0x'):
            s = s[2:]
        if len(s) != 6:
            return (0, 255, 0)  # default: verde
        try:
            r = int(s[0:2], 16)
            g = int(s[2:4], 16)
            b = int(s[4:6], 16)
            return (r, g, b)
        except Exception:
            return (0, 255, 0)

    def __apply_ultra_key(self, clip, key_hex: str = "#000000") -> bool:
        """
        Garante o efeito 'Ultra Key' no clipe e define a cor-chave.
        Tenta tanto por nome em PT/EN quanto por matchName (ADBE UltraKey).
        """
        try:
            # 1) já existe?
            uk = None
            for comp in clip.components:
                dn = (getattr(comp, 'displayName', '') or '').lower()
                # cobre PT/EN
                if 'ultra' in dn and 'key' in dn:
                    uk = comp
                    break
                if 'chave' in dn and 'ultra' in dn:
                    uk = comp
                    break

            # 2) se nao existe, tenta adicionar por nome/matchName e re-checar
            if uk is None:
                added = False
                # Nomes possiveis: matchName (ADBE), EN, PT-BR
                effect_names = (
                    'ADBE UltraKey',
                    'AE.ADBE Ultra Key',
                    'Ultra Key',
                    'Chave ultra',
                    'Chave Ultra',
                )
                for name in effect_names:
                    try:
                        clip.addVideoEffect(name)
                        added = True
                        print(f"[ultrakey] adicionado com nome: {name}")
                        break
                    except Exception:
                        continue

                # fallback: tenta via QE (aceita matchName direto)
                if not added:
                    try:
                        qe_seq = pymiere.objects.qe.project.getActiveSequence()
                        # busca o clip no QE pela posicao
                        # TODO(limpeza): atributo `_track_index` nao e setado em
                        # nenhum lugar do codigo (verificado via grep). Sempre
                        # cai no fallback 0 (V1). Codigo possivelmente legado;
                        # avaliar se esta funcao ainda e usada antes de remover.
                        qe_track = qe_seq.getVideoTrackAt(
                            getattr(clip, '_track_index', 0) if hasattr(clip, '_track_index') else 0)
                        if qe_track:
                            for ci in range(qe_track.numItems):
                                qe_clip = qe_track.getItemAt(ci)
                                if qe_clip and hasattr(qe_clip, 'addVideoEffect'):
                                    try:
                                        qe_clip.addVideoEffect(
                                            pymiere.objects.qe.project.getVideoEffectByName('Ultra Key'))
                                        added = True
                                        print("[ultrakey] adicionado via QE")
                                        break
                                    except Exception:
                                        pass
                    except Exception:
                        pass

                # procura de novo com polling
                if added:
                    for _ in range(30):
                        self.__throttle()
                        time.sleep(0.05)
                        for comp in clip.components:
                            dn = (getattr(comp, 'displayName', '') or '').lower()
                            if ('ultra' in dn and 'key' in dn) or ('chave' in dn and 'ultra' in dn):
                                uk = comp
                                break
                        if uk is not None:
                            break

            if uk is None:
                # Lista todos os efeitos do clip para debug
                try:
                    comp_names = [getattr(c, 'displayName', '?') for c in clip.components]
                    print(f"[ultrakey] componentes do clip: {comp_names}")
                except Exception:
                    pass
                print("[ultrakey] nao foi possivel adicionar/encontrar o Ultra Key.")
                return False

            # 3) define a cor-chave
            r, g, b = self.__hex_to_rgb(key_hex)
            # tenta propriedades comuns: "Key Color" / "Cor-chave"
            prop = None
            for p in uk.properties:
                pn = (getattr(p, 'displayName', '') or '').lower()
                pn_norm = pn.replace('ã', 'a').replace(
                    'ç', 'c').replace('é', 'e').replace('í', 'i')
                if ('key' in pn_norm and 'color' in pn_norm) or ('cor' in pn_norm and 'chave' in pn_norm):
                    prop = p
                    break

            if prop is not None:
                # Alguns builds aceitam setColorValue(r,g,b) em 0..1; outros, setValue([r,g,b])
                try:
                    # 0..1
                    if hasattr(prop, 'setColorValue'):
                        prop.setColorValue(r/255.0, g/255.0, b/255.0)
                    else:
                        raise Exception("no setColorValue")
                except Exception:
                    try:
                        # 0..255
                        prop.setValue([r, g, b], True)
                    except Exception:
                        # 0..1 como fallback
                        prop.setValue([r/255.0, g/255.0, b/255.0], True)

            # 4) Transparência = 100
            try:
                for p in uk.properties:
                    pn = (getattr(p, 'displayName', '') or '').lower()
                    pn_norm = (pn
                               .replace('ã', 'a').replace('á', 'a').replace('â', 'a')
                               .replace('ç', 'c')
                               .replace('é', 'e').replace('í', 'i').replace('ó', 'o').replace('õ', 'o').replace('ú', 'u')
                               )
                    if ('transpar' in pn_norm) or ('transparency' in pn_norm):
                        try:
                            p.setValue(100.0, True)   # 0..100
                        except Exception:
                            try:
                                p.setValue(1.0, True)  # fallback 0..1
                            except Exception:
                                pass
                        break
            except Exception:
                pass

            # (opcional) você pode ajustar outros parâmetros aqui, por nome:
            # for p in uk.properties:
            #     pn = (getattr(p, 'displayName', '') or '').lower()
            #     if 'tolerance' in pn or 'tolerancia' in pn: p.setValue(50.0, True)
            #     if 'pedestal' in pn: p.setValue(10.0, True)
            #     if 'choke' in pn or 'contração' in pn: p.setValue(0.0, True)
            #     if 'soften' in pn or 'suavizar' in pn: p.setValue(0.0, True)
            #     ...

            # (opcional) ajustes para key em preto (tolerance/pedestal)
            try:
                for p in uk.properties:
                    pn = (getattr(p, 'displayName', '') or '').lower()
                    pn_norm = (pn.replace('ã', 'a').replace('á', 'a').replace('â', 'a')
                               .replace('ç', 'c').replace('é', 'e').replace('í', 'i')
                               .replace('ó', 'o').replace('õ', 'o').replace('ú', 'u'))
                    if 'tolerance' in pn_norm or 'tolerancia' in pn_norm:
                        try:
                            p.setValue(50.0, True)
                        except:
                            pass
                    if 'pedestal' in pn_norm:
                        try:
                            p.setValue(10.0, True)
                        except:
                            pass
            except Exception:
                pass

            # debug: listar componentes do clipe
            try:
                names = [(getattr(c, 'displayName', '') or '')
                         for c in clip.components]
                print("[ultrakey] componentes do clipe:", names)
            except Exception:
                pass

            return True

        except Exception as e:
            print(f"[ultrakey] erro: {e}")
            return False

    def __parse_layer_cfg(self, roteiro_name: str, filename: str,
                          default_opacity: float = 100.0,
                          default_blend: str = 'Normal') -> tuple[float, str]:
        base = os.path.join(self.CWD, 'partes', roteiro_name, filename)
        opacity = default_opacity
        blend = default_blend
        if not os.path.exists(base):
            return (opacity, blend)
        try:
            raw = open(base, 'r', encoding='utf-8',
                       errors='ignore').read().splitlines()
            for ln in raw:
                l = (ln or '').strip()
                if not l:
                    continue
                low = l.lower()
                if 'opacidade' in low:
                    nums = ''.join(ch for ch in l if (
                        ch.isdigit() or ch in '.,')).replace(',', '.')
                    try:
                        opacity = float(nums)
                    except:
                        pass
                elif ('modo' in low) and (('mesclagem' in low) or ('blend' in low)):
                    v = l.split('=')[-1].strip()
                    if v:
                        blend = v
        except Exception:
            pass
        return (opacity, blend)

    def __parse_overlay_cfg(self, roteiro_name: str) -> tuple[float, str]:
        # overlay costuma usar Tela/Screen por padrão
        return self.__parse_layer_cfg(roteiro_name, 'overlay.txt', 100.0, '22')

    def __parse_logo_cfg(self, roteiro_name: str) -> tuple[float, str]:
        # logo com alpha geralmente é Normal 100%
        return self.__parse_layer_cfg(roteiro_name, 'logo.txt', 100.0, '22')

    def __get_video_duration_s(self, video_path: str) -> float:
        """Retorna a duracao do video em segundos via ffprobe. Retorna 0.0 se falhar."""
        try:
            ffprobe = self.__get_ffprobe_bin()
            cmd = [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                   '-of', 'json', video_path]
            proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_FFMPEG_CREATE_FLAGS)
            data = json.loads(proc.stdout or '{}')
            return float(data.get('format', {}).get('duration', 0))
        except Exception:
            return 0.0

    def __extend_video_ffmpeg(self, video_path: str, target_duration_s: float = 600.0) -> str:
        """Estende um video curto via FFmpeg (stream_loop) para target_duration_s. Retorna path do video estendido."""
        try:
            # Se o video original ja tem duracao >= 9min, nao precisa estender
            original_dur = self.__get_video_duration_s(video_path)
            if original_dur >= (target_duration_s * 0.9):
                print(f"[overlay] video original ja tem {original_dur:.0f}s (>= {target_duration_s * 0.9:.0f}s), pulando extend")
                return video_path

            out_dir = os.path.dirname(video_path)
            base_name = os.path.splitext(os.path.basename(video_path))[0]
            out_path = os.path.join(out_dir, f'{base_name}_10m.mp4')

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                print(f"[overlay] cache encontrado: {out_path}")
                return out_path

            ffmpeg = self.__get_ffmpeg_bin()
            cmd = [
                ffmpeg, '-y',
                '-stream_loop', '-1',
                '-i', video_path,
                '-t', f'{target_duration_s:.3f}',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-preset', 'medium', '-crf', '18',
                '-movflags', '+faststart',
                '-an',
                out_path
            ]
            print(f"[overlay] ffmpeg extend -> {out_path}")
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True, creationflags=_FFMPEG_CREATE_FLAGS)
            if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
        except Exception as e:
            print(f"[overlay] extend error: {e}")
        return video_path

    def __insert_overlay_full(self, *, roteiro_name: str, seq_end_time, paths_map: dict, project_item_cache: dict, overlay_path_override: str = '', prerendered_overlay_path: str = ''):
        """Insere overlay na timeline, repetindo ate cobrir toda a sequencia."""
        if overlay_path_override and os.path.exists(overlay_path_override):
            overlay_abs = overlay_path_override
        else:
            base_dir = os.path.join(self.CWD, 'partes', roteiro_name)
            overlay_abs = os.path.join(base_dir, 'overlay.mp4')
        if not os.path.exists(overlay_abs):
            print("[overlay] arquivo nao encontrado:", overlay_abs)
            return

        total_end_s = float(getattr(seq_end_time, "seconds", 0.0))
        if total_end_s <= 0.001:
            return

        # Usa video pre-renderizado de 10min se disponivel (menos insercoes na timeline)
        if prerendered_overlay_path and os.path.exists(prerendered_overlay_path):
            overlay_src = paths_map.get(prerendered_overlay_path, prerendered_overlay_path)
            print(f"[overlay] usando video pre-renderizado: {prerendered_overlay_path}")
        else:
            # Fallback: tenta estender via FFmpeg agora (bloqueante)
            extended = self.__extend_video_ffmpeg(overlay_abs, target_duration_s=600.0)
            if extended and extended != overlay_abs and os.path.exists(extended):
                overlay_src = paths_map.get(extended, extended)
                print(f"[overlay] usando video estendido (fallback): {extended}")
            else:
                overlay_src = paths_map.get(overlay_abs, overlay_abs)

        if not self.__ensure_video_track_index(self.OVERLAY_TRACK_INDEX):
            return

        seq = pymiere.objects.app.project.activeSequence
        vtrack = seq.videoTracks[self.OVERLAY_TRACK_INDEX]
        cur = pymiere.wrappers.time_from_seconds(0)

        imported_src = self.__get_or_import_project_item(
            overlay_src, project_item_cache)
        if imported_src == self.PYMIERE_UNDEFINED:
            print("[overlay] não foi possível importar o overlay de origem.")
            return

        # NOVO: pega dimensões do arquivo do overlay para calcular 'cover'
        try:
            overlay_dims = self.__get_scene_dimensions(overlay_src)
            overlay_cover_scale = self.__get_new_initial_scale(overlay_dims)
        except Exception:
            overlay_cover_scale = None

        debug_done = False

        # EPS maior para evitar "chiar" no fim por causa de floats
        EPS = 1e-2  # 10 ms
        stuck_count = 0

        while cur.seconds < total_end_s - EPS:
            prev_end = cur.seconds

            clip = self.__insert_clip_with_retry(
                track_type='video',
                track_index=self.OVERLAY_TRACK_INDEX,
                project_item=imported_src,
                start_time=cur
            )
            self.__throttle()

            vtrack = seq.videoTracks[self.OVERLAY_TRACK_INDEX]

            # ✅ APLICA TUDO ANTES do possível corte final
            try:
                self.__apply_overlay_settings(
                    clip, roteiro_name, overlay_cover_scale)
            except Exception:
                pass

            # Se passou do fim da sequência, corta AGORA
            if clip.end.seconds > total_end_s + EPS:
                tc = pymiere.wrappers.timecode_from_seconds(total_end_s, seq)
                self.__qe_razor_with_retry(track_type='video',
                                           track_index=self.OVERLAY_TRACK_INDEX,
                                           timecode=tc)
                # remove apenas o "restinho" pós-corte
                vtrack.clips[-1].remove(False, True)

                # 🔒 Reaplica ajustes no clipe que ficou (pós-razor)
                try:
                    kept = vtrack.clips[-1]
                    self.__apply_overlay_settings(
                        kept, roteiro_name, overlay_cover_scale)
                except Exception:
                    pass

                cur = pymiere.wrappers.time_from_seconds(total_end_s)
                break

            # fluxo normal (sem corte): avança
            cur = clip.end

            # trava anti-loop
            if (cur.seconds - prev_end) <= EPS:
                stuck_count += 1
                if stuck_count >= 2:
                    print(
                        "[overlay] avanço < EPS; encerrando para evitar loop infinito.")
                    break
            else:
                stuck_count = 0

        print(
            "[overlay] inserido (sem FFmpeg, loop até cobrir a sequência e SEM BARRAS PRETAS).")

    def __insert_logo_full(self, *, logo_path: str, logo_position: str, seq_end_time, paths_map: dict, project_item_cache: dict, dims_cache: dict, roteiro_name: str = '', prerendered_logo_path: str = ''):
        """
        Logo em V6 (LOGO_TRACK_INDEX):
        - Renderiza um video MP4 de 60s com o logo posicionado via FFmpeg.
        - Insere UMA VEZ e repete ate cobrir toda a sequencia.
        - logo_position: top_left, top_right, bottom_left, bottom_right
        """
        if not logo_path or not os.path.exists(logo_path):
            print("[logo] arquivo de logo nao encontrado:", logo_path)
            return

        total_end_s = float(getattr(seq_end_time, "seconds", 0.0))
        if total_end_s <= 1e-3:
            return

        # Usa video pre-renderizado se disponivel (thread paralela ja terminou)
        if prerendered_logo_path and os.path.exists(prerendered_logo_path):
            logo_video = prerendered_logo_path
            print(f"[logo] usando video pre-renderizado: {logo_video}")
        else:
            # Fallback: renderiza agora (bloqueante)
            logo_video = self.__render_logo_positioned_mp4(
                logo_path, logo_position, duration_s=600.0)
        if not logo_video or not os.path.exists(logo_video):
            print("[logo] falha ao renderizar logo posicionado.")
            return

        if not self.__ensure_video_track_index(self.LOGO_TRACK_INDEX):
            return

        seq = pymiere.objects.app.project.activeSequence
        cur = pymiere.wrappers.time_from_seconds(0)

        logo_src = paths_map.get(logo_video, logo_video)
        imported = self.__get_or_import_project_item(
            logo_src, project_item_cache)
        if imported == self.PYMIERE_UNDEFINED:
            print("[logo] nao foi possivel importar o logo.")
            return

        EPS = 1e-2
        stuck_count = 0

        while cur.seconds < total_end_s - EPS:
            prev_end = cur.seconds

            clip = self.__insert_clip_with_retry(
                track_type='video',
                track_index=self.LOGO_TRACK_INDEX,
                project_item=imported,
                start_time=cur
            )
            self.__throttle()

            try:
                self.__disable_scale_to_frame_size(clip)
            except Exception:
                pass

            # aplica opacidade/blend se houver config
            if roteiro_name:
                try:
                    opacity, blend = self.__parse_logo_cfg(roteiro_name)
                    self.__set_opacity_and_blend(
                        clip, opacity=opacity, blend_mode=blend)
                except Exception:
                    pass

            if clip.end.seconds > total_end_s + EPS:
                tc_cut = pymiere.wrappers.timecode_from_seconds(
                    total_end_s, seq)
                self.__qe_razor_with_retry(track_type='video',
                                           track_index=self.LOGO_TRACK_INDEX,
                                           timecode=tc_cut)
                vtrack = seq.videoTracks[self.LOGO_TRACK_INDEX]
                vtrack.clips[-1].remove(False, True)
                break

            cur = clip.end

            if (cur.seconds - prev_end) <= EPS:
                stuck_count += 1
                if stuck_count >= 2:
                    break
            else:
                stuck_count = 0

        print(f"[logo] inserido (posicao: {logo_position}).")

    def __render_logo_positioned_mp4(self, logo_path: str, position: str, duration_s: float = 600.0) -> Optional[str]:
        """
        Renderiza MP4 transparente (ProRes4444 ou MP4 com alpha) do logo posicionado
        no canto escolhido sobre base transparente/preta, na resolucao da sequencia.
        """
        try:
            out_dir = os.path.dirname(logo_path)
            logo_base = os.path.splitext(os.path.basename(logo_path))[0]
            safe_pos = position.replace(' ', '_')
            out_path = os.path.join(out_dir, f'{logo_base}_10m_{safe_pos}_{self.FRAME_W}x{self.FRAME_H}.mp4')

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                print(f"[logo] cache encontrado: {out_path}")
                return out_path

            ffmpeg = self.__get_ffmpeg_bin()
            ext = os.path.splitext(logo_path)[1].lower()
            is_image = ext in EXTENSIONS['IMAGE']

            if is_image:
                src_args = ['-loop', '1', '-t', f'{duration_s:.3f}', '-i', logo_path]
            else:
                src_args = ['-stream_loop', '-1', '-i', logo_path, '-t', f'{duration_s:.3f}']

            size = f"{self.FRAME_W}x{self.FRAME_H}"
            base = ['-f', 'lavfi', '-t', f'{duration_s:.3f}', '-i', f'color=c=black:s={size}']

            margin = int(self.LOGO_MARGIN_PX)
            scale_pct = float(getattr(self, 'LOGO_FIXED_SCALE_PERCENT', 12.0)) / 100.0

            # Escala proporcional à resolução da sequência (não ao tamanho original do logo)
            target_h = int(self.FRAME_H * scale_pct)

            # Calcula posicao do overlay no FFmpeg
            pos_map = {
                'top_left': f'x={margin}:y={margin}',
                'top_right': f'x=main_w-overlay_w-{margin}:y={margin}',
                'bottom_left': f'x={margin}:y=main_h-overlay_h-{margin}',
                'bottom_right': f'x=main_w-overlay_w-{margin}:y=main_h-overlay_h-{margin}',
            }
            pos_expr = pos_map.get(position, pos_map['bottom_right'])

            fc = (
                f"[0:v]scale=-1:{target_h}:flags=bicubic,format=rgba[lg];"
                f"[1:v][lg]overlay={pos_expr}:format=auto"
            )

            fps = self.__get_active_sequence_fps_or(25)

            cmd = [
                ffmpeg, '-y',
                *src_args,
                *base,
                '-filter_complex', fc,
                '-r', str(fps),
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-preset', 'medium', '-crf', '18',
                '-movflags', '+faststart',
                '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                '-an',
                out_path
            ]
            print(f"[logo] ffmpeg render ({position}, {fps}fps) -> {out_path}")
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True, creationflags=_FFMPEG_CREATE_FLAGS)
            self.__write_ffmpeg_debug(out_dir, cmd, proc, f'logo_60s_{safe_pos}')
            if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
        except Exception as e:
            print(f"[logo] render error: {e}")

        return None

    def __apply_logo_settings(self, clip, roteiro_name: str):
        """Aplica neutralização de Scale-to-Frame e Opacidade/Blend no clipe do logo."""
        # 1) Neutraliza Scale-to-Frame (evita herdar flags indesejadas)
        try:
            self.__disable_scale_to_frame_size(clip)
        except Exception:
            pass

        # 2) (Opcional) garantir posição/escala padrão (logo_60s já vem 1920x1080 com alpha)
        #    Se quiser força absoluta, descomente:
        # try:
        #     self.__set_motion(clip, scale=100.0)
        # except Exception:
        #     pass

        # 3) Ultra Key raramente necessário para MOV com alpha, mas respeita flag
        if getattr(self, 'ULTRA_KEY_ENABLE', False):
            try:
                self.__apply_ultra_key(clip, key_hex=getattr(
                    self, 'ULTRA_KEY_COLOR_HEX', '#000000'))
            except Exception:
                pass

        # 4) Opacidade e Modo de Mesclagem (lidos de logo.txt)
        try:
            opacity, blend = self.__parse_logo_cfg(roteiro_name)
            self.__set_opacity_and_blend(
                clip, opacity=opacity, blend_mode=blend)
        except Exception:
            pass

    def export_xml(self) -> Result:
        from .premiere import editing
        return editing.export_xml(self)

    def save_project(self, *_args, **_kwargs) -> Result:
        from .premiere import editing
        return editing.save_project(self)

    def __get_ffprobe_bin(self) -> str:
        """Retorna path do ffprobe (mesmo diretorio do ffmpeg)."""
        ffmpeg = self.__get_ffmpeg_bin()
        ffprobe = os.path.join(os.path.dirname(ffmpeg), 'ffprobe')
        if os.path.exists(ffprobe) or os.path.exists(ffprobe + '.exe'):
            return ffprobe
        return 'ffprobe'  # fallback para PATH

    def __get_scene_dimensions(self, scene_path: str) -> Dimensions:
        try:
            ffprobe = self.__get_ffprobe_bin()
            cmd = [ffprobe, '-v', 'error', '-select_streams', 'v:0',
                   '-show_entries', 'stream=width,height', '-of', 'json', scene_path]
            proc = subprocess.run(cmd, capture_output=True,
                                  encoding='utf-8', text=True, creationflags=_FFMPEG_CREATE_FLAGS)
            data = json.loads(proc.stdout or '{}')
            streams = data.get('streams') or []
            if streams and 'width' in streams[0] and 'height' in streams[0]:
                return Dimensions(streams[0]['width'], streams[0]['height'])
        except Exception:
            pass
        # fallback seguro
        return Dimensions(self.FRAME_W, self.FRAME_H)

    def __get_new_initial_scale(self, scene_dimensions: Dimensions):
        """
        'Cover' (preencher o quadro): escolhe o MAIOR fator de escala necessário
        para garantir que NENHUMA dimensão fique menor que 1920x1080.
        Pode cortar nas bordas, mas elimina barras pretas.
        """
        IDEAL_WIDTH_IN_PX = float(self.FRAME_W)
        IDEAL_HEIGHT_IN_PX = float(self.FRAME_H)

        scene_width = float(scene_dimensions.width or 1920)
        scene_height = float(scene_dimensions.height or 1080)

        # fatores para cada dimensão
        scale_w = IDEAL_WIDTH_IN_PX / max(1.0, scene_width)
        scale_h = IDEAL_HEIGHT_IN_PX / max(1.0, scene_height)

        # cover => usa o MAIOR dos dois
        cover_scale = max(scale_w, scale_h) * 100.0
        return math.ceil(cover_scale)

    def __get_scale_calculator(
        self,
        initial_scale: int | float,
        start_second: float,
        end_second: float,
        min_scale_multiplier=1.0,
        max_scale_multiplier=1.1
    ):
        def calculate_scale(current_second: float):
            if current_second <= start_second:
                return initial_scale

            if current_second >= end_second:
                return initial_scale * max_scale_multiplier

            # Linear interpolation
            return initial_scale * (min_scale_multiplier + (max_scale_multiplier - min_scale_multiplier) * (current_second - start_second) / (end_second - start_second))

        return calculate_scale

    def __animate_zoom(
        self,
        clip: pymiere.TrackItem,
        animation_fn: Callable[[float], float]
    ):
        for component in clip.components:
            if component.displayName != self.PREMIERE_MOVEMENT_EFFECT_NAME:
                continue

            for component_property in component.properties:
                if component_property.displayName != self.PREMIERE_SCALE_PROPERTY_NAME:
                    continue

                if not component_property.isTimeVarying():
                    component_property.setTimeVarying(True)

                component_property.addKey(clip.inPoint.seconds)
                component_property.setValueAtKey(
                    clip.inPoint.seconds,
                    animation_fn(clip.start.seconds),
                    True
                )

                component_property.addKey(clip.outPoint.seconds)
                component_property.setValueAtKey(
                    clip.outPoint.seconds,
                    animation_fn(clip.end.seconds),
                    True
                )

    # --- helpers de performance ---

    def __throttle(self):
        """Evita saturar o servidor local do pymiere (porta 3000)."""
        try:
            if self.REQUEST_THROTTLE_SECONDS and self.REQUEST_THROTTLE_SECONDS > 0:
                time.sleep(self.REQUEST_THROTTLE_SECONDS)
        except Exception:
            pass

    def __get_project_item_cached(self, media_path: str, cache: dict):
        """
        Retorna o ProjectItem do cache; se não houver, garante a importação
        (delegando para __get_or_import_project_item) e atualiza o cache.
        Nunca retorna None: retorna o item ou self.PYMIERE_UNDEFINED.
        """
        item = cache.get(media_path)
        if item is not None:
            return item

        # garante a importação/busca com polling
        item = self.__get_or_import_project_item(media_path, cache)
        return item

    def __get_scene_dimensions_cached(self, media_path: str, dims_cache: dict):
        """Dimensões de vídeo com cache (evita rodar ffprobe a cada inserção)."""
        dims = dims_cache.get(media_path)
        if dims is not None:
            return dims
        dims = self.__get_scene_dimensions(media_path)
        dims_cache[media_path] = dims
        return dims

    def __prefetch_all_media(
        self,
        media_paths: list,
        project_item_cache: dict,
        dims_cache: dict,
        max_workers: int = 5,
    ):
        """
        Fase 1 de pré-processamento (Performance de Inserção):
        1. Importa todos os arquivos de mídia em UMA única chamada importFiles,
           eliminando N chamadas individuais + 5s de polling por arquivo.
        2. Aguarda indexação de todos em paralelo (polling único com timeout 20s).
        3. Pré-calcula dimensões via ffprobe em paralelo com ThreadPoolExecutor,
           eliminando o ffprobe sequencial dentro do loop principal.
        Popula project_item_cache e dims_cache antes do loop de montagem.
        """
        paths_to_import = [
            p for p in media_paths
            if p and p not in project_item_cache
            or project_item_cache.get(p) == self.PYMIERE_UNDEFINED
        ]

        if paths_to_import:
            # --- passo 1: importa tudo de uma vez ---
            try:
                pymiere.objects.app.project.importFiles(
                    paths_to_import,
                    True,
                    pymiere.objects.app.project.getInsertionBin(),
                    False,
                )
            except Exception as e:
                print(f"[prefetch] importFiles em lote falhou (parcial ok): {e}")

            # --- passo 2: polling unico ate todos indexados (timeout 20s) ---
            remaining = set(paths_to_import)
            deadline = time.time() + 20.0
            while remaining and time.time() < deadline:
                time.sleep(0.05)  # sleep fixo leve, sem throttle IPC
                for p in list(remaining):
                    try:
                        items = pymiere.objects.app.project.rootItem \
                            .findItemsMatchingMediaPath(p, ignoreSubclips=False)
                        if items:
                            project_item_cache[p] = items[0]
                            remaining.discard(p)
                    except Exception:
                        pass
            # arquivos que não indexaram no tempo limite → marca UNDEFINED
            for p in remaining:
                project_item_cache.setdefault(p, self.PYMIERE_UNDEFINED)

        # --- passo 3: ffprobe em paralelo para todos os paths ---
        paths_for_dims = [p for p in media_paths if p and p not in dims_cache]
        if paths_for_dims:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                future_to_path = {
                    ex.submit(self.__get_scene_dimensions, p): p
                    for p in paths_for_dims
                }
                for future in as_completed(future_to_path):
                    p = future_to_path[future]
                    try:
                        dims_cache[p] = future.result()
                    except Exception:
                        pass

    def __get_or_import_project_item(self, media_path: str, cache: dict):
        """
        Garante que o arquivo esteja importado no projeto e retorna o ProjectItem.
        Faz importFiles + polling de ate ~5s para a indexacao do Premiere.
        Tenta com path original e normalizado (barras) para compatibilidade.
        """
        # Tenta pegar do cache
        item = cache.get(media_path)
        if item is not None and item != self.PYMIERE_UNDEFINED:
            return item

        # Paths alternativos para busca (Windows: / vs \)
        norm_path = os.path.normpath(media_path)
        fwd_path = media_path.replace('\\', '/')
        search_paths = list(dict.fromkeys([media_path, norm_path, fwd_path]))

        # Tenta encontrar ja importado
        for sp in search_paths:
            try:
                item = pymiere.objects.app.project.rootItem.findItemsMatchingMediaPath(
                    sp, ignoreSubclips=False)[0]
            except Exception:
                item = self.PYMIERE_UNDEFINED
            if item != self.PYMIERE_UNDEFINED:
                cache[media_path] = item
                return item

        # Importa e aguarda indexacao
        try:
            pymiere.objects.app.project.importFiles(
                [norm_path], True, pymiere.objects.app.project.getInsertionBin(), False)
        except Exception:
            pass

        start_wait = time.time()
        item = self.PYMIERE_UNDEFINED
        while time.time() - start_wait < 8.0:
            self.__throttle()
            for sp in search_paths:
                try:
                    item = pymiere.objects.app.project.rootItem.findItemsMatchingMediaPath(
                        sp, ignoreSubclips=False)[0]
                except Exception:
                    item = self.PYMIERE_UNDEFINED
                if item != self.PYMIERE_UNDEFINED:
                    break
            if item != self.PYMIERE_UNDEFINED:
                break
            time.sleep(0.2)

        if item == self.PYMIERE_UNDEFINED:
            print(f"[import] FALHA ao encontrar no projeto: {media_path}")

        cache[media_path] = item
        return item

    # cache de velocidade de zoom por mídia (delta de multiplicador por segundo)
    _zoom_slope_cache: dict = {}

    def __get_or_set_zoom_slope(self, media_path: str, min_mult: float, max_mult: float, duration: float) -> float:
        """
        Guarda/retorna a taxa de zoom por segundo para a mídia.
        slope = (max_mult - min_mult) / duração_da_primeira_ocorrência
        Em ocorrências futuras, mantemos a mesma velocidade (mesmo slope).
        """
        s = self._zoom_slope_cache.get(media_path)
        if s is not None:
            return s
        s = (max_mult - min_mult) / max(0.001, duration)
        self._zoom_slope_cache[media_path] = s
        return s

    def __is_image(self, media_path: str) -> bool:
        try:
            _, ext = os.path.splitext(media_path)
            return ext.lower() in EXTENSIONS['IMAGE']
        except Exception:
            return False

    def __is_video(self, media_path: str) -> bool:
        try:
            _, ext = os.path.splitext(media_path)
            return ext.lower() in EXTENSIONS['VIDEO']
        except Exception:
            return False

    def __try_set_speed(self, track_item, speed_percent: float) -> bool:
        return self.__set_speed_with_retry(track_item, speed_percent)

    # ====== CARTELA: leitura do info.txt ======

    def __read_info_lines_for_roteiro(self, roteiro_name: str) -> list[str]:
        """
        Lê partes/<roteiro_name>/info.txt e retorna lista de linhas (uma por cena).
        Se não existir, retorna [].
        """
        try:
            base = os.path.join(self.CWD, 'partes', roteiro_name, 'info.txt')
            if not os.path.exists(base):
                return []
            with open(base, 'r', encoding='utf-8', errors='ignore') as f:
                lines = [ln.strip('\r\n') for ln in f.readlines()]
            return lines
        except Exception:
            return []

    # ====== CARTELA: estilo (.prtextstyle) → dict ======

    def __load_text_style_from_prtextstyle(self, roteiro_name: str) -> Dict[str, Any]:
        """
        Lê partes/<roteiro_name>/estilodetexto.prtextstyle (tentativa de JSON)
        e retorna um dicionário com: font, font_size, color, line_spacing,
        (opcionais) wrap_max_chars, duration_secs.
        Defaults: Arial, 64, white.
        """
        style_path = os.path.join(
            self.CWD, 'partes', roteiro_name, 'estilodetexto.prtextstyle')
        style: Dict[str, Any] = {
            'font': 'Arial',
            'font_size': 64,
            'color': 'white',   # ffmpeg drawtext aceita 'white' ou '0xRRGGBB@alpha'
            'line_spacing': 0
        }

        if os.path.exists(style_path):
            try:
                raw = open(style_path, 'r', encoding='utf-8',
                           errors='ignore').read()
                try:
                    data = json.loads(raw)
                except Exception:
                    data = {}

                # font
                for k in ('fontFamily', 'family', 'font_name', 'font'):
                    v = data.get(k) if isinstance(data, dict) else None
                    if isinstance(v, str) and v.strip():
                        style['font'] = v.strip()
                        break

                # font_size
                for k in ('fontSize', 'size', 'pointSize'):
                    v = data.get(k) if isinstance(data, dict) else None
                    if isinstance(v, (int, float)) and v > 0:
                        style['font_size'] = int(float(v))
                        break

                # color (aceita dict, lista ou string)
                color = None
                for k in ('fillColor', 'color', 'textColor'):
                    v = data.get(k) if isinstance(data, dict) else None
                    if isinstance(v, dict):
                        r = v.get('r') or v.get('red')
                        g = v.get('g') or v.get('green')
                        b = v.get('b') or v.get('blue')
                        a = v.get('a') or v.get('alpha') or 1

                        def _norm(x):
                            if x is None:
                                return 255
                            x = float(x)
                            return int(round(x*255)) if x <= 1.0 else int(round(x))
                        R, G, B = _norm(r), _norm(g), _norm(b)
                        A = float(a)
                        A = A if A <= 1.0 else A/255.0
                        color = f"0x{R:02X}{G:02X}{B:02X}@{max(0.0,min(1.0,A)):.3f}"
                    elif isinstance(v, list) and len(v) >= 3:
                        R, G, B = [int(float(x)*255) if float(x)
                                   <= 1 else int(x) for x in v[:3]]
                        A = 1.0
                        if len(v) >= 4:
                            a = float(v[3])
                            A = a if a <= 1.0 else a/255.0
                        color = f"0x{R:02X}{G:02X}{B:02X}@{max(0.0,min(1.0,A)):.3f}"
                    elif isinstance(v, str) and v.strip():
                        color = v.strip()
                    if color:
                        break
                if color:
                    style['color'] = color

                # line spacing
                ls = data.get('lineSpacing') if isinstance(
                    data, dict) else None
                if isinstance(ls, (int, float)):
                    style['line_spacing'] = int(ls)

                # (NOVO) wrap_max_chars
                wmc = None
                for k in ('wrapMaxChars', 'maxChars', 'maxLineChars', 'wrapWidth'):
                    v = data.get(k) if isinstance(data, dict) else None
                    if isinstance(v, (int, float)) and int(v) > 0:
                        wmc = int(v)
                        break
                if wmc:
                    style['wrap_max_chars'] = wmc

                # (NOVO) duration_secs
                dur = None
                for k in ('duration', 'titlecardDuration', 'durationSecs'):
                    v = data.get(k) if isinstance(data, dict) else None
                    try:
                        fv = float(v)
                        if fv > 0:
                            dur = fv
                            break
                    except Exception:
                        pass
                if dur:
                    style['duration_secs'] = dur

            except Exception:
                pass

        return style

    def __wrap_text(self, text: str, max_chars: int = 36) -> str:
        if not text:
            return ''
        out = []
        for paragraph in str(text).splitlines():
            p = paragraph.strip()
            if not p:
                out.append('')
                continue
            wrapped = textwrap.fill(
                p,
                width=max_chars,
                break_long_words=True,     # <-- garante quebra em palavras gigantes
                break_on_hyphens=False,
                replace_whitespace=True,
                drop_whitespace=True,
            )
            out.append(wrapped)
        return "\n".join(out)

    def __get_wrap_max_chars(self, roteiro_name: str, default: int = 36) -> int:
        style = self.__load_text_style_from_prtextstyle(roteiro_name)
        v = style.get('wrap_max_chars')
        try:
            iv = int(v)
            return iv if iv > 0 else default
        except Exception:
            return default

    def __get_titlecard_duration(self, roteiro_name: str, default: Optional[float] = 3.0) -> Optional[float]:
        style = self.__load_text_style_from_prtextstyle(roteiro_name)
        v = style.get('duration_secs')
        try:
            fv = float(v)
            return fv if fv > 0 else default
        except Exception:
            return default

    # ====== CARTELA: helpers FFmpeg ======
    def __chromakey_to_alpha(self, video_path: str, key_color_hex: str = "#00FF00",
                              similarity: float = 0.3, blend: float = 0.1) -> str:
        """
        Converte video com fundo chroma key para MOV ProRes 4444 com canal alfa real.
        Retorna o path do arquivo convertido, ou o original se falhar.
        """
        try:
            out_dir = os.path.dirname(video_path)
            base_name = os.path.splitext(os.path.basename(video_path))[0]
            out_path = os.path.join(out_dir, f'{base_name}_alpha.mov')

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path

            ffmpeg = self.__get_ffmpeg_bin()

            # Converte hex para formato FFmpeg (0xRRGGBB)
            color = key_color_hex.replace('#', '0x')

            cmd = [
                ffmpeg, '-y',
                '-i', video_path,
                '-vf', f'chromakey={color}:{similarity}:{blend}',
                '-c:v', 'prores_ks',
                '-profile:v', '4444',
                '-pix_fmt', 'yuva444p10le',
                '-c:a', 'pcm_s16le',
                out_path
            ]
            print(f"[cta] ffmpeg chromakey -> {out_path}")
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True, creationflags=_FFMPEG_CREATE_FLAGS)
            if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                print(f"[cta] chromakey convertido com sucesso")
                return out_path
            else:
                print(f"[cta] ffmpeg chromakey falhou (code {proc.returncode})")
                if proc.stderr:
                    print(f"[cta] stderr: {proc.stderr[-300:]}")
        except Exception as e:
            print(f"[cta] chromakey erro: {e}")

        return video_path

    @staticmethod
    def __db_to_linear(db: float) -> float:
        """Converte dB para ganho linear na escala do Premiere.
        Premiere usa escala onde 1.0 linear = +15 dB na UI.
        Portanto: 0 dB UI = 10^(-15/20) ~= 0.1778, -12 dB UI = 10^(-27/20), etc."""
        if db <= -96:
            return 0.0
        return 10.0 ** ((db - 15.0) / 20.0)

    def __set_audio_track_volume_db(self, track_index: int, db: float):
        """Define o volume (em dB) de todos os clips de uma trilha de audio.
        Converte dB para ganho linear antes de aplicar."""
        linear = self.__db_to_linear(db)
        try:
            seq = pymiere.objects.app.project.activeSequence
            atrack = seq.audioTracks[track_index]

            # Garantir que a trilha NAO esteja mutada
            try:
                atrack.setMute(0)
            except Exception:
                pass

            clips = list(atrack.clips)
            if not clips:
                print(f"[mixer] A{track_index + 1}: nenhum clip encontrado")
                return

            applied = 0
            for ci, clip in enumerate(clips):
                try:
                    found = False
                    for comp in clip.components:
                        dn = (getattr(comp, 'displayName', '') or '').lower()
                        if 'volume' not in dn:
                            continue
                        # Pegar somente o componente "Volume", nao "Volume do canal"
                        if 'canal' in dn or 'channel' in dn:
                            continue

                        for prop in comp.properties:
                            pn = (getattr(prop, 'displayName', '') or '').lower()
                            pn_clean = pn.replace('\u00ed', 'i').replace('\u00e1', 'a').replace('\u00e9', 'e')
                            if 'level' in pn_clean or 'nivel' in pn_clean or 'db' in pn_clean or pn_clean == 'volume':
                                try:
                                    prop.setValue(linear, True)
                                    applied += 1
                                    found = True
                                except Exception as e_set:
                                    print(f"[mixer] A{track_index + 1} clip{ci} setValue falhou: {e_set}")
                                break
                        if found:
                            break
                except Exception as e_clip:
                    print(f"[mixer] A{track_index + 1} clip{ci} erro: {e_clip}")

            print(f"[mixer] A{track_index + 1} = {db} dB (linear={linear:.6f}) (aplicado em {applied}/{len(clips)} clips)")
        except Exception as e:
            print(f"[mixer] erro ao definir volume A{track_index + 1}: {e}")

    def __get_ffmpeg_bin(self) -> str:
        """
        Retorna o caminho do binário do FFmpeg. Tenta:
        1) variável de ambiente FFMPEG_BIN
        2) ffmpeg bundled junto ao executável/projeto (ffmpeg/bin/)
        3) 'ffmpeg' no PATH
        """
        # 1) env
        env_bin = os.environ.get('FFMPEG_BIN')
        if env_bin and os.path.exists(env_bin):
            return env_bin

        # 2) bundled + PATH (via utilitário centralizado)
        from app.utils.ffmpeg_path import get_ffmpeg_bin
        return get_ffmpeg_bin()

    def __write_ffmpeg_debug(self, log_dir: str, cmd: list[str], proc: subprocess.CompletedProcess, tag: str):
        """Grava o comando e stderr/stdout do FFmpeg para depuração."""
        try:
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, f'ffmpeg_{tag}_cmd.txt'), 'w', encoding='utf-8') as f:
                f.write(" ".join(cmd))
            with open(os.path.join(log_dir, f'ffmpeg_{tag}_stdout.txt'), 'w', encoding='utf-8') as f:
                f.write(proc.stdout or '')
            with open(os.path.join(log_dir, f'ffmpeg_{tag}_stderr.txt'), 'w', encoding='utf-8') as f:
                f.write(proc.stderr or '')
        except Exception:
            pass

    def __ffmpeg_escape_text(self, s: str) -> str:
        # normaliza quebras de linha
        s = s.replace('\r\n', '\n').replace('\r', '\n')
        # ESCAPE: primeiro faça o \n virar \n, depois dobre as barras
        s = s.replace('\n', r'\n')     # agora temos uma barra
        s = s.replace('\\', r'\\')     # dobra todas as barras (vira \\n)
        # demais escapings do filtro
        s = s.replace(':', r'\:')
        s = s.replace("'", r"\'")
        return s

    def __find_font_file(self, font_family: str) -> Optional[str]:
        """
        Tenta localizar .ttf/.otf da família informada. Se não achar, devolve None.
        """
        candidates_dirs = []
        if os.name == 'nt':
            candidates_dirs += [r'C:\Windows\Fonts']
        else:
            candidates_dirs += [
                '/System/Library/Fonts', '/Library/Fonts', os.path.expanduser(
                    '~/Library/Fonts'),
                '/usr/share/fonts', '/usr/local/share/fonts', os.path.expanduser(
                    '~/.fonts')
            ]
        family = (font_family or '').lower().replace(' ', '')
        exts = ('.ttf', '.otf', '.ttc', '.otc')
        for d in candidates_dirs:
            try:
                for root, _, files in os.walk(d):
                    for f in files:
                        fn = f.lower()
                        if fn.endswith(exts):
                            base = fn.replace(' ', '')
                            if family and family in base:
                                return os.path.join(root, f)
            except Exception:
                pass
        return None

    def __render_titlecard_video(self, roteiro_name: str, cena_index: int, text: str, duration_secs: float, style: Dict[str, Any]) -> Optional[str]:
        """
        Gera um MP4 com a cartela:
        - TXT/logs continuam em assets/titlecards/<roteiro>
        - VÍDEO final é salvo em partes/<roteiro>/title_###.mp4
        """
        duration_secs = max(0.5, float(duration_secs))

        # >>> diretórios separados: TXT em assets/, VÍDEO em partes/
        txt_dir = os.path.join(self.CWD, 'assets', 'titlecards', roteiro_name)
        os.makedirs(txt_dir, exist_ok=True)

        out_dir = os.path.join(self.CWD, 'partes', roteiro_name)
        os.makedirs(out_dir, exist_ok=True)

        safe_idx = f"{cena_index+1:03d}"
        out_path = os.path.join(out_dir, f"title_{safe_idx}.mp4")

        font_family = style.get('font') or 'Arial'
        font_size = int(style.get('font_size') or 64)
        font_color = style.get('color') or 'white'
        line_spacing = int(style.get('line_spacing') or 0)

        # TXT de conteúdo (continua em assets/titlecards/…)
        txt_path = os.path.join(txt_dir, f"title_{safe_idx}.txt")
        with open(txt_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

        def _esc_path(p: str) -> str:
            p = os.path.abspath(p).replace('\\', '/')
            p = p.replace(':', r'\:').replace("'", r"\'")
            return p

        base_params = [
            f"textfile='{_esc_path(txt_path)}'",
            f"fontsize={font_size}",
            f"fontcolor={font_color}",
            "x=(w-text_w)/2",
            "y=(h-text_h)/2",
            "text_align=center",
            "box=0",
        ]
        if line_spacing:
            base_params.append(f"line_spacing={line_spacing}")

        ffmpeg_bin = self.__get_ffmpeg_bin()

        # Varie drawtext (com fontfile, com font=, sem fonte explícita)
        draw_variants = []
        fontfile = self.__find_font_file(font_family)
        if fontfile and os.path.exists(fontfile):
            draw_variants.append(
                base_params + [f"fontfile='{_esc_path(fontfile)}'"])
        draw_variants.append(
            base_params + [f"font='{self.__ffmpeg_escape_text(font_family)}'"])
        draw_variants.append(list(base_params))

        combos = []
        for dv in draw_variants:
            combos.append(('libx264', dv))
        for dv in draw_variants:
            combos.append(('mpeg4', dv))  # fallback

        for idx_try, (codec, dv) in enumerate(combos, start=1):
            draw_filter = "drawtext=" + ":".join(dv)
            size = f"{self.FRAME_W}x{self.FRAME_H}"
            cmd = [
                ffmpeg_bin, '-y',
                '-f', 'lavfi', '-i', f'color=c=black:s={size}:d={duration_secs:.3f}',
                '-vf', draw_filter,
                '-c:v', codec, '-pix_fmt', 'yuv420p',
                out_path
            ]
            try:
                proc = subprocess.run(
                    cmd, check=False, capture_output=True, text=True, creationflags=_FFMPEG_CREATE_FLAGS)
                # >>> logs agora vão para txt_dir (assets/titlecards/…)
                self.__write_ffmpeg_debug(
                    txt_dir, cmd, proc, f'try{idx_try}_{codec}')
                if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return out_path
            except Exception as e:
                fake = subprocess.CompletedProcess(
                    cmd, returncode=1, stdout='', stderr=str(e))
                self.__write_ffmpeg_debug(
                    txt_dir, cmd, fake, f'exception_try{idx_try}_{codec}')

        return None

    # ====== Helpers genericos para garantir trilhas de video/audio ======

    def __ensure_audio_track_index(self, idx: int):
        """Garante que o indice de trilha de audio 'idx' exista."""
        try:
            _ = pymiere.objects.app.project.activeSequence.audioTracks[idx]
            return True
        except Exception:
            pass
        try:
            qe_seq = pymiere.objects.qe.project.getActiveSequence()
            for _ in range(8):
                try:
                    qe_seq.addTracks(0, 1)  # 0 video, 1 audio
                except Exception:
                    try:
                        qe_seq.addTracks()
                    except Exception:
                        pass
                try:
                    _ = pymiere.objects.app.project.activeSequence.audioTracks[idx]
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def __ensure_video_track_index(self, idx: int):
        """
        Garante que o índice de trilha de vídeo 'idx' exista (V0=0, V1=1, V2=2, ...).
        Tenta via QE addTracks() se estiver faltando.
        """
        try:
            _ = pymiere.objects.app.project.activeSequence.videoTracks[idx]
            return True
        except Exception:
            pass

        # tenta criar pelo QE
        try:
            qe_seq = pymiere.objects.qe.project.getActiveSequence()
            # tenta várias vezes adicionar 1 trilha
            for _ in range(8):
                try:
                    qe_seq.addTracks(1, 0)  # 1 vídeo, 0 áudio
                except Exception:
                    # algumas versões usam assinatura diferente; tenta sem args
                    try:
                        qe_seq.addTracks()
                    except Exception:
                        pass
                try:
                    _ = pymiere.objects.app.project.activeSequence.videoTracks[idx]
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def __clear_video_track_range(self, vindex: int, start_s: float, end_s: float):
        """
        Remove todos os clipes de vídeo da trilha vindex que intersectam o intervalo [start_s, end_s).
        Útil para 'limpar e refazer' overlay/logo sem duplicar.
        """
        try:
            v = pymiere.objects.app.project.activeSequence.videoTracks[vindex]
        except Exception:
            return  # trilha nem existe, então não há o que limpar

        try:
            # varre de trás pra frente para não quebrar os índices ao remover
            for i in range(len(v.clips) - 1, -1, -1):
                c = v.clips[i]
                c_start = getattr(c.start, "seconds", 0.0)
                c_end = getattr(c.end, "seconds",   0.0)
                if c_start < end_s and c_end > start_s:  # há interseção com a janela
                    c.remove(False, True)
        except Exception:
            pass

    # ====== CARTELA: fade-out por Opacidade ======

    def __animate_opacity_fade_out(self, clip, fade_seconds: float = 0.5):
        """
        Adiciona keyframes de Opacidade para "desaparecer" no final do clipe.
        """
        try:
            end_s = clip.outPoint.seconds
            start_fade = max(clip.inPoint.seconds, end_s -
                             max(0.1, float(fade_seconds)))
            for comp in clip.components:
                for prop in comp.properties:
                    dn = getattr(prop, 'displayName', '') or ''
                    if dn.lower() in ('opacity', 'opacidade'):
                        if not prop.isTimeVarying():
                            prop.setTimeVarying(True)
                        try:
                            # 100% no início do fade
                            prop.addKey(start_fade)
                            prop.setValueAtKey(start_fade, 100.0, True)
                            # 0% no final
                            prop.addKey(end_s)
                            prop.setValueAtKey(end_s, 0.0, True)
                            return True
                        except Exception:
                            pass
        except Exception:
            pass
        return False

    def __animate_opacity_fade_in(self, clip, fade_seconds: float = 0.5) -> bool:
        """
        Adiciona keyframes de Opacidade para "aparecer" no início do clipe.
        0% no inPoint → 100% em fade_seconds.
        """
        try:
            start_s = float(clip.inPoint.seconds)
            end_s = float(clip.outPoint.seconds)
            duration = max(0.0, end_s - start_s)
            fade = max(0.05, min(float(fade_seconds),
                       max(0.0, duration - 0.01)))
            if duration <= 0.1 or fade <= 0.05:
                return False

            fade_in_end = start_s + fade

            for comp in clip.components:
                cname = (getattr(comp, 'displayName', '') or '').lower()
                if cname not in ('opacidade', 'opacity'):
                    continue
                for prop in comp.properties:
                    pn = (getattr(prop, 'displayName', '') or '').lower()
                    if pn not in ('opacidade', 'opacity'):
                        continue

                    if not prop.isTimeVarying():
                        prop.setTimeVarying(True)

                    try:
                        prop.addKey(start_s)
                        prop.setValueAtKey(start_s, 0.0, True)

                        prop.addKey(fade_in_end)
                        prop.setValueAtKey(fade_in_end, 100.0, True)
                        return True
                    except Exception:
                        pass
        except Exception:
            pass
        return False

    # ====== Fade-in/out genérico por Opacidade (para qualquer clip de vídeo) ======

    def __animate_opacity_fade_in_out(self, clip, fade_seconds_each: float) -> bool:
        """
        Adiciona keyframes na propriedade Opacidade do CLIP:
          - início: 0%  -> 100% em fade_seconds_each
          - final : 100% -> 0%  em fade_seconds_each
        Faz clamping para clipes muito curtos (<= 2 * fade_seconds_each).
        """
        try:
            start_s = float(clip.inPoint.seconds)
            end_s = float(clip.outPoint.seconds)
            duration = max(0.0, end_s - start_s)

            # clamp: fade não pode ser maior que metade do clipe
            fade = max(0.05, min(float(fade_seconds_each),
                       max(0.0, duration / 2.0 - 0.01)))
            if fade <= 0.05 or duration <= 0.1:
                # clipes muito curtos -> ignora
                return False

            fade_in_end = start_s + fade
            fade_out_ini = end_s - fade

            # acha a propriedade "Opacidade"/"Opacity" e cria keyframes
            for comp in clip.components:
                cname = (getattr(comp, 'displayName', '') or '').lower()
                if cname not in ('opacidade', 'opacity'):
                    continue

                for prop in comp.properties:
                    pn = (getattr(prop, 'displayName', '') or '').lower()
                    if pn not in ('opacidade', 'opacity'):
                        continue

                    if not prop.isTimeVarying():
                        prop.setTimeVarying(True)

                    try:
                        # --- FADE IN ---
                        prop.addKey(start_s)
                        prop.setValueAtKey(start_s, 0.0, True)

                        prop.addKey(fade_in_end)
                        prop.setValueAtKey(fade_in_end, 100.0, True)

                        # --- FADE OUT ---
                        prop.addKey(fade_out_ini)
                        prop.setValueAtKey(fade_out_ini, 100.0, True)

                        prop.addKey(end_s)
                        prop.setValueAtKey(end_s, 0.0, True)

                        return True
                    except Exception:
                        pass
        except Exception:
            pass

        return False

    def apply_fade_to_scene_track_clips(self, fade_percentage: float = 10.0) -> int:
        """
        Aplica fade-in/out (opacidade) em TODOS os clipes da trilha V1/A1
        (cenas COERENTES). NAO cobre V2/A2 (cenas filler/duplicadas).
        Atualmente nao chamada pelo programa - se voltar a ser usada para
        clipes filler, precisa ampliar para FILLER_SCENE_TRACK_INDEX tambem.
        fade_percentage: porcentagem da duração do clipe (ex.: 10%).
        Retorna quantos clipes receberam o efeito.
        """
        try:
            seq = pymiere.objects.app.project.activeSequence
            vtrack = seq.videoTracks[self.SCENE_TRACK_INDEX]
            count = 0
            for clip in list(vtrack.clips):
                try:
                    start_s = float(clip.inPoint.seconds)
                    end_s = float(clip.outPoint.seconds)
                    duration = max(0.0, end_s - start_s)
                    if duration <= 0.1:
                        continue
                    fade_each = max(
                        0.05, (float(fade_percentage) / 100.0) * duration)
                    if self.__animate_opacity_fade_in_out(clip, fade_each):
                        count += 1
                except Exception:
                    pass
            return count
        except Exception:
            return 0

        # ====== CARTELA: insere cartela (preto + graphic de texto) ======
    def __insert_title_card(
        self,
        start_time,                 # pymiere.wrappers time
        duration_secs: float,
        text: str,
        style_name: str,            # ignorado (compat)
        project_item_cache: dict,
        dims_cache: dict,
        zoom_min_scale_multiplier: float,
        zoom_max_scale_multiplier: float,
        roteiro_name: Optional[str] = None,
        cena_index: Optional[int] = None
    ) -> float:
        """
        Gera um MP4 (fundo preto + texto centralizado) via FFmpeg e insere como um único clipe em V1.
        Aplica zoom coerente e fade-out no final. Retorna a duração aplicada.
        O estilo é lido de partes/<roteiro_name>/estilodetexto.prtextstyle (se existir).
        """
        roteiro_key = (roteiro_name or 'default')
        idx = int(cena_index or 0)

        # carrega estilo
        style = self.__load_text_style_from_prtextstyle(roteiro_key)

        # renderiza arquivo de vídeo com o texto
        vid_path = self.__render_titlecard_video(
            roteiro_name=roteiro_key,
            cena_index=idx,
            text=text,
            duration_secs=duration_secs,
            style=style
        )
        if not vid_path or not os.path.exists(vid_path):
            return 0.0

        # importa/resolve ProjectItem com retry (o Premiere pode demorar a indexar)
        item = self.__get_project_item_cached(vid_path, project_item_cache)
        if item == self.PYMIERE_UNDEFINED:
            try:
                pymiere.objects.app.project.importFiles(
                    [vid_path], True, pymiere.objects.app.project.getInsertionBin(), False)
            except Exception:
                pass

            # aguarda até ~5s pela indexação
            start_wait = time.time()
            item = self.PYMIERE_UNDEFINED
            while time.time() - start_wait < 5.0:
                self.__throttle()
                try:
                    item = pymiere.objects.app.project.rootItem.findItemsMatchingMediaPath(
                        vid_path, ignoreSubclips=False)[0]
                except Exception:
                    item = self.PYMIERE_UNDEFINED
                if item != self.PYMIERE_UNDEFINED:
                    break
                time.sleep(0.1)

            # atualiza cache (mesmo se falhar, para evitar buscas repetidas)
            project_item_cache[vid_path] = item
            if item == self.PYMIERE_UNDEFINED:
                return 0.0

        # insere no track de vídeo com pequena espera pelo clipe aparecer
        seq = pymiere.objects.app.project.activeSequence

        clip = self.__insert_clip_with_retry(
            track_type='video',
            track_index=self.SCENE_TRACK_INDEX,
            project_item=item,
            start_time=start_time
        )
        self.__throttle()

        # se você ainda precisar do objeto da trilha:
        v0 = seq.videoTracks[self.SCENE_TRACK_INDEX]

        # aparar se vier maior que o solicitado
        clip_len = clip.end.seconds - clip.start.seconds
        duration_secs = max(0.5, float(duration_secs))
        if clip_len > duration_secs + 1e-6:
            cut_sec = clip.start.seconds + duration_secs
            tc = pymiere.wrappers.timecode_from_seconds(cut_sec, seq)
            self.__qe_razor_with_retry(
                track_type='video', track_index=self.SCENE_TRACK_INDEX, timecode=tc)
            v0.clips[-1].remove(False, True)
            clip = v0.clips[-1]

        # zoom coerente (mesma taxa para todas as cartelas)
        try:
            initial_scale = 100.0
            slope = self.__get_or_set_zoom_slope(
                media_path='__TITLECARD__',
                min_mult=zoom_min_scale_multiplier,
                max_mult=zoom_max_scale_multiplier,
                duration=duration_secs
            )
            max_local = zoom_min_scale_multiplier + slope * duration_secs
            anim = self.__get_scale_calculator(
                initial_scale=initial_scale,
                start_second=clip.start.seconds,
                end_second=clip.end.seconds,
                min_scale_multiplier=zoom_min_scale_multiplier,
                max_scale_multiplier=max_local
            )
            self.__animate_zoom(clip, anim)
        except Exception:
            pass

        # fade-out suave no final
        self.__animate_opacity_fade_out(clip, min(0.7, duration_secs * 0.35))

        return duration_secs
