import os
import sys
from pathlib import Path
import re
from ..entities import EXTENSIONS


def get_runtime_root() -> str:
    """
    Runtime root = pasta onde o settings.json vive.
    Centralizado no SettingsManager para evitar duplicação.
    """
    try:
        from .SettingsManager import get_runtime_root as _get_runtime_root
    except Exception:
        from SettingsManager import get_runtime_root as _get_runtime_root

    return _get_runtime_root()


class DirectoriesManager:
    CWD = get_runtime_root()
    DIRECTORIES = [
        {
            'name': 'musica',
            'creatable': True,
            'readable': True
        },
        {
            'name': 'cenas',
            'creatable': True,
            'readable': True
        },
        {
            'name': 'narracao',
            'creatable': True,
            'readable': True
        },
        {
            'name': 'xml',
            'creatable': False,
            'readable': False
        },
        {
            'name': 'projeto',
            'creatable': True,
            'readable': False
        },
        {
            'name': 'partes',
            'creatable': True,
            'readable': False  # nao aparece no read_directories; vamos ler com metodo proprio
        },
        {
            'name': 'logo',
            'creatable': True,
            'readable': False
        },
        {
            'name': 'overlay',
            'creatable': True,
            'readable': False
        },
        {
            'name': 'animacao',
            'creatable': True,
            'readable': False
        }
    ]

    def ensure_directories(self):
        for directory in self.DIRECTORIES:
            if directory.get('creatable') == False:
                continue

            dir_path = os.path.join(self.CWD, directory.get('name'))

            if not os.path.exists(dir_path):
                os.makedirs(dir_path)

    def read_directories(self) -> dict:
        content_dict = dict()

        for directory in self.DIRECTORIES:
            if directory.get('creatable') == False or directory.get('readable') == False:
                continue

            dir_name = directory.get('name')
            dir_path = os.path.join(self.CWD, dir_name)
            if not os.path.exists(dir_path):
                raise Exception(f'Expected "{dir_name}" directory not found')

            content_dict[dir_name] = [item.name for item in Path(
                dir_path).iterdir() if item.is_dir()]

        return content_dict

    # --- helpers privados ---

    def __natural_key(self, s: str):
        """Chave de ordenação 'natural' (arquivo2 < arquivo10)."""
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]

    def __sorted_dirs(self, base_path: str, prefix: str | None = None) -> list[str]:
        """Lista pastas (apenas diretórios) ordenadas 'naturalmente' e com prefixo opcional."""
        if not os.path.exists(base_path):
            return []
        items = [d for d in os.listdir(base_path) if os.path.isdir(
            os.path.join(base_path, d))]
        if prefix:
            items = [d for d in items if d.lower().startswith(prefix.lower())]
        items_sorted = sorted(items, key=self.__natural_key)
        return [os.path.join(base_path, d) for d in items_sorted]

    def __sorted_files(self, base_path: str, exts: set[str]) -> list[str]:
        """Lista arquivos (apenas files) por extensão, ordenados 'naturalmente'."""
        if not os.path.exists(base_path):
            return []
        items = [f for f in os.listdir(base_path) if os.path.isfile(
            os.path.join(base_path, f))]
        files = []
        for f in items:
            _, ext = os.path.splitext(f)
            if ext.lower() in exts:
                files.append(f)
        files_sorted = sorted(files, key=self.__natural_key)
        return [os.path.join(base_path, f) for f in files_sorted]

    # --- leitor da estrutura de 'vídeo em massa' ---

    def read_mass_structure(self) -> dict:
        """
        Lê 'partes/<qualquer_nome>/<qualquer_nome>' (apenas pastas) e retorna em ORDEM NATURAL:
        - Não exige prefixos como 'roteiro_' ou 'cenas_'.
        - Dentro de cada cena, lê:
            * ÁUDIOS por extensão (EXTENSIONS['AUDIO']) em ordem de nome;
            * MÍDIAS (imagem+vídeo) por extensão (EXTENSIONS['IMAGE']|['VIDEO']) em ordem de nome.
        """
        partes_base = os.path.join(self.CWD, 'partes')
        if not os.path.exists(partes_base):
            return {'base_path': partes_base, 'roteiros': [], 'files_paths': []}

        structure = {'base_path': partes_base,
                     'roteiros': [], 'files_paths': []}

        # todas as pastas dentro de 'partes' (ordem natural, sem prefixo fixo)
        roteiros_dirs = self.__sorted_dirs(partes_base)
        for roteiro_path in roteiros_dirs:
            roteiro_name = os.path.basename(roteiro_path)

            # todas as subpastas dentro do roteiro (ordem natural, sem prefixo fixo)
            cenas_dirs = self.__sorted_dirs(roteiro_path)
            roteiro_obj = {'name': roteiro_name,
                           'path': roteiro_path, 'cenas': []}

            for cena_path in cenas_dirs:
                cena_name = os.path.basename(cena_path)

                # áudios de narração (qualquer nome, em ordem de nome)
                audios = self.__sorted_files(cena_path, EXTENSIONS['AUDIO'])

                # mídias visuais (imagem + vídeo) (qualquer nome, em ordem de nome)
                medias = self.__sorted_files(
                    cena_path, EXTENSIONS['IMAGE'] | EXTENSIONS['VIDEO'])

                roteiro_obj['cenas'].append({
                    'name': cena_name,
                    'path': cena_path,
                    'audios': audios,
                    'medias': medias
                })

                structure['files_paths'].extend(audios + medias)

            structure['roteiros'].append(roteiro_obj)

        # dedup dos caminhos agregados (preserva ordem)
        seen = set()
        unique_files = []
        for p in structure['files_paths']:
            if p not in seen:
                seen.add(p)
                unique_files.append(p)
        structure['files_paths'] = unique_files

        return structure
