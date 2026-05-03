import sys
from os import path
from PIL import Image
try:
    from moviepy.editor import VideoFileClip  # moviepy 1.x
except ImportError:
    from moviepy import VideoFileClip  # moviepy 2.x
import subprocess

from ..entities import EXTENSIONS, Result
from ..utils.ffmpeg_path import get_ffmpeg_bin

# Oculta console preto do subprocess (ffmpeg) em Windows --windowed.
_NO_WINDOW_FLAGS = 0x08000000 if sys.platform == 'win32' else 0

class ConversionManager:
  UNDERLINE = '_'

  def __split_path(self, file_path: str) -> dict[str, str]:
    base_path, filename = path.split(file_path)
    filename_without_extension, extension = path.splitext(filename)

    return {
      'base_path': base_path,
      'filename': filename,
      'filename_without_extension': filename_without_extension,
      'extension': extension
    }

  def __get_save_path(self, base_path: str, filename_without_extension: str, extension: str) -> str:
    save_path = path.join(base_path, f'{filename_without_extension}{self.UNDERLINE}{extension}')

    return save_path

  def identify_file_type(self, file_path: str) -> str:
    extension = self.__split_path(file_path).get('extension').lower()

    for file_type, extensions in EXTENSIONS.items():
      if extension in extensions:
        return file_type

    return 'OTHER'

  def convert_audio(self, audio_path: str) -> Result[str]:
    UNKNOWN_ERROR_MESSAGE ='Ocorreu um erro desconhecido ao converter o arquivo de áudio.'

    try:
      splitted_path_dict = self.__split_path(audio_path)

      base_path = splitted_path_dict.get('base_path')
      filename_without_extension = splitted_path_dict.get('filename_without_extension')

      save_path = self.__get_save_path(base_path, filename_without_extension, '.mp3')
      if path.exists(save_path):
        return Result(success=True, data=save_path)

      conversion_return = subprocess.run(
          [get_ffmpeg_bin(), '-hide_banner', '-loglevel', 'error', '-i', audio_path, save_path],
          creationflags=_NO_WINDOW_FLAGS)

      successful_conversion = conversion_return.returncode == 0

      conversion_result = Result(success=successful_conversion)

      if successful_conversion:
        conversion_result.data = save_path
      else:
        conversion_result.error = UNKNOWN_ERROR_MESSAGE

      return conversion_result
    except FileNotFoundError:
      return Result(success=False, error='O FFMPEG não foi encontrado no sistema.')
    except Exception:
      return Result(success=False, error=UNKNOWN_ERROR_MESSAGE)

  def convert_image(self, image_path: str) -> str:
    splitted_path_dict = self.__split_path(image_path)

    base_path = splitted_path_dict.get('base_path')
    filename_without_extension = splitted_path_dict.get('filename_without_extension')

    save_path = self.__get_save_path(base_path, filename_without_extension, '.png')
    if path.exists(save_path):
      return save_path

    image = Image.open(image_path)
    image.save(save_path, 'PNG')
    image.close()

    return save_path

  def convert_video(self, file_path: str) -> str:
    splitted_path_dict = self.__split_path(file_path)

    base_path = splitted_path_dict.get('base_path')
    filename_without_extension = splitted_path_dict.get('filename_without_extension')
    extension = splitted_path_dict.get('extension')

    is_gif = extension == '.gif'

    save_path = self.__get_save_path(base_path, filename_without_extension, '.gif' if is_gif else '.mp4')
    if path.exists(save_path):
      return save_path

    video = VideoFileClip(file_path)

    if is_gif:
      video.write_gif(save_path)
    else:
      video.write_videofile(save_path, codec='libx264')

    video.close()

    return save_path
