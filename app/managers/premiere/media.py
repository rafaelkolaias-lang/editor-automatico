# managers/premiere/media.py
import os
import time
import pymiere
from os import path
from ...entities import EXTENSIONS, Result


def get_files_paths(self, script_name: str, music_style: str) -> Result[dict[str, str | list[str]]]:
    narration_base_path = os.path.join(self.CWD, 'narracao', script_name)
    scenes_base_path = os.path.join(self.CWD, 'cenas', script_name)
    musics_base_path = os.path.join(self.CWD, 'musica', music_style)

    does_narration_exist = os.path.exists(narration_base_path)
    if not does_narration_exist:
        return Result(
        success=False,
        error=f'A pasta de narrações do roteiro "{script_name}" não foi encontrada.'
        )

    does_scenes_exist = os.path.exists(scenes_base_path)
    if not does_scenes_exist:
        return Result(
        success=False,
        error=f'A pasta de cenas do roteiro "{script_name}" não foi encontrada.'
        )

    does_musics_exist = os.path.exists(musics_base_path)
    if not does_musics_exist:
        return Result(
        success=False,
        error=f'A pasta de músicas do estilo "{music_style}" não foi encontrada.'
        )

    scenes_files = [scene_file for scene_file in os.listdir(scenes_base_path)]

    scenes_paths = [
        os.path.join(scenes_base_path, scene_file)
        for scene_file in scenes_files
        if any(scene_file.endswith(vx) for vx in EXTENSIONS['VIDEO'])
        or any(scene_file.endswith(ix) for ix in EXTENSIONS['IMAGE'])
    ]

    if len(scenes_paths) == 0:
        return Result(
        success=False,
        error=f'A pasta de cenas do roteiro "{script_name}" não contém vídeos/imagens suportados.'
        )



    narrations_files = [narration_file for narration_file in os.listdir(narration_base_path) if any(narration_file.endswith(audio_extension) for audio_extension in EXTENSIONS['AUDIO'])]
    if len(narrations_files) == 0:
        return Result(
        success=False,
        error=f'A pasta de narrações do roteiro "{script_name}" está vazia.'
        )

    narrations_paths = [os.path.join(narration_base_path, narration_file) for narration_file in narrations_files]

    musics_files =[music_file for music_file in os.listdir(musics_base_path) if any(music_file.endswith(audio_extension) for audio_extension in EXTENSIONS['AUDIO'])]
    if len(musics_files) == 0:
        return Result(
        success=False,
        error=f'A pasta de músicas do estilo "{music_style}" está vazia.'
        )

    musics_paths = [os.path.join(musics_base_path, music) for music in musics_files]

    files_paths = scenes_paths + narrations_paths + musics_paths

    return Result(
        success=True,
        data={
        'files_paths': files_paths,
        'narrations_files': narrations_files,
        'narration_base_path': narration_base_path,
        'scenes_files': scenes_files,
        'scenes_base_path': scenes_base_path,
        'musics_files': musics_files,
        'musics_base_path': musics_base_path
        }
    )

def import_files(mgr, files_paths: list[str]) -> dict[str, bool]:
    """
    Importa em lote e confirma, arquivo a arquivo, se o Premiere indexou.
    Mantém o mesmo contrato do PremiereManager.import_files original:
    retorna { caminho: True/False }.
    """
    files_success: dict[str, bool] = {}

    # 1) tentativa de importação em lote (não falha o fluxo se der erro)
    try:
        pymiere.objects.app.project.importFiles(
            files_paths, True, pymiere.objects.app.project.getInsertionBin(), False
        )
    except Exception:
        pass

    # 2) para cada arquivo, confirma se está importado; se não, tenta unitário + polling
    for fp in files_paths:
        item = None

        # 2.1) já importado?
        try:
            res = pymiere.objects.app.project.rootItem.findItemsMatchingMediaPath(
                fp, ignoreSubclips=False
            )
            item = res[0] if res else None
        except Exception:
            item = None

        # 2.2) se não achar, tenta importar individualmente e aguarda indexação (~5s)
        if item is None:
            try:
                pymiere.objects.app.project.importFiles(
                    [fp], True, pymiere.objects.app.project.getInsertionBin(), False
                )
            except Exception:
                pass

            start_wait = time.time()
            while time.time() - start_wait < 5.0:
                try:
                    res = pymiere.objects.app.project.rootItem.findItemsMatchingMediaPath(
                        fp, ignoreSubclips=False
                    )
                    item = res[0] if res else None
                    if item is not None:
                        break
                except Exception:
                    item = None
                time.sleep(0.1)

        files_success[fp] = bool(item)

    return files_success
