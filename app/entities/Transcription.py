class TranscriptionWord:
  text: str
  start: int
  end: int

  def __init__(self, text: str, start: int, end: int):
    self.text = text
    self.start = start
    self.end = end

class Transcription:
  status: str
  words: list[TranscriptionWord]

  def __init__(self, status: str, words: list[TranscriptionWord]):
    self.status = status
    self.words = words
