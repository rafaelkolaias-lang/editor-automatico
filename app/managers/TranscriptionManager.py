import assemblyai as aai

from threading import Thread
from os import path
import json
import time
import requests

from ..entities import Part, Result, Transcription, TranscriptionWord
from ..utils import handle_thread_error

class TranscriptionManager:
  API_KEY: str = ''
  OPENAI_API_KEY: str = ''
  OPENAI_BASE_URL: str = 'https://api.openai.com/v1'
  OPENAI_MODEL: str = 'gpt-4o-mini'

  def __init__(self, api_key: str, openai_api_key: str = ''):
    self.API_KEY = api_key
    aai.settings.api_key = api_key
    self.OPENAI_API_KEY = openai_api_key or ''

  def set_api_key(self, api_key: str):
    self.API_KEY = api_key
    aai.settings.api_key = api_key

  def set_openai_api_key(self, openai_api_key: str):
    self.OPENAI_API_KEY = openai_api_key or ''

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

  def transcribe_multiple_audios(self, audio_paths: list[str]) -> Result[list[Transcription]]:
    if self.API_KEY is None or self.API_KEY == '':
      return Result(success=False, error='A chave de API da AssemblyAI é necessária.')

    transcriptions: list[Transcription] = [None] * len(audio_paths)
    threads: list[Thread] = []

    def transcribe_single_audio(audio_path: str, audio_index: int) -> None:
      try:
        audio_dir, audio_file = path.split(audio_path)
        audio_filename = path.splitext(audio_file)[0]

        json_transcription_path = path.join(audio_dir, f'{audio_filename}.json')

        if path.exists(json_transcription_path):
          with open(json_transcription_path, 'r', encoding='utf-8') as json_transcription_file:
            transcripted_audio_data: dict = json.load(json_transcription_file)

            transcripted_audio = Transcription(
              status=transcripted_audio_data['status'],
              words=[TranscriptionWord(
                text=word['text'],
                start=word['start'],
                end=word['end']
              ) for word in transcripted_audio_data['words']]
            )

            transcriptions[audio_index] = transcripted_audio
            return

        transcriber = aai.Transcriber()
        transcription_config = aai.TranscriptionConfig(
          language_detection=True,
          punctuate=True,
          speech_model=aai.SpeechModel.best
        )

        transcripted_audio_data: aai.Transcript = transcriber.transcribe(audio_path, config=transcription_config)
        transcripted_audio = Transcription(
          status=transcripted_audio_data.status,
          words=[TranscriptionWord(
            text=word.text,
            start=word.start,
            end=word.end
          ) for word in transcripted_audio_data.words]
        )

        with open(json_transcription_path, 'w', encoding='utf-8') as json_transcription_file:
          json.dump(self.__get_transcription_dict(transcripted_audio), json_transcription_file, indent=2, ensure_ascii=False)

        transcriptions[audio_index] = transcripted_audio
      except Exception as error:
        handle_thread_error(error)

    for audio_index, audio_path in enumerate(audio_paths):
      thread = Thread(target=transcribe_single_audio, args=(audio_path, audio_index))
      threads.append(thread)
      thread.start()

    for thread in threads:
      thread.join()

    transcriptions_success = False if any([thread.is_alive() for thread in threads]) else all([transcription is not None and transcription.status == aai.TranscriptStatus.completed for transcription in transcriptions])

    transcriptions_result = Result(success=transcriptions_success)
    if transcriptions_success:
      transcriptions_result.data = transcriptions
    else:
      transcriptions_result.error = 'O processo de transcrição não pôde ser concluído.'

    return transcriptions_result

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

