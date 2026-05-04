import json
import os
import sys
from pathlib import Path


def get_runtime_root() -> str:
    # Quando virar .exe: pasta onde está o executável
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent)

    # Rodando no Python: raiz do projeto (pasta que contém "app" e o index.py)
    return str(Path(__file__).resolve().parents[2])


class SettingsManager:
    SETTINGS_PATH = os.path.join(get_runtime_root(), "settings.json")

    def ensure_settings(self):
        if os.path.exists(self.SETTINGS_PATH):
            return

        # Bloco `env` foi removido: API keys vem do servidor (manual-credenciais)
        # e nao sao persistidas em disco.
        settings = {
            'ui_cache': {
                # selects
                'mode': 'transcription',
                'script': '',
                'music_style': '',
                'resolution': '1920 x 1080',

                # zoom + fade
                'fade_percentage': 10,
                'fade_live': False,
                'zoom_min': 100,
                'zoom_max': 110,

                # cenas
                'duplicate_scenes': False,
                'fill_gaps_without_scene': True,
                'max_fill_scene_duration': 12.0,

                # modo em massa
                'mass_order': 'asc',
                'min_scene_seconds': 5,
                'max_scene_seconds': 7,

                # frases impactantes
                'impact': {
                    'enabled': False,
                    'mode': 'phrase',  # phrase | word
                    'max_phrases_total': 5,
                    'min_gap_seconds': 8.0,
                    'position': 'bottom',  # bottom | center | top
                    # fonte (pasta fontes)
                    'font_choice': '',
                    'font_file': '',
                    'font_size_px': None,
                    # legado (mantém para compatibilidade)
                    'font_name': ''
                }
            }
        }

        with open(self.SETTINGS_PATH, 'w') as file:
            json.dump(settings, file, indent=2)

    def read_settings(self) -> dict:
        self.ensure_settings()
        with open(self.SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Migracao: remove bloco `env` herdado de versoes antigas para
        # evitar que API keys continuem em disco (manual-credenciais).
        if isinstance(data, dict) and "env" in data:
            data.pop("env", None)
            try:
                with open(self.SETTINGS_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
        return data

    def write_settings(self, settings: dict):
        # Forca remocao de `env` antes de gravar: credenciais nao vao para disco.
        if isinstance(settings, dict):
            settings.pop("env", None)
        with open(self.SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
