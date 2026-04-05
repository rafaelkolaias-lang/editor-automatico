import os
import time
import random
from concurrent.futures import ThreadPoolExecutor, Future
import pymiere
import pymiere.wrappers
from typing import Optional, Dict, Any, Callable
from ...entities import EXTENSIONS, Dimensions, Part, Result
from ..TextOnScreenManager import TextOnScreenManager


def _choose_sequence_preset_path(target_w: int, target_h: int) -> str:
    """
    Procura um preset REAL (.sqpreset) dentro da instalação do Premiere,
    sem depender de um nome fixo que pode mudar entre versões.
    """
    presets_root = os.path.join(
        pymiere.objects.app.path,
        'Settings',
        'SequencePresets'
    )

    if not os.path.isdir(presets_root):
        raise FileNotFoundError(
            f'Pasta de presets não encontrada: "{presets_root}"'
        )

    candidates = []

    for root, _, files in os.walk(presets_root):
        for file_name in files:
            if not file_name.lower().endswith('.sqpreset'):
                continue

            full_path = os.path.join(root, file_name)
            searchable = f'{root} {file_name}'.lower()
            score = 0

            # preferência para 1920x1080 em 59.94/60 fps
            if target_w == 1920 and target_h == 1080:
                if '1080' in searchable:
                    score += 100
                if '59.94' in searchable:
                    score += 50
                if '60' in searchable:
                    score += 20
                if 'hd' in searchable:
                    score += 10
            else:
                # tenta achar algo mais próximo da resolução desejada
                if str(target_w) in searchable:
                    score += 60
                if str(target_h) in searchable:
                    score += 60
                if f'{target_h}p' in searchable:
                    score += 20
                if '59.94' in searchable:
                    score += 10

            candidates.append((score, full_path))

    if not candidates:
        raise FileNotFoundError(
            f'Nenhum arquivo .sqpreset foi encontrado em "{presets_root}"'
        )

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][1]


def _apply_sequence_frame_size(sequence, width: int, height: int):
    """
    Ajusta a resolução da sequência depois que ela é criada.
    """
    settings = sequence.getSettings()
    settings.videoFrameWidth = int(width)
    settings.videoFrameHeight = int(height)
    settings.previewFrameWidth = int(width)
    settings.previewFrameHeight = int(height)
    sequence.setSettings(settings)


def ensure_sequence(self, script_name: str):
    """
    Abre (se existir) ou cria (se não existir) uma sequência com nome do roteiro,
    usando a resolução escolhida (FRAME_W/FRAME_H).
    """
    try:
        existing = [
            seq for seq in pymiere.objects.app.project.sequences
            if seq.name == script_name
        ]
        if existing:
            pymiere.objects.app.project.openSequence(
                sequenceID=existing[0].sequenceID
            )
            return
    except Exception:
        pass

    preset_path = _choose_sequence_preset_path(self.FRAME_W, self.FRAME_H)
    print(f'[Premiere] preset escolhido: {preset_path}')

    pymiere.objects.qe.project.newSequence(script_name, preset_path)

    matches = [
        seq for seq in pymiere.objects.app.project.sequences
        if seq.name == script_name
    ]
    if not matches:
        raise RuntimeError(
            f'Sequência "{script_name}" não foi criada pelo Premiere. '
            f'Verifique se o Premiere está aberto e com um projeto ativo. '
            f'Preset usado: "{preset_path}"'
        )
    new_seq = matches[0]

    _apply_sequence_frame_size(new_seq, self.FRAME_W, self.FRAME_H)

    try:
        pymiere.objects.app.project.openSequence(
            sequenceID=new_seq.sequenceID
        )
    except Exception:
        pass


def mount_sequence(
    mgr,
    narrations_files: list[str],
    narration_base_path: str,
    scenes_base_path: str,
    musics_files: list[str],
    musics_base_path: str,
    paths_map: dict[str, str],
    narrations_map: dict[str, list[Part]],
    zoom_min_scale_multiplier: float,
    zoom_max_scale_multiplier: float,
    fade_percentage: float = 10.0,
    apply_fade_immediately: bool = False,
    duplicate_scenes_until_next: bool = True,
    fill_gaps_with_random_scenes: bool = False,
    max_fill_scene_duration: float = 0.0,

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

    project_item_cache = {}
    dims_cache = {}

    # NOVO: mapa arquivo->transcrição (pra casar com a ordem correta)
    transcriptions_by_file: dict[str, Any] = {}
    if narrations_transcriptions and len(narrations_transcriptions) == len(narrations_files):
        for i, nf in enumerate(narrations_files):
            transcriptions_by_file[nf] = narrations_transcriptions[i]

    # NOVO: offsets reais (em segundos) de cada narração na timeline
    narration_offset_by_file: dict[str, float] = {}

    # Resolve caminhos mesmo quando houve: original -> renomeado -> convertido -> renomeado_convertido
    def _resolve_path(p: str) -> str:
        try:
            cur = p
            seen = set()
            while True:
                nxt = paths_map.get(cur, cur)
                if nxt == cur:
                    return cur
                if nxt in seen:
                    return nxt
                seen.add(cur)
                cur = nxt
        except Exception:
            return p

    # Lista de cenas disponíveis (para o modo "Preencher espaço sem cena")
    try:
        _scene_candidates = [
            f for f in os.listdir(scenes_base_path)
            if any(f.lower().endswith(ext.lower()) for ext in EXTENSIONS['VIDEO'])
            or any(f.lower().endswith(ext.lower()) for ext in EXTENSIONS['IMAGE'])
        ]
    except Exception:
        _scene_candidates = []

    # NOVO: coletor de fades por bloco (first_clip, last_clip, fade_each_seconds)
    fade_blocks = []

    # NOVO: coletor de operações de zoom deferidas (aplicadas após o loop principal)
    zoom_jobs = []  # list[tuple[list, list, float, float]]

    last_narration_end = pymiere.wrappers.time_from_seconds(0)

    sorted_narrations_files = sorted(narrations_files)

    # Garante que TODAS as trilhas necessarias existam ANTES de inserir conteudo.
    # addTracks insere no topo e empurra conteudo existente, entao criamos
    # todas de uma vez antes de qualquer clip para evitar deslocamento.
    _max_video_idx = max(
        mgr.SCENE_TRACK_INDEX, mgr.CTA_TRACK_INDEX,
        mgr.IMPACT_TEXT_TRACK_INDEX, mgr.OVERLAY_TRACK_INDEX,
        mgr.LOGO_TRACK_INDEX
    )
    _max_audio_idx = max(
        mgr.SCENE_TRACK_INDEX, mgr.NARRATION_TRACK_INDEX,
        mgr.CTA_AUDIO_TRACK_INDEX, mgr.MUSIC_TRACK_INDEX
    )
    try:
        mgr._PremiereManager__ensure_video_track_index(_max_video_idx)
    except Exception:
        pass
    try:
        mgr._PremiereManager__ensure_audio_track_index(_max_audio_idx)
    except Exception:
        pass

    # ── Fase 1: Pré-processamento assíncrono ──────────────────────────────────
    # Coleta TODOS os paths únicos (narrações + cenas + músicas) e pré-popula os
    # caches ANTES do loop de montagem:
    #   • importFiles em lote → 1 chamada ao invés de N×(importFiles + 5s polling)
    #   • ffprobe em paralelo → ThreadPoolExecutor(5) em vez de sequencial
    _prefetch_paths: list[str] = []
    for _nf in sorted(narrations_files):
        _prefetch_paths.append(_resolve_path(os.path.join(narration_base_path, _nf)))
        for _part in (narrations_map.get(_nf) or []):
            _prefetch_paths.append(_resolve_path(os.path.join(scenes_base_path, _part.text)))
    for _mf in musics_files:
        _prefetch_paths.append(_resolve_path(os.path.join(musics_base_path, _mf)))
    # Inclui overlay, logo e CTA no prefetch para evitar importação duplicada
    for _vp in [overlay_path, logo_path, cta_anim_path]:
        if _vp and os.path.exists(_vp):
            _prefetch_paths.append(_resolve_path(_vp))
    _prefetch_paths = list(dict.fromkeys(p for p in _prefetch_paths if p))
    print(f"[prefetch] pré-processando {len(_prefetch_paths)} arquivos...")
    mgr._PremiereManager__prefetch_all_media(_prefetch_paths, project_item_cache, dims_cache)
    print("[prefetch] concluído — caches prontos.")
    # ─────────────────────────────────────────────────────────────────────────

    # ── Fase 1b: Pré-calcular offsets das narrações (para frases impactantes) ──
    _precalc_offsets: dict[str, float] = {}
    _offset_acc = 0.0
    for _nf in sorted_narrations_files:
        _precalc_offsets[_nf] = _offset_acc
        _narr_path = _resolve_path(os.path.join(narration_base_path, _nf))
        _narr_dur = mgr._PremiereManager__get_video_duration_s(_narr_path)
        if _narr_dur > 0:
            _offset_acc += _narr_dur
        else:
            _offset_acc += 30.0  # fallback seguro
    print(f"[impact] offsets pré-calculados: {len(_precalc_offsets)} narrações, total={_offset_acc:.1f}s")

    # ── Fase 1c: Renderização FFmpeg paralela (overlay + logo + frases) ──────
    # Dispara threads para pré-renderizar overlay (10 min loop) e logo (10 min
    # posicionado) via FFmpeg ENQUANTO as cenas são inseridas na timeline.
    # Se o arquivo já foi renderizado anteriormente, retorna do cache instantaneamente.
    _ffmpeg_overlay_future: Future | None = None
    _ffmpeg_logo_future: Future | None = None
    _ffmpeg_pool: ThreadPoolExecutor | None = None

    if (overlay_path and os.path.exists(overlay_path)) or \
       (logo_path and os.path.exists(logo_path)):
        _ffmpeg_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='ffmpeg')

        if overlay_path and os.path.exists(overlay_path):
            _ffmpeg_overlay_future = _ffmpeg_pool.submit(
                mgr._PremiereManager__extend_video_ffmpeg,
                overlay_path, 600.0
            )
            print("[ffmpeg-thread] overlay: verificando cache/renderizando em paralelo...")

        if logo_path and os.path.exists(logo_path):
            _ffmpeg_logo_future = _ffmpeg_pool.submit(
                mgr._PremiereManager__render_logo_positioned_mp4,
                logo_path, logo_position, 600.0
            )
            print("[ffmpeg-thread] logo: verificando cache/renderizando em paralelo...")

        # Não faz shutdown(wait=True) aqui — as threads rodam em paralelo com
        # a montagem das cenas. O join acontece antes da inserção na timeline.
    # ─────────────────────────────────────────────────────────────────────────

    # ── Fase 1d: Frases impactantes em paralelo (usando offsets pré-calculados) ──
    _impact_future: Future | None = None
    _impact_tos = None
    _impact_pool: ThreadPoolExecutor | None = None

    try:
        cfg = impact_phrases_config or {}
        if isinstance(cfg, dict) and cfg.get("enabled"):
            t_list = []
            off_list = []
            for nf in sorted_narrations_files:
                t = transcriptions_by_file.get(nf)
                off = _precalc_offsets.get(nf, None)
                if t is None or off is None:
                    continue
                t_list.append(t)
                off_list.append(float(off))

            if t_list:
                dims = Dimensions(mgr.FRAME_W, mgr.FRAME_H)
                script_name = os.path.basename(os.path.normpath(narration_base_path))
                out_dir = os.path.join(mgr.CWD, "projeto", script_name, "impact_text")
                os.makedirs(out_dir, exist_ok=True)

                _impact_tos = TextOnScreenManager(openai_api_key=openai_api_key)
                _impact_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='impact')
                _impact_future = _impact_pool.submit(
                    _impact_tos.build_text_overlays,
                    transcriptions=t_list,
                    offsets_seconds=off_list,
                    dims=dims,
                    output_dir=out_dir,
                    mode=str(cfg.get("mode", "phrase")),
                    max_phrases_total=int(cfg.get("max_phrases_total", 5)),
                    min_gap_seconds=float(cfg.get("min_gap_seconds", 8.0)),
                    fps="60000/1001",
                    language="pt-BR",
                    position=str(cfg.get("position", "bottom")),
                    font_name=str(cfg.get("font_name", "")),
                    font_file=str(cfg.get("font_file", "")),
                    font_size_px=cfg.get("font_size_px", None),
                    text_style=cfg.get("text_style", None),
                    use_cache=bool(cfg.get("use_cache", False)),
                )
                print("[impact] seleção + renderização iniciada em paralelo (offsets pré-calculados).")
    except Exception as e:
        print(f"[impact] excepcao ao iniciar: {e}")
    # ─────────────────────────────────────────────────────────────────────────

    with mgr.fast_ops():
        for narration_file in sorted_narrations_files:
            ...
            narration_abs = os.path.join(narration_base_path, narration_file)
            narration_path = _resolve_path(narration_abs)

            imported_narration = mgr._PremiereManager__get_or_import_project_item(
                narration_path, project_item_cache)
            if imported_narration == mgr.PYMIERE_UNDEFINED:
                return Result(
                    success=False,
                    error=f'A narração salva no caminho "{narration_path}" não pôde ser importada.'
                )

            # NOVO: guarda o início real dessa narração na timeline
            try:
                narration_offset_by_file[narration_file] = float(
                    last_narration_end.seconds)
            except Exception:
                narration_offset_by_file[narration_file] = 0.0

            mgr._PremiereManager__insert_clip_with_retry(
                track_type='audio',
                track_index=mgr.NARRATION_TRACK_INDEX,
                project_item=imported_narration,
                start_time=last_narration_end
            )

            narration_clip = pymiere.objects.app.project.activeSequence.audioTracks[
                mgr.NARRATION_TRACK_INDEX].clips[-1]
            if narration_clip.end is None:
                raise Exception(
                    f'Narration clip with path "{narration_path}" has no end')

            narration_transcription_parts = narrations_map.get(narration_file)
            if narration_transcription_parts is None:
                raise Exception(
                    f'Transcription for narration "{narration_file}" not found')

            for part_index, part in enumerate(narration_transcription_parts):
                scene_start = last_narration_end if part_index == 0 else pymiere.wrappers.time_from_seconds(
                    last_narration_end.seconds + part.start / 1000
                )

                scene_abs = os.path.join(scenes_base_path, part.text)
                scene_path = _resolve_path(scene_abs)
                if scene_path is None:
                    raise Exception('Scene path is "None"')

                imported_scene = mgr._PremiereManager__get_or_import_project_item(
                    scene_path, project_item_cache)
                if imported_scene == mgr.PYMIERE_UNDEFINED:
                    return Result(
                        success=False,
                        error=f'A cena salva no caminho "{scene_path}" não pôde ser importada.'
                    )

                scene_dimensions = mgr._PremiereManager__get_scene_dimensions_cached(
                    scene_path, dims_cache)
                current_scene_end = scene_start

                next_scene_start = pymiere.wrappers.time_from_seconds(
                    last_narration_end.seconds +
                    narration_transcription_parts[part_index + 1].start / 1000
                ) if part_index + 1 < len(narration_transcription_parts) else narration_clip.end

                # Counting scenes for adding zoom later
                scenes_repetition_count = 0

                # Scenes insertion
                inserted_scene_dims: list[Dimensions] = []
                inserted_scene_clips: list = []  # referências diretas — evita list(vtrack.clips) O(N)

                def _insert_scene_clip(project_item, media_path: str, start_time, max_dur: float = 0.0):
                    nonlocal current_scene_end, scenes_repetition_count
                    # Captura o retorno: __insert_clip_with_retry já devolve o clipe
                    # inserido via índice O(1) — elimina o fetch clips[-1] separado.
                    current_scene_clip = mgr._PremiereManager__insert_clip_with_retry(
                        track_type='video',
                        track_index=mgr.SCENE_TRACK_INDEX,
                        project_item=project_item,
                        start_time=start_time
                    )

                    scenes_repetition_count += 1

                    if current_scene_clip is None or current_scene_clip.end is None:
                        raise Exception(
                            f'Scene clip with path "{media_path}" has no end')

                    # Limita duracao maxima: SEMPRE corta fisicamente quando excede max_dur
                    # (a otimizacao de "nao cortar" so se aplica entre cenas, nao aqui)
                    if max_dur > 0:
                        clip_dur = current_scene_clip.end.seconds - current_scene_clip.start.seconds
                        actual_max = max_dur + random.uniform(-1.0, 1.0)
                        actual_max = max(2.0, actual_max)
                        if clip_dur > actual_max:
                            cut_at = current_scene_clip.start.seconds + actual_max
                            tc = pymiere.wrappers.timecode_from_seconds(
                                cut_at, pymiere.objects.app.project.activeSequence)
                            mgr._PremiereManager__qe_razor_with_retry(
                                track_type='video', track_index=mgr.SCENE_TRACK_INDEX, timecode=tc)
                            mgr._PremiereManager__qe_razor_with_retry(
                                track_type='audio', track_index=mgr.SCENE_TRACK_INDEX, timecode=tc)
                            vt = pymiere.objects.app.project.activeSequence.videoTracks[
                                mgr.SCENE_TRACK_INDEX]
                            n_razor = len(vt.clips)
                            if n_razor > 0:
                                vt.clips[n_razor - 1].remove(False, False)
                            try:
                                at = pymiere.objects.app.project.activeSequence.audioTracks[
                                    mgr.SCENE_TRACK_INDEX]
                                a_clips = list(at.clips)
                                if a_clips:
                                    last_a = a_clips[-1]
                                    if abs(last_a.start.seconds - cut_at) < 0.15:
                                        last_a.remove(False, False)
                            except Exception:
                                pass
                            n_trim = len(vt.clips)
                            if n_trim > 0:
                                current_scene_clip = vt.clips[n_trim - 1]

                    dims = mgr._PremiereManager__get_scene_dimensions_cached(
                        media_path, dims_cache)

                    inserted_scene_dims.append(dims)
                    inserted_scene_clips.append(current_scene_clip)
                    current_scene_end = current_scene_clip.end

                # Duracao maxima para cenas principais (15-20s aleatorio)
                MAX_MAIN_SCENE_SEC = random.uniform(15.0, 20.0)

                if duplicate_scenes_until_next:
                    while current_scene_end.seconds < next_scene_start.seconds:
                        prev_end = current_scene_end.seconds
                        _insert_scene_clip(
                            imported_scene, scene_path, current_scene_end,
                            max_dur=MAX_MAIN_SCENE_SEC)

                        # trava anti-loop (se o Premiere nao avancar o tempo)
                        if current_scene_end.seconds <= prev_end + 1e-6:
                            break

                else:
                    # Insere apenas 1 clipe (limita a 15-20s)
                    _insert_scene_clip(
                        imported_scene, scene_path, current_scene_end,
                        max_dur=MAX_MAIN_SCENE_SEC)

                    # NOVO: se habilitado, preenche o "buraco" com cenas aleatórias
                    if fill_gaps_with_random_scenes:
                        last_used = part.text
                        MIN_FILL_SEC = 3.0
                        safety = 0
                        while current_scene_end.seconds < next_scene_start.seconds - 1e-6 and safety < 50:
                            # Não adiciona um clipe se o espaço restante for muito curto.
                            # Isso evita criar clipes "picotados" (ex.: 2s) quando sobra pouco tempo até a próxima cena.
                            remaining_gap = next_scene_start.seconds - current_scene_end.seconds
                            if remaining_gap < 3.0:
                                break

                            safety += 1
                            if not _scene_candidates:
                                break

                            candidates = [f for f in _scene_candidates if f != last_used] or list(
                                _scene_candidates)
                            random_file = random.choice(candidates)
                            last_used = random_file

                            rand_abs = os.path.join(
                                scenes_base_path, random_file)
                            rand_path = _resolve_path(rand_abs)

                            imported_rand = mgr._PremiereManager__get_or_import_project_item(
                                rand_path, project_item_cache)
                            if imported_rand == mgr.PYMIERE_UNDEFINED:
                                # tenta outra sem quebrar o projeto inteiro
                                continue

                            prev_end = current_scene_end.seconds
                            prev_rep = scenes_repetition_count
                            prev_dims_len = len(inserted_scene_dims)

                            _insert_scene_clip(
                                imported_rand, rand_path, current_scene_end,
                                max_dur=max_fill_scene_duration)

                            # Se o Premiere não avançar o tempo, trava anti-loop
                            if current_scene_end.seconds <= prev_end + 1e-6:
                                break

                            # --- REGRA 1: nao aceita clipe menor que o minimo (3s) ---
                            # Usa inserted_scene_clips ao inves de list(track.clips) para evitar IPC O(N)
                            last_clip_tmp = inserted_scene_clips[-1] if inserted_scene_clips else None
                            if last_clip_tmp is not None:
                                last_len_tmp = last_clip_tmp.end.seconds - last_clip_tmp.start.seconds
                                if last_len_tmp + 1e-6 < MIN_FILL_SEC:
                                    last_clip_tmp.remove(False, False)
                                    scenes_repetition_count = prev_rep
                                    del inserted_scene_dims[prev_dims_len:]
                                    del inserted_scene_clips[prev_dims_len:]
                                    current_scene_end = pymiere.wrappers.time_from_seconds(
                                        prev_end)
                                    continue

                            # --- REGRA 2: evita sobrar "restinho" preto (< 3s) ---
                            remaining_after = next_scene_start.seconds - current_scene_end.seconds
                            if 0 < remaining_after < MIN_FILL_SEC - 1e-6:
                                delta = MIN_FILL_SEC - remaining_after
                                last_clip_tmp = inserted_scene_clips[-1] if inserted_scene_clips else None

                                if last_clip_tmp is not None:
                                    last_len_tmp = last_clip_tmp.end.seconds - last_clip_tmp.start.seconds

                                    if (last_len_tmp - delta) >= MIN_FILL_SEC - 1e-6:
                                        new_end_sec = last_clip_tmp.end.seconds - delta
                                        tc = pymiere.wrappers.timecode_from_seconds(
                                            new_end_sec, pymiere.objects.app.project.activeSequence
                                        )
                                        mgr._PremiereManager__qe_razor_with_retry(
                                            track_type='video',
                                            track_index=mgr.SCENE_TRACK_INDEX,
                                            timecode=tc
                                        )

                                        # remove a parte da direita e atualiza referencia (1 leitura unica)
                                        vtrack_tmp = pymiere.objects.app.project.activeSequence.videoTracks[
                                            mgr.SCENE_TRACK_INDEX]
                                        n_tmp = len(vtrack_tmp.clips)
                                        if n_tmp > 0:
                                            vtrack_tmp.clips[n_tmp - 1].remove(False, False)
                                        # atualiza referencia do clip que ficou
                                        n_after = len(vtrack_tmp.clips)
                                        if n_after > 0:
                                            kept = vtrack_tmp.clips[n_after - 1]
                                            current_scene_end = kept.end
                                            if inserted_scene_clips:
                                                inserted_scene_clips[-1] = kept
                                    else:
                                        last_clip_tmp.remove(False, False)
                                        scenes_repetition_count = prev_rep
                                        del inserted_scene_dims[prev_dims_len:]
                                        del inserted_scene_clips[prev_dims_len:]
                                        current_scene_end = pymiere.wrappers.time_from_seconds(
                                            prev_end)
                                        continue

                # Corte de cena excedente
                # Modo rapido (duplicar/gaps habilitado):
                #   VIDEO: nao corta - a proxima cena sobrescreve automaticamente (overwriteClip)
                #          so corta na ultima cena para alinhar com fim da narracao
                #   AUDIO: corta apenas se a cena TEM audio (overwrite nao afeta trilha de audio)
                # Modo normal (ambos desabilitados):
                #   Corta tudo (video + audio) como antes
                _modo_rapido = duplicate_scenes_until_next or fill_gaps_with_random_scenes
                is_last_part = (part_index + 1 >= len(narration_transcription_parts))

                if current_scene_end.seconds > next_scene_start.seconds:
                    timecode_to_cut = pymiere.wrappers.timecode_from_time(
                        next_scene_start,
                        pymiere.objects.app.project.activeSequence
                    )

                    # VIDEO: corta se modo normal OU se for a ultima cena
                    if not _modo_rapido or is_last_part:
                        mgr._PremiereManager__qe_razor_with_retry(
                            track_type='video', track_index=mgr.SCENE_TRACK_INDEX, timecode=timecode_to_cut)

                        video_clips = pymiere.objects.app.project.activeSequence.videoTracks[
                            mgr.SCENE_TRACK_INDEX].clips
                        if len(video_clips) > 0:
                            video_clips[-1].remove(False, False)

                        if inserted_scene_clips:
                            try:
                                vt_post = pymiere.objects.app.project.activeSequence.videoTracks[
                                    mgr.SCENE_TRACK_INDEX]
                                n_post = len(vt_post.clips)
                                if n_post > 0:
                                    inserted_scene_clips[-1] = vt_post.clips[n_post - 1]
                            except Exception:
                                pass

                    # AUDIO: corta apenas se a cena tem audio na trilha A0
                    try:
                        audio_clips = pymiere.objects.app.project.activeSequence.audioTracks[
                            mgr.SCENE_TRACK_INDEX].clips
                        if len(audio_clips) > 0:
                            last_audio = audio_clips[-1]
                            # So corta se o audio realmente se estende alem do ponto
                            if last_audio.end.seconds > next_scene_start.seconds + 0.1:
                                mgr._PremiereManager__qe_razor_with_retry(
                                    track_type='audio', track_index=mgr.SCENE_TRACK_INDEX, timecode=timecode_to_cut)
                                audio_clips = pymiere.objects.app.project.activeSequence.audioTracks[
                                    mgr.SCENE_TRACK_INDEX].clips
                                if len(audio_clips) > 0:
                                    last_a = audio_clips[-1]
                                    if abs(last_a.start.seconds - next_scene_start.seconds) < 0.15:
                                        last_a.remove(False, False)
                    except Exception:
                        pass

            #     # Add zoom effect
                # Usa inserted_scene_clips (já populado por _insert_scene_clip) em vez de
                # list(vtrack.clips) — elimina O(N) IPC calls por parte de narração.
                n = min(int(scenes_repetition_count), len(inserted_scene_clips))
                recent_clips = inserted_scene_clips[-n:] if n > 0 else []
                dims_for_zoom = inserted_scene_dims[-n:] if n > 0 else []

                if not recent_clips:
                    raise Exception('Nenhum clipe de cena para aplicar zoom')

                zoom_start_clip = recent_clips[0]
                zoom_end_clip = recent_clips[-1]

                start_point = zoom_start_clip.start.seconds
                end_point = zoom_end_clip.end.seconds

                # Fase 2: defer zoom — acumula para aplicar após o loop principal,
                # evitando GUI redraws intercalados com inserções de clipes.
                zoom_jobs.append((recent_clips, dims_for_zoom, start_point, end_point))

                # >>> FADES POR BLOCO (não por clipe)
                if fade_percentage > 0 and scenes_repetition_count > 0:
                    block_dur = max(0.0, end_point - start_point)
                    if block_dur > 0.1:
                        fade_each = max(0.05, min(block_dur / 2.0 - 0.01,
                                                  (float(fade_percentage) / 100.0) * block_dur))
                        first_c = zoom_start_clip
                        last_c = zoom_end_clip

                        if apply_fade_immediately:
                            # aplica agora nas bordas do bloco
                            if first_c == last_c:
                                one_len = max(
                                    0.0, first_c.outPoint.seconds - first_c.inPoint.seconds)
                                safe_each = max(
                                    0.05, min(fade_each, max(0.0, one_len / 2.0 - 0.01)))
                                mgr._PremiereManager__animate_opacity_fade_in_out(
                                    first_c, safe_each)
                            else:
                                # fade-in no primeiro
                                first_len = max(
                                    0.0, first_c.outPoint.seconds - first_c.inPoint.seconds)
                                fin = max(
                                    0.05, min(fade_each, max(0.0, first_len - 0.01)))
                                if fin > 0.05:
                                    mgr._PremiereManager__animate_opacity_fade_in(
                                        first_c, fin)

                                # fade-out no último
                                last_len = max(
                                    0.0, last_c.outPoint.seconds - last_c.inPoint.seconds)
                                fout = max(
                                    0.05, min(fade_each, max(0.0, last_len - 0.01)))
                                if fout > 0.05:
                                    mgr._PremiereManager__animate_opacity_fade_out(
                                        last_c, fout)
                        else:
                            # guarda para aplicar no final
                            fade_blocks.append((first_c, last_c, fade_each))

            last_narration_end = narration_clip.end

    # ── Fase 2: aplicação de zoom em lote (após todas as inserções) ───────────
    # Todas as referências de clipe em zoom_jobs ainda são válidas no Premiere.
    # Aplicar após o loop evita GUI redraws intercalados com os insertClip(),
    # que é a maior fonte de lentidão progressiva na timeline.
    with mgr.fast_ops():
        for _clips, _dims, _sp, _ep in zoom_jobs:
            for _clip, _dim in zip(_clips, _dims):
                _initial_scale = mgr._PremiereManager__get_new_initial_scale(_dim)
                mgr._PremiereManager__animate_zoom(
                    clip=_clip,
                    animation_fn=mgr._PremiereManager__get_scale_calculator(
                        initial_scale=_initial_scale,
                        start_second=_sp,
                        end_second=_ep,
                        min_scale_multiplier=zoom_min_scale_multiplier,
                        max_scale_multiplier=zoom_max_scale_multiplier,
                    ),
                )
    # ─────────────────────────────────────────────────────────────────────────

    last_music_end = pymiere.wrappers.time_from_seconds(0)
    music_count = 0

    # Musics insertion
    with mgr.fast_ops():
        while last_music_end.seconds < last_narration_end.seconds:
            music_abs = os.path.join(
                musics_base_path, musics_files[music_count % len(musics_files)])
            music_path = _resolve_path(music_abs)
            imported_music = mgr._PremiereManager__find_item_with_retry(
                music_path)
            if imported_music == mgr.PYMIERE_UNDEFINED:
                return Result(
                    success=False,
                    error=f'A música salva no caminho "{music_path}" não pôde ser importada.'
                )

            mgr._PremiereManager__insert_clip_with_retry(
                track_type='audio',
                track_index=mgr.MUSIC_TRACK_INDEX,
                project_item=imported_music,
                start_time=last_music_end
            )
            mgr._PremiereManager__throttle()

            music_clip = pymiere.objects.app.project.activeSequence.audioTracks[
                mgr.MUSIC_TRACK_INDEX].clips[-1]
            if music_clip.end is None:
                raise Exception(
                    f'Music clip with path "{music_path}" has no end')

            last_music_end = music_clip.end
            music_count += 1

    # Cut remaining music
    if last_music_end.seconds > last_narration_end.seconds:
        timecode = pymiere.wrappers.timecode_from_seconds(
            last_narration_end.seconds, pymiere.objects.app.project.activeSequence)

        mgr._PremiereManager__qe_razor_with_retry(
            track_type='audio', track_index=mgr.MUSIC_TRACK_INDEX, timecode=timecode)

        pymiere.objects.app.project.activeSequence.audioTracks[mgr.MUSIC_TRACK_INDEX].clips[-1].remove(
            False, True)

    # Volume da trilha de cenas sera aplicado no bloco MIXER abaixo

    # ==========================
    # OVERLAY, LOGO, CTA (dentro de fast_ops para velocidade)
    # Roda em paralelo com a renderização das frases impactantes
    # ==========================

    # Aguarda threads FFmpeg de overlay/logo (se ainda estiverem rodando)
    _prerendered_overlay = ''
    _prerendered_logo = ''

    if _ffmpeg_overlay_future is not None:
        try:
            print("[ffmpeg-thread] aguardando overlay...")
            _prerendered_overlay = _ffmpeg_overlay_future.result(timeout=300) or ''
            print(f"[ffmpeg-thread] overlay pronto: {_prerendered_overlay}")
        except Exception as e:
            print(f"[ffmpeg-thread] erro no overlay: {e}")

    if _ffmpeg_logo_future is not None:
        try:
            print("[ffmpeg-thread] aguardando logo...")
            _prerendered_logo = _ffmpeg_logo_future.result(timeout=300) or ''
            print(f"[ffmpeg-thread] logo pronto: {_prerendered_logo}")
        except Exception as e:
            print(f"[ffmpeg-thread] erro no logo: {e}")

    if _ffmpeg_pool is not None:
        _ffmpeg_pool.shutdown(wait=False)

    with mgr.fast_ops():
        if overlay_path and os.path.exists(overlay_path):
            try:
                script_name = os.path.basename(os.path.normpath(narration_base_path))
                mgr._PremiereManager__insert_overlay_full(
                    roteiro_name=script_name,
                    seq_end_time=last_narration_end,
                    paths_map=paths_map,
                    project_item_cache=project_item_cache,
                    overlay_path_override=overlay_path,
                    prerendered_overlay_path=_prerendered_overlay
                )
            except Exception as e:
                print(f"[overlay] erro: {e}")

        if logo_path and os.path.exists(logo_path):
            try:
                script_name = os.path.basename(os.path.normpath(narration_base_path))
                mgr._PremiereManager__insert_logo_full(
                    logo_path=logo_path,
                    logo_position=logo_position,
                    seq_end_time=last_narration_end,
                    paths_map=paths_map,
                    project_item_cache=project_item_cache,
                    dims_cache=dims_cache,
                    roteiro_name=script_name,
                    prerendered_logo_path=_prerendered_logo
                )
            except Exception as e:
                print(f"[logo] erro: {e}")

    # ==========================
    # CTA INSCREVA-SE
    # ==========================
    if cta_enabled and not cta_anim_path:
        print("[cta] AVISO: CTA ativado mas nenhum arquivo de animacao foi selecionado.")
    if cta_enabled and cta_anim_path and not os.path.exists(cta_anim_path):
        print(f"[cta] AVISO: arquivo nao encontrado: {cta_anim_path}")
    if cta_enabled and cta_anim_path and os.path.exists(cta_anim_path):
        try:
            _insert_cta(
                mgr=mgr,
                cta_anim_path=cta_anim_path,
                cta_chroma_key=cta_chroma_key,
                transcriptions_by_file=transcriptions_by_file,
                narration_offset_by_file=narration_offset_by_file,
                sorted_narrations_files=sorted_narrations_files,
                seq_end_seconds=last_narration_end.seconds,
                paths_map=paths_map,
                project_item_cache=project_item_cache,
            )
        except Exception as e:
            print(f"[cta] erro: {e}")

    # ==========================
    # FRASES IMPACTANTES — Fase 2: Coleta dos .mov e inserção no Premiere
    # ==========================
    if _impact_future is not None:
        try:
            print("[impact] aguardando renderização FFmpeg...")
            build_res = _impact_future.result(timeout=600)
            if build_res.success and build_res.data:
                print(f"[impact] overlays gerados: {len(build_res.data)}")
                try:
                    mgr._PremiereManager__ensure_video_track_index(mgr.IMPACT_TEXT_TRACK_INDEX)
                except Exception:
                    pass
                with mgr.fast_ops():
                    ins_res = _impact_tos.insert_overlays_into_premiere(
                        premiere_mgr=mgr,
                        overlays=build_res.data,
                        track_index=mgr.IMPACT_TEXT_TRACK_INDEX
                    )
                if ins_res.success is False:
                    print("[impact] erro ao inserir overlays:", ins_res.error)
            elif build_res.success is False:
                print("[impact] erro ao gerar overlays:", build_res.error)
        except Exception as e:
            print(f"[impact] excepcao: {e}")

    if _impact_pool is not None:
        _impact_pool.shutdown(wait=False)

    # ==========================
    # MIXER DE AUDIO (aplicar volumes em dB)
    # ==========================
    try:
        print(f"[mixer] Aplicando volumes: cenas={vol_scene_db}dB, narracao={vol_narration_db}dB, cta={vol_cta_db}dB, musica={vol_music_db}dB")
        mgr._PremiereManager__set_audio_track_volume_db(mgr.SCENE_TRACK_INDEX, vol_scene_db)
        mgr._PremiereManager__set_audio_track_volume_db(mgr.NARRATION_TRACK_INDEX, vol_narration_db)
        mgr._PremiereManager__set_audio_track_volume_db(mgr.CTA_AUDIO_TRACK_INDEX, vol_cta_db)
        mgr._PremiereManager__set_audio_track_volume_db(mgr.MUSIC_TRACK_INDEX, vol_music_db)
    except Exception as e:
        print(f"[mixer] erro: {e}")

    # >>> APLICAR FADE POR BLOCO (somente se NAO for imediato) <<<
    if not apply_fade_immediately and fade_percentage > 0:
        for (first_c, last_c, fade_each) in fade_blocks:
            try:
                if first_c == last_c:
                    one_len = max(0.0, first_c.outPoint.seconds -
                                  first_c.inPoint.seconds)
                    safe_each = max(
                        0.05, min(fade_each, max(0.0, one_len / 2.0 - 0.01)))
                    mgr._PremiereManager__animate_opacity_fade_in_out(
                        first_c, safe_each)
                else:
                    first_len = max(
                        0.0, first_c.outPoint.seconds - first_c.inPoint.seconds)
                    last_len = max(0.0, last_c.outPoint.seconds -
                                   last_c.inPoint.seconds)
                    fin = max(0.05, min(fade_each, max(0.0, first_len - 0.01)))
                    fout = max(0.05, min(fade_each, max(0.0, last_len - 0.01)))

                    if fin > 0.05:
                        mgr._PremiereManager__animate_opacity_fade_in(
                            first_c, fin)
                    if fout > 0.05:
                        mgr._PremiereManager__animate_opacity_fade_out(
                            last_c, fout)
            except Exception:
                pass

    return Result(success=True)


def _insert_cta(
    mgr,
    cta_anim_path: str,
    cta_chroma_key: bool,
    transcriptions_by_file: dict,
    narration_offset_by_file: dict,
    sorted_narrations_files: list,
    seq_end_seconds: float,
    paths_map: dict,
    project_item_cache: dict,
):
    """
    Detecta pedido de inscricao na transcricao e insere animacao CTA.
    Se nao encontrar, insere em ponto aleatorio nos primeiros 3 minutos.
    """
    CTA_KEYWORDS = ('inscreva', 'inscrev', 'gostei', 'like', 'curtir', 'curta', 'canal')

    # Busca timestamp do CTA na transcricao
    cta_time_s = None
    for nf in sorted_narrations_files:
        t = transcriptions_by_file.get(nf)
        offset = narration_offset_by_file.get(nf, 0.0)
        if t is None:
            continue
        for word in (t.words if hasattr(t, 'words') else []):
            text_lower = (word.text or '').lower()
            for kw in CTA_KEYWORDS:
                if kw in text_lower:
                    cta_time_s = offset + (word.start / 1000.0)
                    break
            if cta_time_s is not None:
                break
        if cta_time_s is not None:
            break

    # Se nao encontrou, ponto aleatorio nos primeiros 3 min
    if cta_time_s is None:
        max_cta = min(180.0, seq_end_seconds * 0.8)
        if max_cta > 10.0:
            cta_time_s = random.uniform(10.0, max_cta)
        else:
            cta_time_s = 5.0

    print(f"[cta] inserindo em {cta_time_s:.1f}s")

    # Garante trilhas
    mgr._PremiereManager__ensure_video_track_index(mgr.CTA_TRACK_INDEX)
    mgr._PremiereManager__ensure_audio_track_index(mgr.CTA_AUDIO_TRACK_INDEX)

    # Converte chroma key verde para canal alfa via FFmpeg (se ativado)
    if cta_chroma_key:
        cta_alpha_path = mgr._PremiereManager__chromakey_to_alpha(
            os.path.normpath(cta_anim_path), key_color_hex="#00FF00")
    else:
        cta_alpha_path = os.path.normpath(cta_anim_path)

    cta_src = paths_map.get(cta_alpha_path, cta_alpha_path)
    imported = mgr._PremiereManager__get_or_import_project_item(
        cta_src, project_item_cache)
    if imported == mgr.PYMIERE_UNDEFINED:
        print("[cta] nao foi possivel importar a animacao CTA.")
        return

    start_time = pymiere.wrappers.time_from_seconds(cta_time_s)

    # Insere video na V3 (o Premiere automaticamente linka o audio na trilha correspondente)
    mgr._PremiereManager__insert_clip_with_retry(
        track_type='video',
        track_index=mgr.CTA_TRACK_INDEX,
        project_item=imported,
        start_time=start_time
    )

    print(f"[cta] animacao inserida em V{mgr.CTA_TRACK_INDEX + 1} (com alfa)")


def mount_mass_project(
    mgr,
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

    def _open_or_create_sequence(sequence_name: str):
        ensure_sequence(mgr, sequence_name)

    try:
        roteiros = mass_structure.get('roteiros', [])
        project_item_cache: dict[str, object] = {}
        dims_cache: dict[str, Dimensions] = {}

        # <<< NOVO: cole aqui >>>
        fade_blocks = []  # [(first_clip, last_clip, fade_each_seconds)]

        if not roteiros:
            return Result(success=False, error='Estrutura de "partes" vazia (nenhum roteiro encontrado).')

        with mgr.fast_ops():
            for roteiro in roteiros:
                roteiro_name = roteiro.get('name')
                _open_or_create_sequence(roteiro_name)

                # info.txt — uma linha por cena
                info_lines = mgr._PremiereManager__read_info_lines_for_roteiro(
                    roteiro_name)

                current_end = pymiere.wrappers.time_from_seconds(0)

                cenas = roteiro.get('cenas', [])
                for cena_idx, cena in enumerate(cenas):
                    cena_start = current_end
                    cena_end = cena_start

                    # ------------------ ÁUDIOS ------------------
                    audios = cena.get('audios', [])
                    for audio_abs_path in audios:
                        audio_path = paths_map.get(
                            audio_abs_path, audio_abs_path)

                        imported_audio = mgr._PremiereManager__get_project_item_cached(
                            audio_path, project_item_cache)
                        if imported_audio == mgr.PYMIERE_UNDEFINED:
                            return Result(success=False, error=f'A narração da cena "{cena.get("name")}" não pôde ser importada: {audio_path}')

                        mgr._PremiereManager__insert_clip_with_retry(
                            track_type='audio',
                            track_index=mgr.NARRATION_TRACK_INDEX,
                            project_item=imported_audio,
                            start_time=current_end
                        )

                        mgr._PremiereManager__throttle()
                        audio_clip = pymiere.objects.app.project.activeSequence.audioTracks[
                            mgr.NARRATION_TRACK_INDEX].clips[-1]
                        if audio_clip.end is None:
                            raise Exception(
                                f'Audio clip sem "end": {audio_path}')

                        current_end = audio_clip.end
                        cena_end = current_end

                    # ---------- CARTELA ----------
                    title_text = ''
                    if info_lines and cena_idx < len(info_lines):
                        title_text = (info_lines[cena_idx] or '').strip()

                    if title_text:
                        wrap_cols = mgr._PremiereManager__get_wrap_max_chars(
                            roteiro_name, default=36)
                        wrapped_text = mgr._PremiereManager__wrap_text(
                            title_text, max_chars=wrap_cols)

                        style_dur = mgr._PremiereManager__get_titlecard_duration(
                            roteiro_name, default=None)
                        if titlecard_seconds is None:
                            title_seconds = float(
                                style_dur if style_dur and style_dur > 0 else 3.0)
                        else:
                            title_seconds = float(titlecard_seconds)

                        added = mgr._PremiereManager__insert_title_card(
                            start_time=cena_start,
                            duration_secs=title_seconds,
                            text=wrapped_text,
                            style_name='(ignored)',
                            project_item_cache=project_item_cache,
                            dims_cache=dims_cache,
                            zoom_min_scale_multiplier=zoom_min_scale_multiplier,
                            zoom_max_scale_multiplier=zoom_max_scale_multiplier,
                            roteiro_name=roteiro_name,
                            cena_index=cena_idx
                        )

                        if added <= 0.0:
                            return Result(
                                success=False,
                                error=('Falha ao inserir a cartela na timeline. '
                                       'Verifique os logs em assets/titlecards/{roteiro_name}.')
                            )

                        cena_start = pymiere.wrappers.time_from_seconds(
                            cena_start.seconds + added)

                    # ------------------ MÍDIAS ------------------
                    medias = cena.get('medias', [])
                    medias_list = list(medias)
                    if order_mode == 'random':
                        random.shuffle(medias_list)

                    if len(medias_list) > 0 and cena_end.seconds > cena_start.seconds:
                        visual_time = cena_start
                        media_index = 0

                        while visual_time.seconds < cena_end.seconds:
                            remaining_part = max(
                                0.0, cena_end.seconds - visual_time.seconds)
                            if remaining_part <= 1e-3:
                                break

                            desired = random.uniform(
                                float(min_scene_seconds), float(max_scene_seconds))
                            target_len = min(desired, remaining_part)
                            if target_len <= 0.0:
                                break

                            vtrack = pymiere.objects.app.project.activeSequence.videoTracks[
                                mgr.SCENE_TRACK_INDEX]

                            media_abs_path = medias_list[media_index % len(
                                medias_list)]
                            media_index += 1
                            media_path = paths_map.get(
                                media_abs_path, media_abs_path)

                            imported_scene = mgr._PremiereManager__get_project_item_cached(
                                media_path, project_item_cache)
                            if imported_scene == mgr.PYMIERE_UNDEFINED:
                                return Result(success=False, error=f'A mídia da cena "{cena.get("name")}" não pôde ser importada: {media_path}')

                            block_start_sec = visual_time.seconds
                            inserted_clips = []

                            mgr._PremiereManager__insert_clip_with_retry(
                                track_type='video',
                                track_index=mgr.SCENE_TRACK_INDEX,
                                project_item=imported_scene,
                                start_time=visual_time
                            )
                            vtrack = pymiere.objects.app.project.activeSequence.videoTracks[
                                mgr.SCENE_TRACK_INDEX]

                            mgr._PremiereManager__throttle()
                            scene_clip = vtrack.clips[-1]
                            inserted_clips.append(scene_clip)

                            clip_len = max(
                                0.0, scene_clip.end.seconds - scene_clip.start.seconds)
                            is_img = mgr._PremiereManager__is_image(media_path)
                            is_vid = mgr._PremiereManager__is_video(media_path)

                            if is_img:
                                need = target_len - clip_len
                                if need > 1e-3:
                                    grew = False
                                    try:
                                        new_out_sec = scene_clip.start.seconds + target_len
                                        tc = pymiere.wrappers.timecode_from_seconds(
                                            new_out_sec, pymiere.objects.app.project.activeSequence)
                                        scene_clip.setOutPoint(tc.ticks, True)
                                        mgr._PremiereManager__throttle()
                                        grew = True
                                        scene_clip = vtrack.clips[-1]
                                        inserted_clips[-1] = scene_clip
                                    except Exception:
                                        grew = False

                                    if not grew:
                                        remain = need
                                        while remain > 1e-3:
                                            mgr._PremiereManager__insert_clip_with_retry(
                                                track_type='video',
                                                track_index=mgr.SCENE_TRACK_INDEX,
                                                project_item=imported_scene,
                                                start_time=vtrack.clips[-1].end
                                            )
                                            mgr._PremiereManager__throttle()
                                            last = vtrack.clips[-1]
                                            inserted_clips.append(last)

                                            # try:
                                            #     mgr._PremiereManager__normalize_clip_scale(
                                            #         last,
                                            #         mgr._PremiereManager__get_scene_dimensions_cached(media_path, dims_cache)
                                            #     )
                                            # except Exception:
                                            #     pass

                                            last_len = last.end.seconds - last.start.seconds
                                            use_len = min(remain, last_len)

                                            if last_len > use_len + 1e-6:
                                                cut_sec = last.start.seconds + use_len
                                                tc = pymiere.wrappers.timecode_from_seconds(
                                                    cut_sec, pymiere.objects.app.project.activeSequence)
                                                mgr._PremiereManager__qe_razor_with_retry(
                                                    track_type='video', track_index=mgr.SCENE_TRACK_INDEX, timecode=tc)
                                                vtrack.clips[-1].remove(
                                                    False, True)
                                                last = vtrack.clips[-1]
                                                inserted_clips[-1] = last

                                            remain -= use_len

                            elif is_vid and clip_len + 1e-3 < target_len:
                                desired_speed = max(
                                    5.0, min(100.0, 100.0 * (clip_len / target_len)))
                                sped = mgr._PremiereManager__try_set_speed(
                                    scene_clip, desired_speed)
                                if sped:
                                    time.sleep(0.05)
                                    mgr._PremiereManager__throttle()
                                    scene_clip = vtrack.clips[-1]
                                    inserted_clips[-1] = scene_clip
                                    clip_len = max(
                                        0.0, scene_clip.end.seconds - scene_clip.start.seconds)

                            current_block_len = vtrack.clips[-1].end.seconds - \
                                inserted_clips[0].start.seconds
                            if current_block_len > target_len + 1e-6:
                                cut_sec = inserted_clips[0].start.seconds + \
                                    target_len
                                tc = pymiere.wrappers.timecode_from_seconds(
                                    cut_sec, pymiere.objects.app.project.activeSequence)
                                mgr._PremiereManager__qe_razor_with_retry(
                                    track_type='video', track_index=mgr.SCENE_TRACK_INDEX, timecode=tc)
                                vtrack.clips[-1].remove(False, True)
                                inserted_clips[-1] = vtrack.clips[-1]

                            block_end_sec = vtrack.clips[-1].end.seconds

                            try:
                                scene_dimensions = mgr._PremiereManager__get_scene_dimensions_cached(
                                    media_path, dims_cache)
                                initial_scale = mgr._PremiereManager__get_new_initial_scale(
                                    scene_dimensions)

                                slope = mgr._PremiereManager__get_or_set_zoom_slope(
                                    media_path=media_path,
                                    min_mult=zoom_min_scale_multiplier,
                                    max_mult=zoom_max_scale_multiplier,
                                    duration=max(
                                        0.001, block_end_sec - block_start_sec)
                                )

                                max_local = zoom_min_scale_multiplier + slope * \
                                    max(0.0, block_end_sec - block_start_sec)

                                anim = mgr._PremiereManager__get_scale_calculator(
                                    initial_scale=initial_scale,
                                    start_second=block_start_sec,
                                    end_second=block_end_sec,
                                    min_scale_multiplier=zoom_min_scale_multiplier,
                                    max_scale_multiplier=max_local
                                )

                                # 1) zoom contínuo em todos do bloco (mantém seu comportamento atual)
                                for c in inserted_clips:
                                    mgr._PremiereManager__animate_zoom(c, anim)

                                # 2) FADES POR BLOCO (não por clipe)
                                if fade_percentage > 0 and inserted_clips:
                                    block_dur = max(
                                        0.0, block_end_sec - block_start_sec)
                                    if block_dur > 0.1:
                                        # fade com base na duração do BLOCO
                                        fade_each = max(0.05, min(
                                            block_dur / 2.0 - 0.01, (float(fade_percentage) / 100.0) * block_dur))
                                        first_c = inserted_clips[0]
                                        last_c = inserted_clips[-1]

                                        if apply_fade_immediately:
                                            # aplica AGORA nas bordas
                                            if first_c == last_c:
                                                # bloco de 1 clipe só → in & out no mesmo clipe
                                                one_len = max(
                                                    0.0, first_c.outPoint.seconds - first_c.inPoint.seconds)
                                                safe_each = max(
                                                    0.05, min(fade_each, max(0.0, one_len / 2.0 - 0.01)))
                                                mgr._PremiereManager__animate_opacity_fade_in_out(
                                                    first_c, safe_each)
                                            else:
                                                # fade-in no primeiro clipe (clamp se ele for curtinho)
                                                first_len = max(
                                                    0.0, first_c.outPoint.seconds - first_c.inPoint.seconds)
                                                fin = max(
                                                    0.05, min(fade_each, max(0.0, first_len - 0.01)))
                                                if fin > 0.05:
                                                    mgr._PremiereManager__animate_opacity_fade_in(
                                                        first_c, fin)

                                                # fade-out no último clipe (clamp se ele for curtinho)
                                                last_len = max(
                                                    0.0, last_c.outPoint.seconds - last_c.inPoint.seconds)
                                                fout = max(
                                                    0.05, min(fade_each, max(0.0, last_len - 0.01)))
                                                if fout > 0.05:
                                                    mgr._PremiereManager__animate_opacity_fade_out(
                                                        last_c, fout)
                                        else:
                                            # guarda para aplicar ao final (varredura por BLOCOS)
                                            fade_blocks.append(
                                                (first_c, last_c, fade_each))

                            except Exception:
                                pass

                            prev_sec = visual_time.seconds
                            visual_time = vtrack.clips[-1].end
                            if visual_time.seconds <= prev_sec + 1e-6:
                                break

                    # ------------------ MÚSICA ------------------
                    seq_total_end = current_end
                    if len(musics_files) > 0 and seq_total_end.seconds > 0:
                        last_music_end = pymiere.wrappers.time_from_seconds(0)
                        mi = 0

                        while last_music_end.seconds < seq_total_end.seconds:
                            remaining = seq_total_end.seconds - last_music_end.seconds
                            if remaining <= 0:
                                break

                            music_file = musics_files[mi % len(musics_files)]
                            music_abs = os.path.join(
                                musics_base_path, music_file)
                            music_path = paths_map.get(music_abs, music_abs)

                            imported_music = mgr._PremiereManager__get_project_item_cached(
                                music_path, project_item_cache)

                            if imported_music == mgr.PYMIERE_UNDEFINED:
                                return Result(success=False, error=f'A música não pôde ser importada: {music_path}')

                            mgr._PremiereManager__insert_clip_with_retry(
                                track_type='audio',
                                track_index=mgr.MUSIC_TRACK_INDEX,
                                project_item=imported_music,
                                start_time=last_music_end
                            )

                            mgr._PremiereManager__throttle()
                            music_clip = pymiere.objects.app.project.activeSequence.audioTracks[
                                mgr.MUSIC_TRACK_INDEX].clips[-1]
                            if music_clip.end is None:
                                raise Exception(
                                    f'Music clip sem "end": {music_path}')

                            if music_clip.end.seconds > seq_total_end.seconds:
                                timecode_to_cut = pymiere.wrappers.timecode_from_seconds(
                                    seq_total_end.seconds,
                                    pymiere.objects.app.project.activeSequence
                                )

                                mgr._PremiereManager__qe_razor_with_retry(track_type='audio',
                                                                          track_index=mgr.MUSIC_TRACK_INDEX,
                                                                          timecode=timecode_to_cut)

                                pymiere.objects.app.project.activeSequence.audioTracks[mgr.MUSIC_TRACK_INDEX] \
                                    .clips[-1].remove(False, True)

                                last_music_end = seq_total_end
                                break

                            last_music_end = music_clip.end
                            mi += 1

                    # Volume da trilha de cenas aplicado no bloco MIXER

                    # --- OVERLAY e LOGO ---
                    try:
                        mgr._PremiereManager__clear_video_track_range(
                            mgr.OVERLAY_TRACK_INDEX, 0.0, seq_total_end.seconds)
                        mgr._PremiereManager__insert_overlay_full(
                            roteiro_name=roteiro_name,
                            seq_end_time=seq_total_end,
                            paths_map=paths_map,
                            project_item_cache=project_item_cache
                        )
                    except Exception as e:
                        print(f"[overlay] erro: {e}")

                    try:
                        mgr._PremiereManager__clear_video_track_range(
                            mgr.LOGO_TRACK_INDEX, 20.0, seq_total_end.seconds)
                        mgr._PremiereManager__insert_logo_full(
                            roteiro_name=roteiro_name,
                            seq_end_time=seq_total_end,
                            paths_map=paths_map,
                            project_item_cache=project_item_cache,
                            dims_cache=dims_cache
                        )
                    except Exception as e:
                        print(f"[logo] erro: {e}")

        # >>> APLICAR FADE POR BLOCO (somente se NÃO for imediato) <<<
        if not apply_fade_immediately and fade_percentage > 0:
            for (first_c, last_c, fade_each) in fade_blocks:
                try:
                    if first_c == last_c:
                        one_len = max(
                            0.0, first_c.outPoint.seconds - first_c.inPoint.seconds)
                        safe_each = max(
                            0.05, min(fade_each, max(0.0, one_len / 2.0 - 0.01)))
                        mgr._PremiereManager__animate_opacity_fade_in_out(
                            first_c, safe_each)
                    else:
                        first_len = max(
                            0.0, first_c.outPoint.seconds - first_c.inPoint.seconds)
                        last_len = max(
                            0.0, last_c.outPoint.seconds - last_c.inPoint.seconds)
                        fin = max(
                            0.05, min(fade_each, max(0.0, first_len - 0.01)))
                        fout = max(
                            0.05, min(fade_each, max(0.0, last_len - 0.01)))

                        if fin > 0.05:
                            mgr._PremiereManager__animate_opacity_fade_in(
                                first_c, fin)
                        if fout > 0.05:
                            mgr._PremiereManager__animate_opacity_fade_out(
                                last_c, fout)
                except Exception:
                    pass

        return Result(success=True)

    except Exception as err:
        return Result(success=False, error=str(err))


def export_xml(mgr) -> Result:
    output_filename = f'export_{int(time.time())}.xml'

    output_folder_path = os.path.join(mgr.CWD, 'xml')
    if not os.path.exists(output_folder_path):
        return Result(success=False, error='A pasta "xml" não existe.')

    output_path = os.path.join(output_folder_path, output_filename)

    successful_export = pymiere.objects.app.project.exportFinalCutProXML(
        output_path, True)

    export_result = Result(success=successful_export)
    if successful_export is True:
        export_result.data = output_path

    return export_result


def save_project(mgr, *_args, **_kwargs) -> Result:
    """
    Salva o PROJETO ATUAL sem criar um novo arquivo.
    Ignora quaisquer argumentos passados.
    """
    try:
        project = pymiere.objects.app.project
        ok = project.save()  # em muitas versões retorna None no sucesso
        current_path = getattr(project, 'path', None)
        return Result(success=(ok is None), data=current_path)
    except Exception as e:
        return Result(success=False, error=str(e))
