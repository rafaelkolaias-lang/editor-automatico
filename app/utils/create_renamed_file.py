from os import path
import shutil

def create_renamed_file(file_path: str) -> str:
  dir_path = path.dirname(file_path)
  file_name, file_extension = path.basename(file_path).split('.')

  renamed_file_path = path.join(dir_path, f'{file_name}_.{file_extension}')

  if not path.exists(renamed_file_path):
    shutil.copy(file_path, renamed_file_path)

  return renamed_file_path
