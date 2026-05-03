import assemblyai as aai
import httpx

from concurrent.futures import ThreadPoolExecutor, as_completed
from os import path
import json
import os
import subprocess
import sys
import time
import requests

from ..entities import Part, Result, Transcription, TranscriptionWord
from ..utils.ffmpeg_path import get_ffprobe_bin
from core.remote_credentials import get_api_key

# Parametros de retry para uploads da AssemblyAI (ver !executar.md Tarefa 5)
_UPLOAD_MAX_RETRIES = 3
_UPLOAD_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
_UPLOAD_CONCURRENCY = 2
_ASSEMBLY_HTTP_TIMEOUT = 300.0  # 5 minutos, suporta narracao longa

# Oculta console preto do subprocess (ffmpeg/ffprobe) em Windows --windowed.
_NO_WINDOW_FLAGS = 0x08000000 if sys.platform == 'win32' else 0

_KNOWN_AUDIO_EXTS = {
    '.mp3', '.wav', '.m4a', '.aac', '.ogg', '.oga', '.flac',
    '.wma', '.mp4', '.mov', '.webm', '.opus',
}

_TRANSIENT_MSG_FRAGMENTS = (
    'timed out',
    'timeout',
    'connection reset',
    'read operation timed out',
    'temporarily unavailable',
)

_ASSEMBLY_REFUSED_FRAGMENTS = (
    'transcripterror',
    'failed to transcribe url',
    'select-the-speech-model',
)

_ASSEMBLY_SPEECH_MODELS_FRAGMENT = '"speech_models" must be a non-empty list'


def _is_assembly_speech_models_misconfig(exc: BaseException) -> bool:
    return _ASSEMBLY_SPEECH_MODELS_FRAGMENT in str(exc).lower() \
      or 'speech_models' in str(exc).lower() and 'non-empty list' in str(exc).lower()


def _is_transient_network_error(exc: BaseException) -> bool:
    """Identifica erros de rede que merecem retry (timeout/connection reset)."""
    if isinstance(exc, (TimeoutError,)):
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError)):
        return True
    msg = str(exc).lower()
    return any(frag in msg for frag in _TRANSIENT_MSG_FRAGMENTS)


def _is_assembly_transcript_refused(exc: BaseException) -> bool:
    """Identifica falhas em que a AssemblyAI recusou transcrever o audio."""
    text = f'{exc.__class__.__name__} {exc}'.lower()
    return any(frag in text for frag in _ASSEMBLY_REFUSED_FRAGMENTS)


def _probe_audio_info(audio_path: str) -> dict:
    """Coleta metadados do audio via ffprobe (duracao/codec). Nao falha se ffprobe ausente."""
    info: dict = {'duration_s': None, 'codec': None}
    try:
      ffprobe = get_ffprobe_bin()
      out = subprocess.run(
        [
          ffprobe, '-v', 'error',
          '-select_streams', 'a:0',
          '-show_entries', 'stream=codec_name:format=duration',
          '-of', 'json', audio_path,
        ],
        capture_output=True, text=True, timeout=15,
        creationflags=_NO_WINDOW_FLAGS,
      )
      if out.returncode == 0 and out.stdout:
        data = json.loads(out.stdout)
        streams = data.get('streams') or []
        if streams:
          info['codec'] = streams[0].get('codec_name')
        dur = (data.get('format') or {}).get('duration')
        if dur:
          try:
            info['duration_s'] = float(dur)
          except ValueError:
            pass
    except Exception:
      pass
    return info


def _validate_and_log_audio(audio_path: str) -> dict:
    """Valida arquivo de audio e imprime resumo. Levanta ValueError se invalido."""
    if not path.exists(audio_path):
      raise ValueError(f'Arquivo de audio nao encontrado: {audio_path}')
    size_bytes = os.path.getsize(audio_path)
    if size_bytes <= 0:
      raise ValueError(f'Arquivo de audio vazio: {audio_path}')
    ext = path.splitext(audio_path)[1].lower()
    if ext not in _KNOWN_AUDIO_EXTS:
      raise ValueError(f'Extensao de audio desconhecida ({ext}): {audio_path}')

    probe = _probe_audio_info(audio_path)
    duration = probe.get('duration_s')
    codec = probe.get('codec')
    if duration is not None and duration <= 0:
      raise ValueError(f'Audio com duracao zero: {audio_path}')

    size_mb = size_bytes / (1024 * 1024)
    dur_txt = f'{duration:.2f}' if duration is not None else '?'
    codec_txt = codec or '?'
    print(
      f"[Transcription] Enviando audio para AssemblyAI | arquivo='{audio_path}' "
      f"tamanho_mb={size_mb:.2f} extensao='{ext}' duracao_s={dur_txt} codec={codec_txt}",
      flush=True,
    )
    return {'ext': ext, 'size_bytes': size_bytes, 'duration_s': duration, 'codec': codec}


class _AssemblyRefusedError(Exception):
    """Erro final quando a AssemblyAI recusa transcrever o audio original."""

    def __init__(self, audio_path: str, original: BaseException):
      super().__init__(str(original))
      self.audio_path = audio_path
      self.original = original

class TranscriptionManager:
  OPENAI_BASE_URL: str = 'https://api.openai.com/v1'
  OPENAI_MODEL: str = 'gpt-4o-mini'

  def __init__(self, api_key: str = '', openai_api_key: str = ''):
    # Parametros mantidos por compatibilidade, mas credenciais sao obtidas
    # sob demanda via core.remote_credentials (manual-credenciais).
    pass

  def set_api_key(self, api_key: str):
    """No-op — credenciais sao obtidas sob demanda do servidor."""
    pass

  def set_openai_api_key(self, openai_api_key: str):
    """No-op — credenciais sao obtidas sob demanda do servidor."""
    pass

  @property
  def API_KEY(self) -> str:
    key = get_api_key('ASSEMBLY_AI_KEY')
    if key:
      aai.settings.api_key = key
    return key

  @property
  def OPENAI_API_KEY(self) -> str:
    return get_api_key('OPENAI_API_KEY')

  def __get_transcription_dict(self, transcription: aai.Transcript | Transcription) -> dict:
    return {
      'status': transcription.status,
      'words': [
        {
          'text': word.text,
          'start': word.start,
          'end': word.end
        } for word in transcription.words
      ]
    }

  def _transcribe_single_audio(self, audio_path: str) -> Transcription:
    """Transcreve um audio com cache JSON + retry para erros transitorios.

    Retorna Transcription em caso de sucesso (do cache ou da API).
    Levanta a ultima excecao caso todas as tentativas falhem.
    """
    audio_dir, audio_file = path.split(audio_path)
    audio_filename = path.splitext(audio_file)[0]
    json_transcription_path = path.join(audio_dir, f'{audio_filename}.json')

    # Cache hit: nao chama a API.
    if path.exists(json_transcription_path):
      with open(json_transcription_path, 'r', encoding='utf-8') as fh:
        data: dict = json.load(fh)
      return Transcription(
        status=data['status'],
        words=[TranscriptionWord(
          text=w['text'], start=w['start'], end=w['end']
        ) for w in data['words']]
      )

    # Validacao + log antes do upload. Falhas aqui levantam ValueError claro.
    _validate_and_log_audio(audio_path)

    transcription_config = aai.TranscriptionConfig(
      language_detection=True,
      punctuate=True,
      speech_models=['universal-3-pro', 'universal-2'],
    )

    def _try_upload(target_path: str) -> Transcription:
      """Executa upload com retry para erros transitorios. Levanta a ultima excecao."""
      last_error: BaseException | None = None
      for attempt in range(_UPLOAD_MAX_RETRIES):
        try:
          transcriber = aai.Transcriber()
          api_result: aai.Transcript = transcriber.transcribe(
            target_path, config=transcription_config
          )
          transcription = Transcription(
            status=api_result.status,
            words=[TranscriptionWord(
              text=w.text, start=w.start, end=w.end
            ) for w in api_result.words]
          )
          # Cache sempre associado ao audio original.
          with open(json_transcription_path, 'w', encoding='utf-8') as fh:
            json.dump(
              self.__get_transcription_dict(transcription),
              fh, indent=2, ensure_ascii=False,
            )
          return transcription
        except Exception as error:
          last_error = error
          if not _is_transient_network_error(error):
            raise
          if attempt < _UPLOAD_MAX_RETRIES - 1:
            delay = _UPLOAD_BACKOFF_SECONDS[attempt]
            print(
              f'[Transcription] Tentativa {attempt + 1} falhou ({error.__class__.__name__}: {error}). '
              f'Nova tentativa em {delay:.0f}s...',
              flush=True,
            )
            time.sleep(delay)
      raise last_error if last_error else RuntimeError('Falha desconhecida na transcricao.')

    try:
      return _try_upload(audio_path)
    except Exception as error:
      if _is_assembly_transcript_refused(error):
        raise _AssemblyRefusedError(audio_path, error) from error
      raise

  def transcribe_multiple_audios(self, audio_paths: list[str]) -> Result[list[Transcription]]:
    if self.API_KEY is None or self.API_KEY == '':
      return Result(success=False, error='A chave de API da AssemblyAI é necessária.')

    # Timeout oficial do SDK: 5 min. Suficiente para narracoes longas em
    # conexao lenta. Configurar antes de criar o Transcriber.
    try:
      aai.settings.http_timeout = _ASSEMBLY_HTTP_TIMEOUT
    except Exception:
      pass

    transcriptions: list[Transcription | None] = [None] * len(audio_paths)
    failures: list[tuple[str, Exception]] = []

    with ThreadPoolExecutor(max_workers=_UPLOAD_CONCURRENCY) as executor:
      future_to_index = {
        executor.submit(self._transcribe_single_audio, audio_path): idx
        for idx, audio_path in enumerate(audio_paths)
      }
      for future in as_completed(future_to_index):
        idx = future_to_index[future]
        audio_path = audio_paths[idx]
        try:
          transcriptions[idx] = future.result()
        except Exception as error:
          failures.append((audio_path, error))

    if failures:
      total = len(audio_paths)
      falhadas = len(failures)
      refused = [
        (fp, err) for fp, err in failures
        if isinstance(err, _AssemblyRefusedError) or _is_assembly_transcript_refused(err)
      ]
      if refused and len(refused) == falhadas:
        nomes = ', '.join(path.basename(fp) for fp, _ in refused)
        _, primeiro_refused = refused[0]
        msg_real = str(getattr(primeiro_refused, 'original', primeiro_refused)) or primeiro_refused.__class__.__name__
        error_msg = (
          f'A AssemblyAI recusou {falhadas} audio(s): {nomes}. '
          f'Erro principal: {msg_real}.'
        )
      else:
        primeiro_path, primeiro_erro = failures[0]
        if _is_assembly_speech_models_misconfig(primeiro_erro):
          error_msg = (
            "Falha de configuracao AssemblyAI: a API exige speech_models. "
            "Atualize a configuracao para ['universal-3-pro', 'universal-2'] "
            "ou ['universal-2']."
          )
          return Result(success=False, error=error_msg)
        if isinstance(primeiro_erro, ValueError):
          tipo = f'arquivo invalido ({primeiro_erro})'
        elif _is_transient_network_error(primeiro_erro):
          tipo = 'timeout no upload para AssemblyAI'
        else:
          tipo = f'{primeiro_erro.__class__.__name__}: {primeiro_erro}'
        error_msg = (
          f'Falha ao transcrever {falhadas} de {total} audio(s). '
          f'Motivo principal: {tipo}. '
          f'Verifique sua conexao e tente novamente.'
        )
      return Result(success=False, error=error_msg)

    # Todos concluidos com sucesso. Mas alguns podem nao estar `completed` no status.
    incompletos = [
      t for t in transcriptions
      if t is None or t.status != aai.TranscriptStatus.completed
    ]
    if incompletos:
      return Result(success=False, error='Ao menos uma transcricao nao foi concluida pelo servidor.')

    return Result(success=True, data=transcriptions)

  def find_parts_with_llm(
    self,
    transcription: Transcription,
    scenes_files: list[str],
    theme: str = '',
    pacing_seconds: int = 6,
  ) -> list[Part]:
    """
    Usa OpenAI para associar cenas à narração de forma semântica.
    Agrupa as palavras transcritas em segmentos cronológicos, envia a lista
    de cenas disponíveis e o roteiro para o GPT e obtém de volta os
    timestamps de cada cena.

    Retorna lista de Part ordenada por start_ms.
    Em caso de falha (sem chave, erro de API) faz fallback para a busca literal.
    """
    if not self.OPENAI_API_KEY:
      return self.find_parts_in_transcription(transcription, scenes_files)

    if not transcription.words or not scenes_files:
      return []

    # 1. Agrupar palavras em segmentos cronológicos (~pacing_seconds cada)
    segments: list[dict] = []
    current_words: list[TranscriptionWord] = []
    current_start: int = transcription.words[0].start

    for word in transcription.words:
      current_words.append(word)
      if (word.end - current_start) >= pacing_seconds * 1000:
        segments.append({
          'start_ms': current_start,
          'end_ms': word.end,
          'text': ' '.join(w.text for w in current_words),
        })
        current_words = []
        current_start = word.end

    if current_words:
      segments.append({
        'start_ms': current_start,
        'end_ms': current_words[-1].end,
        'text': ' '.join(w.text for w in current_words),
      })

    # 2. Montar prompt
    scene_names = [path.splitext(f)[0] for f in scenes_files]
    scene_list = '\n'.join(f'- {name}' for name in scene_names)
    timeline_lines = [
      f"{i}. [{seg['start_ms']}ms–{seg['end_ms']}ms] {seg['text']}"
      for i, seg in enumerate(segments)
    ]
    theme_line = f'\nTema do vídeo: {theme}' if theme else ''

    system = (
      'Você é um editor de vídeo inteligente. Associe cenas visuais à fala do locutor.\n'
      'Dado um roteiro dividido em segmentos cronológicos e uma lista de cenas disponíveis,\n'
      'escolha a cena semanticamente mais adequada para cada segmento.\n'
      'Regras:\n'
      '- Cada segmento DEVE receber exatamente uma cena.\n'
      '- Use o nome exato da cena conforme a lista fornecida (sem extensão).\n'
      '- Baseie-se no contexto semântico da fala, não em correspondência literal de palavras.\n'
      '- Varie as cenas; evite repetir a mesma cena em segmentos consecutivos.'
      f'{theme_line}\n'
      'Responda SOMENTE com o JSON solicitado.'
    )

    user = (
      f'Cenas disponíveis:\n{scene_list}\n\n'
      'Segmentos da narração:\n' + '\n'.join(timeline_lines)
    )

    schema = {
      'type': 'object',
      'properties': {
        'assignments': {
          'type': 'array',
          'items': {
            'type': 'object',
            'properties': {
              'segment_index': {'type': 'integer'},
              'scene_name':    {'type': 'string'},
              'start_ms':      {'type': 'integer'},
              'end_ms':        {'type': 'integer'},
            },
            'required': ['segment_index', 'scene_name', 'start_ms', 'end_ms'],
            'additionalProperties': False,
          },
        }
      },
      'required': ['assignments'],
      'additionalProperties': False,
    }

    body = {
      'model': self.OPENAI_MODEL,
      'input': [
        {'role': 'system', 'content': system},
        {'role': 'user',   'content': user},
      ],
      'text': {
        'format': {
          'type':   'json_schema',
          'name':   'scene_assignments',
          'strict': True,
          'schema': schema,
        }
      },
      'temperature':      0.3,
      'max_output_tokens': 1500,
      'store': False,
    }

    url     = f'{self.OPENAI_BASE_URL}/responses'
    headers = {
      'Authorization': f'Bearer {self.OPENAI_API_KEY}',
      'Content-Type':  'application/json',
    }

    # 3. Chamar API (2 tentativas)
    for attempt in range(2):
      try:
        resp = requests.post(url, headers=headers, json=body, timeout=45)
        if resp.status_code >= 400:
          time.sleep(0.5)
          continue

        data = resp.json()
        out_texts: list[str] = []
        for item in data.get('output', []) or []:
          if item.get('type') != 'message':
            continue
          for c in item.get('content', []) or []:
            if c.get('type') == 'output_text' and isinstance(c.get('text'), str):
              out_texts.append(c['text'])

        if not out_texts:
          break

        parsed      = json.loads(out_texts[-1].strip())
        assignments = parsed.get('assignments', [])

        # 4. Converter para Part
        found_parts: list[Part] = []
        for assignment in assignments:
          scene_name = assignment.get('scene_name', '')
          start_ms   = int(assignment.get('start_ms', 0))

          # Busca exata, depois case-insensitive
          matched_file = next(
            (f for f in scenes_files if path.splitext(f)[0] == scene_name),
            None,
          )
          if matched_file is None:
            scene_name_lower = scene_name.lower()
            matched_file = next(
              (f for f in scenes_files if path.splitext(f)[0].lower() == scene_name_lower),
              None,
            )

          if matched_file:
            found_parts.append(Part(text=matched_file, start=start_ms))

        return sorted(found_parts, key=lambda p: p.start)

      except Exception:
        time.sleep(0.5)

    # Fallback para busca literal
    return self.find_parts_in_transcription(transcription, scenes_files)

  def find_parts_in_transcription(self, transcription: Transcription, scenes_files: list[str]) -> list[Part]:
    import re
    import unicodedata

    def norm_token(s: str) -> str:
      s = (s or '').strip().lower()

      # remove acentos (ex: "ação" -> "acao")
      s = unicodedata.normalize('NFD', s)
      s = ''.join(ch for ch in s if unicodedata.category(ch) != 'Mn')

      # remove pontuação e símbolos, mantendo letras/números e espaço
      s = re.sub(r'[^a-z0-9\s]', '', s)

      # colapsa múltiplos espaços
      s = re.sub(r'\s+', ' ', s).strip()
      return s

    found_parts: list[Part] = []

    # normaliza palavras transcritas
    words = [Part(text=norm_token(word.text), start=word.start) for word in transcription.words]

    # pega o "nome da cena" (sem extensão) e normaliza
    parts_to_find_raw = [path.splitext(scene_file)[0] for scene_file in scenes_files]
    parts_to_find = [norm_token(p) for p in parts_to_find_raw]

    for part_index, part in enumerate(parts_to_find):
      if not part:
        continue

      splitted_part = [w for w in part.split(' ') if w]  # tokens já normalizados

      for i in range(len(words)):
        should_append = True

        for j in range(len(splitted_part)):
          if i + j >= len(words):
            should_append = False
            break

          if splitted_part[j] != words[i + j].text:
            should_append = False
            break

        if should_append:
          found_parts.append(Part(text=scenes_files[part_index], start=words[i].start))

    sorted_found_parts = sorted(found_parts, key=lambda part: part.start)
    return sorted_found_parts

