import json
import os
import re
import shutil
import threading
import unicodedata
import subprocess
import sys
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from openai import OpenAI

# miniaturas de imagem
try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# vídeo preview (opcional)
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False





# Tamanho máximo aproximado de caracteres por bloco de roteiro
# (ajuste se quiser blocos maiores ou menores)
MAX_CHARS_ROTEIRO_BLOCO = 20000

# -----------------------------
# Configuração e utilitários
# -----------------------------

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MEDIA_EXTS = VIDEO_EXTS.union(IMAGE_EXTS)

PREFS_PATH = Path.home() / ".renomear_cenas_prefs.json"


def carregar_prefs() -> dict:
    """
    Carrega preferências salvas (API key, pastas etc.) de um arquivo JSON no home do usuário.
    Se não existir ou der erro, retorna {}.
    """
    try:
        if PREFS_PATH.exists():
            texto = PREFS_PATH.read_text(encoding="utf-8")
            return json.loads(texto)
    except Exception:
        pass
    return {}


def salvar_prefs(dados: dict):
    """
    Salva preferências em JSON no home do usuário.
    Qualquer erro na gravação é silenciosamente ignorado.
    """
    try:
        PREFS_PATH.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def abrir_arquivo(path: Path):
    """
    Abre um arquivo de mídia (vídeo/imagem) com o player padrão do sistema operacional.
    """
    if os.name == "nt":  # Windows
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":  # macOS
        subprocess.Popen(["open", str(path)])
    else:  # Linux e afins
        subprocess.Popen(["xdg-open", str(path)])

def slugify(texto: str, max_palavras: Optional[int] = None, max_chars: int = 80) -> str:
    """
    Converte um trecho de frase do roteiro em algo seguro para nome de arquivo:
    - limita quantidade de palavras
    - remove acentos
    - mantém espaços normais
    - deixa minúsculo
    - remove caracteres estranhos
    """
    texto = texto.strip()
    if not texto:
        return "sem texto"

    # limita quantidade de palavras se configurado
    palavras = texto.split()
    if max_palavras is not None and max_palavras > 0:
        palavras = palavras[:max_palavras]
    texto = " ".join(palavras)

    # remove acentos
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))

    # deixa minúsculo
    texto = texto.lower()

    # permite apenas letras, números, espaço e hífen
    # (sem underscore)
    texto = re.sub(r"[^a-z0-9\- ]", "", texto)

    # remove espaços extras do começo/fim
    texto = texto.strip()

    if not texto:
        texto = "sem texto"

    # limita tamanho em caracteres se precisar
    if len(texto) > max_chars:
        texto = texto[:max_chars].rstrip()

    return texto


def listar_cenas(pasta_cenas: Path):
    """
    Lista todos os arquivos de mídia da pasta de cenas (inclusive subpastas).
    Retorna:
        - dict: nome_do_arquivo -> Path completo
        - lista ordenada de nomes de arquivo (para o modelo)
    OBS: se existirem arquivos com o MESMO nome em subpastas diferentes,
    o último encontrado vai sobrescrever o anterior.
    """
    if not pasta_cenas.exists():
        raise FileNotFoundError(f"Pasta de cenas não encontrada: {pasta_cenas}")

    mapa: dict[str, Path] = {}
    nomes: list[str] = []

    # percorre recursivamente todas as subpastas
    for item in sorted(pasta_cenas.rglob("*")):
        if item.is_file() and item.suffix.lower() in MEDIA_EXTS:
            nome = item.name  # mantém só o nome do arquivo, como antes
            mapa[nome] = item
            nomes.append(nome)

    if not nomes:
        raise RuntimeError(
            f"Nenhum arquivo de vídeo/imagem encontrado em {pasta_cenas} (incluindo subpastas).\n"
            f"Extensões aceitas: {', '.join(sorted(MEDIA_EXTS))}"
        )

    return mapa, nomes

def ler_roteiro(caminho_roteiro: Path) -> str:
    if not caminho_roteiro.exists():
        raise FileNotFoundError(f"Arquivo de roteiro não encontrado: {caminho_roteiro}")
    return caminho_roteiro.read_text(encoding="utf-8")

def criar_trechos_por_frases(roteiro: str, frases_por_cena: int) -> list[dict]:
    """
    Divide o roteiro em trechos com até 'frases_por_cena' frases cada.
    Frase aqui é tudo entre pontos finais ('.').
    Retorna uma lista de dicts: {"index": 1, "texto": "frase1. frase2."}
    """
    if frases_por_cena <= 0:
        frases_por_cena = 1

    # Junta quebras de linha em espaços para simplificar
    texto = roteiro.replace("\n", " ").strip()
    if not texto:
        return [{"index": 1, "texto": ""}]

    # quebra por ponto final
    partes_brutas = [p.strip() for p in texto.split(".") if p.strip()]

    if not partes_brutas:
        return [{"index": 1, "texto": texto}]

    # recoloca o ponto final em cada frase
    frases = [p + "." for p in partes_brutas]

    trechos: list[dict] = []
    idx = 1

    for i in range(0, len(frases), frases_por_cena):
        grupo = frases[i : i + frases_por_cena]
        trecho = " ".join(grupo).strip()
        if trecho:
            trechos.append({"index": idx, "texto": trecho})
            idx += 1

    return trechos

def dividir_texto_em_blocos(texto: str, max_chars: int = MAX_CHARS_ROTEIRO_BLOCO) -> list[str]:
    """
    Divide o roteiro em blocos de até max_chars caracteres, tentando respeitar parágrafos.
    Isso evita estourar o limite de contexto da API quando o roteiro é muito grande.
    """
    texto = texto.strip()
    if not texto:
        return [""]

    # separa por blocos de parágrafo (linhas em branco)
    paragrafos = re.split(r"\n\s*\n", texto)
    blocos: list[str] = []
    bloco_atual = ""

    for p in paragrafos:
        p = p.strip()
        if not p:
            continue

        # se um parágrafo sozinho já for maior que max_chars, cortamos em fatias
        if len(p) > max_chars:
            # fecha bloco atual, se existir
            if bloco_atual:
                blocos.append(bloco_atual)
                bloco_atual = ""

            inicio = 0
            while inicio < len(p):
                fim = inicio + max_chars
                parte = p[inicio:fim]
                blocos.append(parte)
                inicio = fim
            continue

        # tenta adicionar o parágrafo ao bloco atual
        if not bloco_atual:
            bloco_atual = p
        elif len(bloco_atual) + 2 + len(p) <= max_chars:
            bloco_atual += "\n\n" + p
        else:
            blocos.append(bloco_atual)
            bloco_atual = p

    if bloco_atual:
        blocos.append(bloco_atual)

    return blocos

def chamar_gpt_montar_timeline(
    client: OpenAI,
    modelo: str,
    roteiro: str,
    nomes_cenas: list[str],
    max_usos_por_cena: int = 3,
    frases_por_cena: int = 2,
) -> dict:
    """
    Usa o GPT para atribuir cenas a trechos do roteiro.
    Cada trecho já vem com até 'frases_por_cena' frases (definido no Python).
    O modelo apenas decide qual cena usar em cada trecho.

    Regras extras aplicadas no pós-processamento:
    - Não permitir a mesma cena em dois trechos consecutivos.
    - missing_scenes_suggestions: sugestões de cenas que NÃO existem ainda,
      com dica do que ter na cena e como pesquisar.
    """
    # divide o roteiro em trechos de N frases
    trechos = criar_trechos_por_frases(roteiro, frases_por_cena)

    system_prompt = (
        "Você ajuda a encontrar cenas corretas para trechos de roteiros.\n"
        "Você recebe uma lista ordenada de trechos do roteiro e uma lista de cenas visuais "
        "(arquivos de vídeo/imagem) onde o nome do arquivo descreve a cena.\n\n"
        "Sua tarefa é, para CADA trecho do roteiro, verificar se alguma cena se encaixa (ou não) no contexto do trecho.\n\n"
        "Regras importantes:\n"
        "- A resposta DEVE ser um único objeto JSON válido.\n"
        "- A saída deve ter um array 'timeline'.\n"
        "- Cada item da 'timeline' deve ter:\n"
        "  - 'index': o mesmo índice do trecho recebido.\n"
        "  - 'script_fragment': o texto completo do trecho.\n"
        "  - 'assigned_scene':\n"
        "      * null se nenhuma cena disponível encaixa bem naquele trecho;\n"
        "      * ou um dos nomes de arquivo exatamente como fornecidos na lista de cenas.\n"
        "- Você pode reutilizar a mesma cena em vários trechos diferentes (duplicar a cena no vídeo), "
        "  MAS NÃO use a mesma cena em dois trechos consecutivos.\n"
        "- Se achar que a mesma cena encaixaria em trechos consecutivos, use a cena no primeiro trecho "
        "  e deixe o seguinte como null ou escolha outra cena que também faça sentido.\n"
        "- Tente, sempre buscar encaixar cada cena no contexto correto, atribuir alguma cena (evite null) desde que não viole a regra de "
        "  não repetir a mesma cena em trechos consecutivos.\n"
        "- Não invente nomes de arquivos novos em 'assigned_scene'. Use apenas os fornecidos.\n"
        "- Em 'missing_scenes_suggestions', liste trechos importantes sem cena e sugira\n"
        "  que tipo de cena visual seria ideal.\n"
        "- Para cada item de 'missing_scenes_suggestions', inclua também:\n"
        "  - 'index': o mesmo índice daquele trecho no array 'timeline';\n"
        "  - 'search_suggestion': um texto curto com as palavras-chave que o usuário deve pesquisar\n"
        "    em bancos de vídeos/imagens (ex.: 'Planeta Marte no espaço', 'cidade futurista à noite').\n"
        "- Mantenha a ordem da 'timeline' igual à ordem dos trechos.\n"
        "- Use português simples e direto nas descrições.\n"
    )

    user_payload = {
        "trechos_roteiro": trechos,
        "cenas_disponiveis": nomes_cenas,
        "instrucoes_saida": {
            "formato_json": {
                "timeline": [
                    {
                        "index": "mesmo índice do trecho de entrada",
                        "script_fragment": "trecho do roteiro associado àquela cena",
                        "assigned_scene": "nome exato do arquivo de cena OU null",
                    }
                ],
                "missing_scenes_suggestions": [
                    {
                        "index": "mesmo índice do trecho no array 'timeline'",
                        "script_fragment": "trecho do roteiro sem cena correspondente",
                        "suggested_scene_description": "descrição resumida da cena que deveria existir",
                        "search_suggestion": "termos sugeridos para pesquisar essa cena em bancos de mídia",
                    }
                ],
            }
        },
        "restricoes": {
            "max_usos_por_cena": max_usos_por_cena,
            "observacao": (
                "não é obrigatório usar todas as cenas; priorize encaixe semântico. "
                "Evite usar a mesma cena em dois trechos consecutivos."
            ),
        },
    }

    completion = client.chat.completions.create(
        model=modelo,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "A seguir estão os dados em JSON. "
                    "Responda APENAS com um único objeto JSON.\n\n"
                    + json.dumps(user_payload, ensure_ascii=False)
                ),
            },
        ],
        temperature=0.4,
        response_format={"type": "json_object"},
    )

    conteudo = completion.choices[0].message.content
    try:
        dados = json.loads(conteudo)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Falha ao interpretar resposta do modelo como JSON.\n"
            f"Erro: {e}\n\nResposta bruta:\n{conteudo}"
        )

    if "timeline" not in dados:
        raise RuntimeError("JSON retornado não contém campo 'timeline'.")

    # garante o campo, mesmo que venha vazio
    dados.setdefault("missing_scenes_suggestions", [])

    # --------- PÓS-PROCESSAMENTO: não repetir mesma cena em trechos consecutivos ---------
    timeline = dados.get("timeline", [])
    last_scene = None

    for item in timeline:
        scene = item.get("assigned_scene")
        if scene is not None and scene == "":
            scene = None

        if scene is not None and scene == last_scene:
            # zera esta cena para não repetir consecutivamente
            item["assigned_scene"] = None
        else:
            last_scene = scene

    return dados




def sugerir_cenas_adicionais(
    client: OpenAI,
    modelo: str,
    roteiro_completo: str,
    nomes_cenas_existentes: list[str],
    timeline_dados: dict,
    num_sugestoes: int = 15,
) -> dict:
    """
    Pede ao modelo uma lista organizada de cenas adicionais recomendadas
    para melhorar o vídeo, considerando que há poucas cenas para a quantidade de roteiro.

    Saída esperada (JSON):
    {
      "extra_scenes": [
        {
          "suggested_filename": "caminhando_na_cidade",
          "short_title": "Pessoa caminhando na cidade",
          "description": "Pessoa andando em uma avenida movimentada ao entardecer...",
          "script_fragment": "Trecho do roteiro relacionado",
          "priority": 1   # 1 = muito importante, 5 = opcional
        },
        ...
      ]
    }
    """
    system_prompt = (
        "Você ajuda a encontrar cenas corretas para trechos de roteiros. "
        "O usuário tem um roteiro de vídeo e uma lista de cenas visuais que já possui, "
        "mas provavelmente são poucas para cobrir todo o roteiro.\n\n"
        "Sua tarefa é sugerir novas cenas que ele deveria baixar para melhorar o vídeo.\n\n"
        "Regras:\n"
        "- A saída DEVE ser um único objeto JSON válido.\n"
        "- Não repita as cenas que já existem na lista de cenas do usuário.\n"
        "- Foque em cenas visuais que ajudem a ilustrar partes importantes do roteiro "
        "  que hoje têm pouca ou nenhuma cobertura visual.\n"
        "- Pense no vídeo como um todo e proponha cenas variadas.\n"
        "- Para cada cena sugerida, indique um pequeno título, uma descrição mais detalhada, "
        "  um trecho de roteiro relacionado e uma prioridade (1 = muito importante, 5 = bem opcional).\n"
    )

    # podemos enviar também um resumo simples da timeline (onde tem ou não cena)
    timeline_resumida = []
    for item in timeline_dados.get("timeline", []):
        timeline_resumida.append(
            {
                "index": item.get("index"),
                "has_scene": bool(item.get("assigned_scene")),
                "script_fragment": item.get("script_fragment", ""),
                "assigned_scene": item.get("assigned_scene"),
            }
        )

    user_payload = {
        "roteiro_completo": roteiro_completo,
        "cenas_existentes": nomes_cenas_existentes,
        "timeline_resumida": timeline_resumida,
        "instrucoes_saida": {
            "extra_scenes": {
                "campos": [
                    "suggested_filename",
                    "short_title",
                    "description",
                    "script_fragment",
                    "priority",
                ],
                "quantidade_maxima": num_sugestoes,
            }
        },
    }

    completion = client.chat.completions.create(
        model=modelo,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "A seguir estão os dados em JSON. "
                    "Responda APENAS com um único objeto JSON contendo um array 'extra_scenes'.\n\n"
                    + json.dumps(user_payload, ensure_ascii=False)
                ),
            },
        ],
        temperature=0.7,
        response_format={"type": "json_object"},
    )

    conteudo = completion.choices[0].message.content
    try:
        dados = json.loads(conteudo)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Falha ao interpretar resposta do modelo como JSON (cenas adicionais).\n"
            f"Erro: {e}\n\nResposta bruta:\n{conteudo}"
        )

    if "extra_scenes" not in dados:
        dados.setdefault("extra_scenes", [])

    return dados

def copiar_e_renomear(
    timeline_dados: dict,
    mapa_cenas: dict[str, Path],
    pasta_saida: Path,
    max_palavras_no_nome: Optional[int] = 10,
) -> dict:
    """
    Cria os arquivos na pasta de saída com nomes baseados nos trechos do roteiro.
    Retorna um dict: nome_cena_original -> quantidade_de_usos
    """
    pasta_saida.mkdir(parents=True, exist_ok=True)

    timeline = timeline_dados.get("timeline", [])
    usados_por_cena: dict[str, int] = {}

    for item in timeline:
        index = item.get("index")
        script_fragment = item.get("script_fragment", "") or ""
        assigned_scene = item.get("assigned_scene")

        if not assigned_scene:
            # Trecho sem cena atribuída — nada a copiar
            continue

        if assigned_scene not in mapa_cenas:
            print(
                f"[AVISO] Cena atribuída '{assigned_scene}' não foi encontrada na pasta. "
                f"Pulando este item (index={index})."
            )
            continue

        origem = mapa_cenas[assigned_scene]
        ext = origem.suffix

        # agora o nome do arquivo é SOMENTE a frase tratada
        slug = slugify(script_fragment, max_palavras=max_palavras_no_nome)
        nome_final = f"{slug}{ext}"
        destino = pasta_saida / nome_final

        # se já existir um arquivo com esse nome, adiciona um sufixo numérico no FINAL
        contador = 2
        while destino.exists():
            nome_com_sufixo = f"{slug} ({contador}){ext}"
            destino = pasta_saida / nome_com_sufixo
            contador += 1

        shutil.copy2(origem, destino)

        usados_por_cena.setdefault(assigned_scene, 0)
        usados_por_cena[assigned_scene] += 1

    return usados_por_cena


# -----------------------------
# GUI
# -----------------------------


class RenomearCenasGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Bot GPT - Renomear cenas pelo roteiro")
        self.root.geometry("850x600")

        # Variáveis de interface
        self.var_pasta_cenas = tk.StringVar()
        self.var_roteiro = tk.StringVar()
        self.var_pasta_saida = tk.StringVar()
        self.var_modelo = tk.StringVar(value="gpt-4.1-mini")
        self.var_max_palavras_nome = tk.IntVar(value=10)
        self.var_max_usos_por_cena = tk.IntVar(value=3)
        self.var_frases_por_cena = tk.IntVar(value=2)  # padrão 2 frases por cena
        self.var_api_key = tk.StringVar()  # opcional: se vazio, usa OPENAI_API_KEY

        # Carrega preferências salvas (API key, pastas, etc.)
        prefs = carregar_prefs()
        api_salva = prefs.get("api_key", "").strip()
        if api_salva:
            self.var_api_key.set(api_salva)

        pasta_cenas_salva = prefs.get("pasta_cenas", "").strip()
        if pasta_cenas_salva:
            self.var_pasta_cenas.set(pasta_cenas_salva)

        pasta_saida_salva = prefs.get("pasta_saida", "").strip()
        if pasta_saida_salva:
            self.var_pasta_saida.set(pasta_saida_salva)

        self._criar_widgets()

    def _criar_widgets(self):
        padding = {"padx": 8, "pady": 4}

        frame_main = ttk.Frame(self.root)
        frame_main.pack(fill="both", expand=True, padx=10, pady=10)

        # Linha: API Key
        lbl_api = ttk.Label(frame_main, text="OpenAI API Key (opcional):")
        lbl_api.grid(row=0, column=0, sticky="w", **padding)
        entry_api = ttk.Entry(frame_main, textvariable=self.var_api_key, show="*", width=50)
        entry_api.grid(row=0, column=1, columnspan=2, sticky="we", **padding)
        lbl_api_hint = ttk.Label(
            frame_main,
            text="Se deixar em branco, dará erro.",
            foreground="gray",
        )
        lbl_api_hint.grid(row=1, column=1, columnspan=2, sticky="w", **padding)

        # Linha: pasta de cenas
        lbl_cenas = ttk.Label(frame_main, text="Pasta de cenas originais:")
        lbl_cenas.grid(row=2, column=0, sticky="w", **padding)
        entry_cenas = ttk.Entry(frame_main, textvariable=self.var_pasta_cenas, width=50)
        entry_cenas.grid(row=2, column=1, sticky="we", **padding)
        btn_cenas = ttk.Button(frame_main, text="Selecionar...", command=self._selecionar_pasta_cenas)
        btn_cenas.grid(row=2, column=2, **padding)

        # Linha: roteiro
        lbl_roteiro = ttk.Label(frame_main, text="Arquivo de roteiro (.txt):")
        lbl_roteiro.grid(row=3, column=0, sticky="w", **padding)
        entry_roteiro = ttk.Entry(frame_main, textvariable=self.var_roteiro, width=50)
        entry_roteiro.grid(row=3, column=1, sticky="we", **padding)
        btn_roteiro = ttk.Button(frame_main, text="Selecionar...", command=self._selecionar_roteiro)
        btn_roteiro.grid(row=3, column=2, **padding)

        # Linha: pasta de saída
        lbl_saida = ttk.Label(frame_main, text="Pasta de saída das cenas:")
        lbl_saida.grid(row=4, column=0, sticky="w", **padding)
        entry_saida = ttk.Entry(frame_main, textvariable=self.var_pasta_saida, width=50)
        entry_saida.grid(row=4, column=1, sticky="we", **padding)
        btn_saida = ttk.Button(frame_main, text="Selecionar...", command=self._selecionar_pasta_saida)
        btn_saida.grid(row=4, column=2, **padding)

        # Linha: modelo
        lbl_modelo = ttk.Label(frame_main, text="Modelo da OpenAI:")
        lbl_modelo.grid(row=5, column=0, sticky="w", **padding)
        combo_modelo = ttk.Combobox(
            frame_main,
            textvariable=self.var_modelo,
            values=[
                "gpt-4.1-mini",   # única opção
            ],
            state="readonly",
            width=20,
        )

        combo_modelo.grid(row=5, column=1, sticky="w", **padding)

        # Linha: max palavras no nome
        lbl_max_palavras = ttk.Label(frame_main, text="Máx. palavras no nome do arquivo:")
        lbl_max_palavras.grid(row=6, column=0, sticky="w", **padding)
        spin_palavras = ttk.Spinbox(
            frame_main,
            from_=0,
            to=50,
            textvariable=self.var_max_palavras_nome,
            width=6,
        )
        spin_palavras.grid(row=6, column=1, sticky="w", **padding)
        lbl_palavras_hint = ttk.Label(
            frame_main,
            text="Use 0 para não limitar por quantidade de palavras.",
            foreground="gray",
        )
        lbl_palavras_hint.grid(row=6, column=2, sticky="w", **padding)

        # Linha: max usos por cena
        lbl_max_usos = ttk.Label(frame_main, text="Máx. usos aproximados por cena:")
        lbl_max_usos.grid(row=7, column=0, sticky="w", **padding)
        spin_usos = ttk.Spinbox(
            frame_main,
            from_=1,
            to=50,
            textvariable=self.var_max_usos_por_cena,
            width=6,
        )
        spin_usos.grid(row=7, column=1, sticky="w", **padding)

        # NOVA linha: frases por cena
        lbl_frases = ttk.Label(frame_main, text="Frases por cena (pontos finais):")
        lbl_frases.grid(row=8, column=0, sticky="w", **padding)
        spin_frases = ttk.Spinbox(
            frame_main,
            from_=1,
            to=10,
            textvariable=self.var_frases_por_cena,
            width=6,
        )
        spin_frases.grid(row=8, column=1, sticky="w", **padding)

        # Botão Rodar
        self.btn_rodar = ttk.Button(frame_main, text="Rodar", command=self._on_rodar)
        self.btn_rodar.grid(row=9, column=0, columnspan=3, pady=(10, 10))

        # Separador
        sep = ttk.Separator(frame_main, orient="horizontal")
        sep.grid(row=10, column=0, columnspan=3, sticky="we", pady=(5, 5))

        # Log
        lbl_log = ttk.Label(frame_main, text="Log / Saída:")
        lbl_log.grid(row=11, column=0, sticky="w", **padding)

        frame_log = ttk.Frame(frame_main)
        frame_log.grid(row=12, column=0, columnspan=3, sticky="nsew", **padding)

        self.text_log = tk.Text(frame_log, height=15, wrap="word")
        self.text_log.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(frame_log, orient="vertical", command=self.text_log.yview)
        scrollbar.pack(side="right", fill="y")
        self.text_log.configure(yscrollcommand=scrollbar.set)

        # Permitir expansão do frame
        frame_main.rowconfigure(12, weight=1)
        frame_main.columnconfigure(1, weight=1)

    # --------- utilidades GUI ---------

    def log(self, mensagem: str):
        """
        Escreve no log da GUI (sempre chamado na thread principal).
        """
        self.text_log.insert("end", mensagem + "\n")
        self.text_log.see("end")

    def _selecionar_pasta_cenas(self):
        pasta = filedialog.askdirectory(title="Selecione a pasta de cenas originais")
        if pasta:
            self.var_pasta_cenas.set(pasta)
            # atualiza prefs com nova pasta de cenas
            prefs = carregar_prefs()
            prefs["pasta_cenas"] = pasta
            salvar_prefs(prefs)

    def _selecionar_roteiro(self):
        caminho = filedialog.askopenfilename(
            title="Selecione o arquivo de roteiro (.txt)",
            filetypes=[("Arquivos de texto", "*.txt"), ("Todos os arquivos", "*.*")],
        )
        if caminho:
            self.var_roteiro.set(caminho)

    def _selecionar_pasta_saida(self):
        pasta = filedialog.askdirectory(title="Selecione a pasta de saída")
        if pasta:
            self.var_pasta_saida.set(pasta)
            # atualiza prefs com nova pasta de saída
            prefs = carregar_prefs()
            prefs["pasta_saida"] = pasta
            salvar_prefs(prefs)

    # --------- fluxo principal ---------

    def _on_rodar(self):
        """
        Chama o processamento em uma thread separada para não travar a GUI.
        Também salva as preferências atuais (API key e pastas).
        """
        # salva prefs atuais
        prefs = carregar_prefs()
        pasta_cenas_str = self.var_pasta_cenas.get().strip()
        pasta_saida_str = self.var_pasta_saida.get().strip()
        api_atual = self.var_api_key.get().strip()

        if pasta_cenas_str:
            prefs["pasta_cenas"] = pasta_cenas_str
        if pasta_saida_str:
            prefs["pasta_saida"] = pasta_saida_str
        # só sobrescreve api_key se o campo não estiver vazio
        if api_atual:
            prefs["api_key"] = api_atual

        salvar_prefs(prefs)

        # Desabilita botão para evitar cliques repetidos
        self.btn_rodar.config(state="disabled")
        self.text_log.delete("1.0", "end")
        self.log("Iniciando processamento...")

        thread = threading.Thread(target=self._processar, daemon=True)
        thread.start()

    def _processar(self):
        try:
            pasta_cenas_str = self.var_pasta_cenas.get().strip()
            roteiro_str = self.var_roteiro.get().strip()
            pasta_saida_str = self.var_pasta_saida.get().strip()
            modelo = self.var_modelo.get().strip()
            max_palavras_nome = self.var_max_palavras_nome.get()
            max_usos_por_cena = self.var_max_usos_por_cena.get()
            frases_por_cena = self.var_frases_por_cena.get()  # quantidade de frases por cena
            api_key_gui = self.var_api_key.get().strip()

            if not pasta_cenas_str or not roteiro_str or not pasta_saida_str:
                raise ValueError("Preencha a pasta de cenas, o roteiro e a pasta de saída.")

            pasta_cenas = Path(pasta_cenas_str)
            roteiro_path = Path(roteiro_str)
            pasta_saida = Path(pasta_saida_str)

            # API Key
            api_key = api_key_gui or os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise EnvironmentError(
                    "Nenhuma API Key foi informada.\n"
                    "Preencha o campo 'OpenAI API Key' ou defina a variável de ambiente OPENAI_API_KEY."
                )

            client = OpenAI(api_key=api_key)

            # 1) Ler roteiro
            self._log_threadsafe("Lendo roteiro...")
            roteiro_texto = ler_roteiro(roteiro_path)

            # 2) Listar cenas
            self._log_threadsafe("Listando cenas disponíveis...")
            mapa_cenas, nomes_cenas = listar_cenas(pasta_cenas)
            self._log_threadsafe(f"Encontradas {len(nomes_cenas)} cenas.")

            self._log_threadsafe(f"Usando {frases_por_cena} frase(s) por cena (trecho).")

            # 3) Chamar modelo para montar timeline
            self._log_threadsafe(
                f"Chamando o modelo '{modelo}' para montar a timeline (isso consome tokens da API)..."
            )
            timeline_dados = chamar_gpt_montar_timeline(
                client=client,
                modelo=modelo,
                roteiro=roteiro_texto,
                nomes_cenas=nomes_cenas,
                max_usos_por_cena=max_usos_por_cena,
                frases_por_cena=frases_por_cena,
            )

            if "timeline" not in timeline_dados:
                raise RuntimeError("O modelo não retornou o campo 'timeline' na resposta.")

            # 4) Abrir janela de revisão visual antes de copiar/renomear
            self._log_threadsafe("Abrindo janela para revisar e ajustar as cenas de cada trecho...")

            review_done_event = threading.Event()
            updated_timeline_holder: dict[str, list[dict]] = {}

            # já podemos pegar as sugestões de cenas faltando aqui
            sugestoes_missing = timeline_dados.get("missing_scenes_suggestions", [])

            def abrir_revisao():
                def on_done(new_timeline: list[dict]):
                    # callback chamado quando o usuário clica em "Concluir revisão"
                    updated_timeline_holder["timeline"] = new_timeline
                    review_done_event.set()

                ReviewWindow(
                    master=self.root,
                    timeline=timeline_dados["timeline"],
                    scene_names=nomes_cenas,
                    scene_paths=mapa_cenas,
                    on_done=on_done,
                    missing_suggestions=sugestoes_missing,
                    client=client,
                    modelo=modelo,
                    max_usos_por_cena=max_usos_por_cena,
                    pasta_cenas=pasta_cenas,
                )

            # cria a janela na thread principal
            self.root.after(0, abrir_revisao)

            # espera (na thread em background) até o usuário concluir a revisão
            review_done_event.wait()

            # se veio timeline revisada, substitui
            if "timeline" in updated_timeline_holder:
                timeline_dados["timeline"] = updated_timeline_holder["timeline"]

            # 5) Copiar e renomear arquivos com base na timeline revisada
            self._log_threadsafe(f"Copiando e renomeando arquivos para '{pasta_saida}'...")
            usados_por_cena = copiar_e_renomear(
                timeline_dados=timeline_dados,
                mapa_cenas=mapa_cenas,
                pasta_saida=pasta_saida,
                max_palavras_no_nome=None if max_palavras_nome == 0 else max_palavras_nome,
            )

            # 6) Log de uso das cenas
            self._log_threadsafe("\nResumo de uso das cenas:")
            for nome, qtd in sorted(usados_por_cena.items(), key=lambda x: x[0]):
                self._log_threadsafe(f"- {nome}: usado {qtd} vez(es)")

            # 7) Sugestões do modelo (se você ainda estiver usando esse campo)
            sugestoes = timeline_dados.get("missing_scenes_suggestions", [])
            if sugestoes:
                self._log_threadsafe("\nSugestões de cenas que você poderia criar/baixar (por trecho):")
                self._log_threadsafe("-" * 80)
                for i, s in enumerate(sugestoes, start=1):
                    sf = (s.get("script_fragment") or "").strip()
                    desc = (s.get("suggested_scene_description") or "").strip()
                    self._log_threadsafe(f"{i:02d}. Trecho do roteiro:")
                    self._log_threadsafe(f"    {sf}")
                    self._log_threadsafe("   Cena sugerida:")
                    self._log_threadsafe(f"    {desc}")
                    self._log_threadsafe("-" * 80)
            else:
                self._log_threadsafe("\nNenhuma sugestão de cena faltante retornada pelo modelo.")

            # 8) Final
            self._log_threadsafe("\nPronto!")
            self._log_threadsafe(f"As cenas renomeadas estão em: {pasta_saida.resolve()}")

            self._mostrar_msg_threadsafe(
                "Processo concluído!",
                f"As cenas renomeadas foram geradas em:\n{pasta_saida.resolve()}",
            )

        except Exception as e:
            self._log_threadsafe("\nERRO:")
            self._log_threadsafe(str(e))
            self._mostrar_msg_threadsafe("Erro", str(e), error=True)
        finally:
            self.root.after(0, lambda: self.btn_rodar.config(state="normal"))

    # --------- helpers de thread ---------

    def _log_threadsafe(self, mensagem: str):
        self.root.after(0, lambda: self.log(mensagem))

    def _mostrar_msg_threadsafe(self, titulo: str, mensagem: str, error: bool = False):
        def _show():
            if error:
                messagebox.showerror(titulo, mensagem, parent=self.root)
            else:
                messagebox.showinfo(titulo, mensagem, parent=self.root)

        self.root.after(0, _show)


class ReviewWindow:
    """
    Janela para revisar a atribuição de cenas aos trechos do roteiro.

    - Esquerda:
        * Listbox com os trechos numerados
        * Abaixo, caixa de texto com o trecho completo (com quebra de linha)
    - Direita:
        * cards em 2+ colunas (responsivo)
        * checkbox + nome da cena
        * miniatura (imagem ou frame do vídeo)
        * para vídeos: preview acelerado ao passar o mouse
        * vídeos com fundo mais escuro para destacar
    """

    def __init__(self, master: tk.Tk, timeline: list[dict], scene_names: list[str],
                 scene_paths: dict[str, Path], on_done,
                 missing_suggestions: list[dict],
                 client: OpenAI, modelo: str, max_usos_por_cena: int, pasta_cenas: Path):
        self.master = master
        self.top = tk.Toplevel(master)
        self.top.title("Revisar cenas por frase")
        self.top.geometry("1400x650")
        self.top.grab_set()  # modal

        self.timeline = timeline              # lista de dicts com index, script_fragment, assigned_scene

        # normaliza index da timeline (se vier "12" como string, vira 12)
        for it in self.timeline:
            idx = it.get("index")
            if isinstance(idx, str) and idx.strip().isdigit():
                it["index"] = int(idx.strip())

            # lista de dicts com index, script_fragment, assigned_scene
        self.scene_names = scene_names        # lista de nomes de arquivo
        self.scene_paths = scene_paths        # mapa nome -> Path
        self.on_done = on_done                # callback ao concluir

        # dados extra para GPT e recarregar cenas
        self.missing_suggestions = missing_suggestions or []
        self.client = client
        self.modelo = modelo
        self.max_usos_por_cena = max_usos_por_cena
        self.pasta_cenas = pasta_cenas

        # índice -> sugestão completa (pra usar em _atualizar_dica)
        self.missing_suggestions_by_index: dict[int, dict] = {}

        for sug in (self.missing_suggestions or []):
            idx = sug.get("index")

            # se vier "12" como string, converte
            if isinstance(idx, str) and idx.strip().isdigit():
                idx = int(idx.strip())

            if isinstance(idx, int):
                self.missing_suggestions_by_index[idx] = sug

        # cena selecionada para o trecho atual
        self.selected_scene_var = tk.StringVar(value="")

        # filtro de exibição dos trechos
        self.filter_var = tk.StringVar(value="Mostrar todos")
        # mapeia a linha visível do Listbox -> índice real em self.timeline
        self.filtered_indices: list[int] = []

        # vars dos checkboxes de cada cena
        self.scene_vars: dict[str, tk.IntVar] = {}

        # referência para cada "card" de cena
        self.scene_cards: dict[str, tk.Widget] = {}

        # cache de miniaturas (para IMAGENS)
        self.thumbnails: dict[str, "ImageTk.PhotoImage"] = {}

        # cache de miniaturas de VÍDEO (frame do meio, por exemplo)
        self.video_thumbs: dict[str, "ImageTk.PhotoImage"] = {}

        # labels de vídeo (para saber qual atualizar/limpar)
        self.video_labels: dict[str, tk.Label] = {}

        # controle de preview por hover (apenas 1 vídeo de cada vez)
        self.hover_scene_name: Optional[str] = None
        self.hover_cap = None
        self.hover_running = False

        # controle de layout dinâmico das colunas de cenas
        self._last_cols_count: Optional[int] = None

        # se fechar na cruz, age como "Concluir" (pra não travar a thread que espera)
        self.top.protocol("WM_DELETE_WINDOW", self.concluir)

        # ---------- layout geral ----------
        self.top.columnconfigure(0, weight=1)
        self.top.columnconfigure(1, weight=1)
        self.top.rowconfigure(0, weight=1)

        # ---------- layout esquerdo ----------
        frame_left = ttk.Frame(self.top)
        frame_left.pack(side="left", fill="y", expand=False, padx=5, pady=5)

        ttk.Label(frame_left, text="Trechos do roteiro").pack(anchor="w")

        # linha do filtro (opções lado a lado)
        frame_filter = ttk.Frame(frame_left)
        frame_filter.pack(fill="x", pady=(0, 5))

        ttk.Label(frame_filter, text="Exibir:").pack(side="left", padx=(0, 4))

        rb_todos = ttk.Radiobutton(
            frame_filter,
            text="Todos",
            value="Mostrar todos",
            variable=self.filter_var,
            command=self._on_filter_change,
        )
        rb_faltando = ttk.Radiobutton(
            frame_filter,
            text="Faltando cenas",
            value="Mostrar Faltando Cenas",
            variable=self.filter_var,
            command=self._on_filter_change,
        )
        rb_atribuidas = ttk.Radiobutton(
            frame_filter,
            text="Com cenas",
            value="Mostrar Cenas Atribuidas",
            variable=self.filter_var,
            command=self._on_filter_change,
        )

        rb_todos.pack(side="left")
        rb_faltando.pack(side="left", padx=(4, 0))
        rb_atribuidas.pack(side="left", padx=(4, 0))


        # top: listbox + scroll
        frame_left_top = ttk.Frame(frame_left)
        frame_left_top.pack(fill="both", expand=True)

        # largura fixa em caracteres para a coluna de trechos
        self.list_trechos = tk.Listbox(frame_left_top, exportselection=False, width=38)
        self.list_trechos.pack(side="left", fill="both", expand=True)

        scroll_trechos = ttk.Scrollbar(frame_left_top, orient="vertical", command=self.list_trechos.yview)
        scroll_trechos.pack(side="right", fill="y")
        self.list_trechos.configure(yscrollcommand=scroll_trechos.set)

        self.list_trechos.bind("<<ListboxSelect>>", self.on_select_trecho)

        # bottom: caixa de texto para mostrar o trecho completo com quebra de linha
        frame_left_bottom = ttk.Frame(frame_left)
        frame_left_bottom.pack(fill="both", expand=False, pady=(5, 0))

        ttk.Label(frame_left_bottom, text="Trecho selecionado (texto completo):").pack(anchor="w")

        self.text_trecho = tk.Text(frame_left_bottom, height=6, wrap="word")
        self.text_trecho.pack(fill="both", expand=True)
        self.text_trecho.configure(state="disabled")

        # dicas de cena para trechos sem vídeo
        self.lbl_dica_oque = ttk.Label(
            frame_left_bottom,
            text="",
            wraplength=380,
            justify="left",
            foreground="blue",
        )
        self.lbl_dica_oque.pack(anchor="w", pady=(4, 0))

        self.lbl_dica_pesquisar = ttk.Label(
            frame_left_bottom,
            text="",
            wraplength=380,
            justify="left",
            foreground="gray25",
        )
        self.lbl_dica_pesquisar.pack(anchor="w")

        # botão para ver todas as sugestões de cenas faltando
        btn_ver_todas = ttk.Button(
            frame_left_bottom,
            text="Ver todas as dicas de cenas",
            command=self._ver_todas_cenas_sugeridas,
        )
        btn_ver_todas.pack(anchor="w", pady=(4, 0))

        # ---------- layout direito: cenas (cards em N colunas) ----------
        frame_right = ttk.Frame(self.top)
        frame_right.pack(side="left", fill="both", expand=True, padx=5, pady=5)

        ttk.Label(frame_right, text="Cenas disponíveis").pack(anchor="w")
        # frame rolável
        frame_cenas_outer = ttk.Frame(frame_right)
        frame_cenas_outer.pack(fill="both", expand=True)

        self.canvas_cenas = tk.Canvas(frame_cenas_outer)
        self.canvas_cenas.bind("<Enter>", lambda e: self._bind_mousewheel())
        self.canvas_cenas.bind("<Leave>", lambda e: self._unbind_mousewheel())

        scroll_cenas = ttk.Scrollbar(frame_cenas_outer, orient="vertical", command=self.canvas_cenas.yview)
        self.canvas_cenas.configure(yscrollcommand=scroll_cenas.set)

        self.canvas_cenas.pack(side="left", fill="both", expand=True)
        scroll_cenas.pack(side="right", fill="y")

        self.scenes_container = ttk.Frame(self.canvas_cenas)
        # guarda o id da janela dentro do canvas pra podermos mudar a largura depois
        self.scenes_window_id = self.canvas_cenas.create_window(
            (0, 0), window=self.scenes_container, anchor="nw"
        )

        # quando o conteúdo mudar de tamanho (mais cards etc.), só atualiza a região de scroll
        def _on_frame_config(event):
            self.canvas_cenas.configure(scrollregion=self.canvas_cenas.bbox("all"))

        self.scenes_container.bind("<Configure>", _on_frame_config)

        # quando o CANVAS muda de tamanho (resize da janela), aí sim recalculamos as colunas
        def _on_canvas_config(event):
            # faz o container ocupar toda a largura visível do canvas
            self.canvas_cenas.itemconfig(self.scenes_window_id, width=event.width)
            # recalcula as colunas com base na largura visível
            self._rebuild_scene_grid(event.width)

        self.canvas_cenas.bind("<Configure>", _on_canvas_config)

        # construir os "cards" de cena (posição das colunas será calculada dinamicamente)
        self._build_scene_items()

        frame_ctrl = ttk.Frame(frame_right)
        frame_ctrl.pack(fill="x", pady=5)

        self.lbl_cena_atual = ttk.Label(frame_ctrl, text="Cena atual: (nenhuma)")
        self.lbl_cena_atual.pack(anchor="w")

        btn_play = ttk.Button(frame_ctrl, text="Play cena selecionada", command=self.play_cena)
        btn_play.pack(side="left", padx=2)

        btn_reload = ttk.Button(
            frame_ctrl,
            text="Recarregar cenas da pasta",
            command=self._recarregar_cenas,
        )
        btn_reload.pack(side="left", padx=4)

        btn_refazer = ttk.Button(
            frame_ctrl,
            text="Reprocessar trechos SEM cena (GPT)",
            command=self._reprocessar_trechos_sem_cena,
        )
        btn_refazer.pack(side="left", padx=4)

        btn_ok = ttk.Button(frame_ctrl, text="Concluir revisão", command=self.concluir)
        btn_ok.pack(side="left", padx=4)



        # monta a lista de trechos de acordo com o filtro inicial
        self._rebuild_trechos_list()

    def _recarregar_cenas(self):
        """
        Rele a pasta de cenas (incluindo subpastas) e atualiza a lista de cenas/card.
        Útil quando você baixa arquivos novos enquanto a janela está aberta.
        """
        try:
            mapa_cenas, nomes_cenas = listar_cenas(self.pasta_cenas)
        except Exception as e:
            messagebox.showerror(
                "Erro ao recarregar cenas",
                f"Ocorreu um erro ao recarregar a pasta de cenas:\n{e}",
                parent=self.top,
            )
            return

        # atualiza estruturas
        self.scene_paths = mapa_cenas
        self.scene_names = nomes_cenas

        # limpa caches e widgets de cards
        self.scene_vars.clear()
        self.scene_cards.clear()
        self.thumbnails.clear()
        self.video_thumbs.clear()
        self.video_labels.clear()
        for w in self.scenes_container.winfo_children():
            w.destroy()

        self._last_cols_count = None
        self._build_scene_items()

        messagebox.showinfo(
            "Cenas recarregadas",
            f"Foram encontradas {len(self.scene_names)} cenas na pasta (incluindo subpastas).",
            parent=self.top,
        )

    def _reprocessar_trechos_sem_cena(self):
        """
        Chama o GPT de novo SÓ para trechos que ainda não têm 'assigned_scene'.
        Usa as cenas atuais (já recarregadas, se você usou o botão).
        """
        # monta lista de trechos sem cena
        trechos_sem_cena = []
        for item in self.timeline:
            if item.get("assigned_scene"):
                continue
            frag = (item.get("script_fragment") or "").strip()
            if not frag:
                continue
            trechos_sem_cena.append({
                "index": item.get("index"),
                "texto": frag,
            })

        if not trechos_sem_cena:
            messagebox.showinfo(
                "Reprocessar trechos",
                "Não há trechos sem cena para reprocessar.",
                parent=self.top,
            )
            return

        if not self.scene_names:
            messagebox.showerror(
                "Reprocessar trechos",
                "Nenhuma cena disponível foi encontrada ao recarregar a pasta.",
                parent=self.top,
            )
            return

        # monta prompt específico só para esses trechos
        system_prompt = (
            "Você ajuda na edição de vídeo.\n"
            "Você receberá UMA lista de trechos de roteiro (cada um com um índice) "
            "e uma lista de cenas visuais (arquivos de vídeo/imagem) onde o nome do "
            "arquivo descreve a cena.\n\n"
            "Sua tarefa é, para CADA trecho, decidir qual cena usar (ou nenhuma).\n\n"
            "Regras importantes:\n"
            "- A resposta DEVE ser um único objeto JSON válido.\n"
            "- A saída deve ter um array 'timeline'.\n"
            "- Cada item da 'timeline' deve ter:\n"
            "  - 'index': o mesmo índice do trecho recebido.\n"
            "  - 'script_fragment': o texto completo do trecho recebido.\n"
            "  - 'assigned_scene':\n"
            "      * null se nenhuma cena disponível encaixa bem naquele trecho;\n"
            "      * ou um dos nomes de arquivo exatamente como fornecidos na lista de cenas.\n"
            "- Você pode reutilizar a mesma cena em vários trechos diferentes, "
            "  mas NÃO use a mesma cena em dois trechos consecutivos ao longo do vídeo.\n"
            "- Use português simples e direto.\n"
        )

        user_payload = {
            "trechos_roteiro": trechos_sem_cena,
            "cenas_disponiveis": self.scene_names,
            "restricoes": {
                "max_usos_por_cena": self.max_usos_por_cena,
                "observacao": (
                    "não é obrigatório usar todas as cenas; priorize encaixe semântico. "
                    "Evite usar a mesma cena em dois trechos consecutivos."
                ),
            },
        }

        try:
            completion = self.client.chat.completions.create(
                model=self.modelo,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": (
                            "A seguir estão os dados em JSON. "
                            "Responda APENAS com um único objeto JSON.\n\n"
                            + json.dumps(user_payload, ensure_ascii=False)
                        ),
                    },
                ],
                temperature=0.4,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            messagebox.showerror(
                "Erro ao chamar GPT",
                f"Ocorreu um erro ao chamar o modelo:\n{e}",
                parent=self.top,
            )
            return

        conteudo = completion.choices[0].message.content
        try:
            dados = json.loads(conteudo)
        except json.JSONDecodeError as e:
            messagebox.showerror(
                "Erro no retorno do GPT",
                f"Não foi possível interpretar o JSON retornado:\n{e}\n\nResposta bruta:\n{conteudo}",
                parent=self.top,
            )
            return

        nova_timeline = dados.get("timeline")
        if not isinstance(nova_timeline, list):
            messagebox.showerror(
                "Erro no retorno do GPT",
                "JSON retornado não contém um array 'timeline' válido.",
                parent=self.top,
            )
            return

        # cria mapa index -> assigned_scene sugerida
        sugestoes_por_index: dict[int, Optional[str]] = {}
        for item in nova_timeline:
            idx = item.get("index")
            scene = item.get("assigned_scene")
            if scene is not None and scene == "":
                scene = None

            if isinstance(idx, str) and idx.strip().isdigit():
                idx = int(idx.strip())

            if isinstance(idx, int):
                sugestoes_por_index[idx] = scene

        # aplica as sugestões na timeline completa,
        # respeitando a regra de não repetir cena em trechos consecutivos
        last_scene = None
        for item in self.timeline:
            idx = item.get("index")

            if isinstance(idx, str) and idx.strip().isdigit():
                idx = int(idx.strip())
                item["index"] = idx  # já aproveita e salva normalizado

            if not isinstance(idx, int):
                continue


            # se esse índice recebeu uma sugestão nova, sobrescreve
            if idx in sugestoes_por_index:
                item["assigned_scene"] = sugestoes_por_index[idx]

            scene = item.get("assigned_scene")
            if scene is not None and scene == "":
                scene = None

            # regra de não repetir cena consecutiva
            if scene is not None and scene == last_scene:
                # se foi sugerido agora, limpamos
                if idx in sugestoes_por_index:
                    item["assigned_scene"] = None
                    scene = None

            last_scene = scene

        # atualiza visual (lista de trechos e dicas do trecho selecionado)
        self._rebuild_trechos_list()
        messagebox.showinfo(
            "Reprocessar trechos",
            "Trechos sem cena foram reenviados ao GPT e atualizados.",
            parent=self.top,
        )




    def _on_filter_change(self, event=None):
        """Chamado quando o usuário troca a opção do filtro."""
        self._rebuild_trechos_list()

    def _build_scene_items(self):
        """
        Cria os cards de cenas, mas deixa a organização (linhas/colunas)
        para _rebuild_scene_grid, que calcula dinamicamente o número de colunas.
        """
        for name in self.scene_names:
            # evita recriar se já existir (defensivo, caso chame de novo)
            if name in self.scene_cards:
                continue

            card = ttk.Frame(self.scenes_container, borderwidth=1, relief="solid", padding=4)
            self.scene_cards[name] = card

            # linha de cima: checkbox + nome da cena
            var = tk.IntVar(value=0)
            self.scene_vars[name] = var

            frame_top = ttk.Frame(card)
            frame_top.pack(fill="x")

            chk = ttk.Checkbutton(
                frame_top,
                variable=var,
                command=lambda n=name: self._on_scene_checkbox_changed(n),
            )
            chk.pack(side="left")

            lbl_name = ttk.Label(frame_top, text=name, wraplength=220, justify="left")
            lbl_name.pack(side="left", padx=4)

            # área de visualização (miniatura ou vídeo com hover)
            path = self.scene_paths.get(name)
            ext = path.suffix.lower() if path else ""

            if path and ext in IMAGE_EXTS:
                # miniatura estática para imagens
                thumb = self._get_thumbnail_for_scene(name)
                if thumb is not None:
                    lbl_thumb = ttk.Label(card, image=thumb)
                    lbl_thumb.image = thumb  # manter referência
                else:
                    lbl_thumb = tk.Label(
                        card,
                        width=30,
                        height=8,
                        bg="black",
                        fg="white",
                        text="prévia indisponível",
                    )

            elif path and ext in VIDEO_EXTS and CV2_AVAILABLE and PIL_AVAILABLE:
                # VÍDEO: miniatura + preview ao passar o mouse
                thumb = self._get_video_thumbnail(name, path)
                if thumb is not None:
                    lbl_thumb = tk.Label(
                        card,
                        bg="#82B7EB",  # destaque para vídeos
                    )
                    lbl_thumb.configure(image=thumb)
                    lbl_thumb.image = thumb
                else:
                    lbl_thumb = tk.Label(
                        card,
                        width=30,
                        height=8,
                        bg="#333333",   # fundo cinza escuro para vídeos
                        fg="white",
                        text="Passe o mouse\npara prévia",
                        justify="center",
                    )

                # guardar label e binds de hover
                self.video_labels[name] = lbl_thumb
                lbl_thumb.bind(
                    "<Enter>",
                    lambda e, n=name, p=path, l=lbl_thumb: self._on_video_enter(n, p, l),
                )
                lbl_thumb.bind(
                    "<Leave>",
                    lambda e, n=name: self._on_video_leave(n),
                )

            else:
                # fallback (sem suporte a video ou sem thumbnail)
                lbl_thumb = tk.Label(
                    card,
                    width=30,
                    height=8,
                    bg="black",
                    fg="white",
                    text="prévia indisponível",
                )

            lbl_thumb.pack(fill="both", expand=True, pady=(4, 0))

            # permitir selecionar a cena clicando em qualquer parte do card
            for w in (card, lbl_thumb, lbl_name):
                w.bind(
                    "<Button-1>",
                    lambda e, n=name: self._on_scene_checkbox_changed(n)
                )

        # posiciona os cards conforme a largura atual
        self._rebuild_scene_grid()



    def _rebuild_scene_grid(self, container_width: Optional[int] = None):
        """
        Reorganiza os cards de cenas em N colunas, de forma dinâmica,
        conforme a largura disponível do container.
        """
        if not self.scene_cards:
            return

        # largura real do container se não vier no evento
        if container_width is None or container_width <= 0:
            container_width = self.scenes_container.winfo_width() or 1

        # largura mínima "ideal" de cada card (ajuste como quiser)
        card_min_width = 280
        cols = max(1, container_width // card_min_width)

        # evita recalcular se o número de colunas não mudou
        if self._last_cols_count == cols:
            return
        self._last_cols_count = cols

        # zera pesos de muitas colunas (defensivo)
        for c in range(0, 20):
            self.scenes_container.grid_columnconfigure(c, weight=0)

        # define pesos para as colunas usadas agora
        for c in range(cols):
            self.scenes_container.grid_columnconfigure(c, weight=1)

        # reposiciona todos os cards
        row = 0
        col = 0
        for name in self.scene_names:
            card = self.scene_cards.get(name)
            if not card:
                continue

            card.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")

            col += 1
            if col >= cols:
                col = 0
                row += 1

    def _get_thumbnail_for_scene(self, scene_name: str):
        """
        Miniaturas só para IMAGENS (png/jpg/webp...).
        Para vídeos, usamos _get_video_thumbnail.
        """
        if not PIL_AVAILABLE:
            return None

        if scene_name in self.thumbnails:
            return self.thumbnails[scene_name]

        path = self.scene_paths.get(scene_name)
        if not path or not path.exists():
            return None

        if path.suffix.lower() not in IMAGE_EXTS:
            # não tentar miniatura para vídeo
            return None

        try:
            img = Image.open(path)
            img.thumbnail((220, 120))
            photo = ImageTk.PhotoImage(img)
            self.thumbnails[scene_name] = photo
            return photo
        except Exception:
            return None

    def _get_video_thumbnail(self, scene_name: str, path: Path):
        """
        Gera (e cacheia) uma miniatura para VÍDEO,
        pegando um frame aproximado do meio do vídeo.
        """
        if not (CV2_AVAILABLE and PIL_AVAILABLE):
            return None

        if scene_name in self.video_thumbs:
            return self.video_thumbs[scene_name]

        cap = None
        try:
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                return None

            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            if frame_count > 0:
                mid_frame = frame_count // 2
            else:
                mid_frame = 0

            cap.set(cv2.CAP_PROP_POS_FRAMES, mid_frame)
            ret, frame = cap.read()
            if not ret or frame is None:
                return None

            max_w, max_h = 220, 120
            h, w, _ = frame.shape
            scale = min(max_w / w, max_h / h)
            new_w, new_h = int(w * scale), int(h * scale)

            frame_resized = cv2.resize(frame, (new_w, new_h))
            frame_rgb = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb)
            photo = ImageTk.PhotoImage(img)

            self.video_thumbs[scene_name] = photo
            return photo
        except Exception:
            return None
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

    # ---------- preview de vídeo com hover ----------

    def _stop_hover_play(self):
        """Para o preview atual (se houver) e volta para a miniatura do vídeo (se existir)."""
        self.hover_running = False
        if self.hover_cap is not None:
            try:
                self.hover_cap.release()
            except Exception:
                pass
            self.hover_cap = None

        # voltar miniatura ou texto padrão
        if self.hover_scene_name and self.hover_scene_name in self.video_labels:
            label = self.video_labels[self.hover_scene_name]
            thumb = self.video_thumbs.get(self.hover_scene_name)
            if thumb is not None:
                label.configure(image=thumb, bg="#82B7EB", fg="black", text="")
                label.image = thumb
            else:
                label.configure(text="Passe o mouse\npara prévia", image="", bg="#82B7EB", fg="black")
                label.image = None

        self.hover_scene_name = None

    def _on_video_enter(self, scene_name: str, path: Path, label: tk.Label):
        """Mouse entrou em cima de um card de vídeo -> inicia preview acelerado (~2x–3x)."""
        if not (CV2_AVAILABLE and PIL_AVAILABLE):
            return

        # para qualquer preview anterior
        self._stop_hover_play()

        # abre novo vídeo
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return

        self.hover_scene_name = scene_name
        self.hover_cap = cap
        self.hover_running = True

        max_w, max_h = 220, 120   # tamanho da caixinha
        delay_ms = 50             # 50ms por update (~20 updates/segundo)

        def update_frame():
            if not self.hover_running or self.hover_cap is None or self.hover_scene_name != scene_name:
                return

            # pular alguns frames para aproximar 2x de velocidade
            frame = None
            for _ in range(3):  # lê ~3 frames (~99ms de vídeo) por atualização → ~2x
                ret, f = self.hover_cap.read()
                if not ret:
                    # recomeça vídeo
                    self.hover_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, f = self.hover_cap.read()
                    if not ret:
                        return
                frame = f

            if frame is None:
                return

            try:
                h, w, _ = frame.shape
                scale = min(max_w / w, max_h / h)
                new_w, new_h = int(w * scale), int(h * scale)

                frame_resized = cv2.resize(frame, (new_w, new_h))
                frame_rgb = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame_rgb)
                photo = ImageTk.PhotoImage(img)
            except Exception:
                return

            label.configure(image=photo, bg="#82B7EB", fg="black", text="")
            label.image = photo  # evitar GC

            label.after(delay_ms, update_frame)

        update_frame()

    def _on_video_leave(self, scene_name: str):
        """Mouse saiu de cima do card de vídeo -> para preview e volta miniatura."""
        if self.hover_scene_name == scene_name:
            self._stop_hover_play()

    # ---------- rolagem até a cena atribuída ----------

    def _scroll_to_scene(self, scene_name: str):
        """
        Rola a área de cenas para que o card da cena apareça visível.
        """
        card = self.scene_cards.get(scene_name)
        if not card:
            return

        self.canvas_cenas.update_idletasks()
        self.scenes_container.update_idletasks()

        try:
            y = card.winfo_y()
            total_height = max(self.scenes_container.winfo_height(), 1)
            frac = y / total_height
            frac = max(0.0, min(1.0, frac))
            self.canvas_cenas.yview_moveto(frac)
        except Exception:
            pass

    # ---------- mouse wheel scroll cenas ----------
    def _on_mousewheel(self, event):
        # Windows e Mac
        if event.delta:
            self.canvas_cenas.yview_scroll(-1 * int(event.delta / 120), "units")
        else:
            # Linux
            if event.num == 4:
                self.canvas_cenas.yview_scroll(-2, "units")
            elif event.num == 5:
                self.canvas_cenas.yview_scroll(2, "units")

    def _bind_mousewheel(self):
        self.canvas_cenas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas_cenas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas_cenas.bind_all("<Button-5>", self._on_mousewheel)

    def _unbind_mousewheel(self):
        self.canvas_cenas.unbind_all("<MouseWheel>")
        self.canvas_cenas.unbind_all("<Button-4>")
        self.canvas_cenas.unbind_all("<Button-5>")


    # ---------- helpers internos ----------

    def _rebuild_trechos_list(self):
        """
        Reconstrói o Listbox de trechos conforme o filtro selecionado:
        - Mostrar todos
        - Mostrar Faltando Cenas
        - Mostrar Cenas Atribuidas
        """
        modo = self.filter_var.get()

        self.list_trechos.delete(0, "end")
        self.filtered_indices = []

        for idx, item in enumerate(self.timeline):
            has_scene = bool(item.get("assigned_scene"))

            if modo == "Mostrar Faltando Cenas" and has_scene:
                continue
            if modo == "Mostrar Cenas Atribuidas" and not has_scene:
                continue

            frag = (item.get("script_fragment") or "").strip().replace("\n", " ")
            max_chars = 80
            if len(frag) > max_chars:
                frag = frag[:max_chars - 3] + "..."

            display = f"{item.get('index'):03d} - {frag}"
            row = self.list_trechos.size()
            self.list_trechos.insert("end", display)

            # cor: vermelho se estiver sem cena
            if has_scene:
                self.list_trechos.itemconfig(row, fg="black")
            else:
                self.list_trechos.itemconfig(row, fg="red")

            self.filtered_indices.append(idx)

        # limpa seleção, texto e dicas
        self.list_trechos.selection_clear(0, "end")
        self.selected_scene_var.set("")
        self.lbl_cena_atual.config(text="Cena atual: (nenhuma)")
        self._atualizar_texto_trecho("")

        # limpa dicas
        self._atualizar_dica(None)


    def _get_trecho_index(self) -> Optional[int]:
        """
        Retorna o índice REAL em self.timeline do trecho selecionado no Listbox,
        levando em conta o filtro (self.filtered_indices).
        """
        sel = self.list_trechos.curselection()
        if not sel:
            return None
        row = sel[0]
        if row < 0 or row >= len(self.filtered_indices):
            return None
        return self.filtered_indices[row]

    def _get_selected_row(self) -> Optional[int]:
        """Retorna o índice da linha selecionada no Listbox (linha visível)."""
        sel = self.list_trechos.curselection()
        if not sel:
            return None
        return sel[0]

    def _atualizar_texto_trecho(self, texto: str):
        """Atualiza a caixa de texto com o trecho completo (com quebra de linha)."""
        self.text_trecho.configure(state="normal")
        self.text_trecho.delete("1.0", "end")
        self.text_trecho.insert("1.0", texto)
        self.text_trecho.configure(state="disabled")

    def _atualizar_dica(self, idx_timeline: Optional[int]):
        """Atualiza as dicas 'O que deve ter na cena' e 'Pesquisar' para o trecho selecionado."""
        if idx_timeline is None:
            self.lbl_dica_oque.config(text="")
            self.lbl_dica_pesquisar.config(text="")
            return

        item = self.timeline[idx_timeline]
        trecho_index = item.get("index")
        has_scene = bool(item.get("assigned_scene"))

        # só mostra dica se NÃO tiver cena atribuída
        if has_scene:
            self.lbl_dica_oque.config(text="")
            self.lbl_dica_pesquisar.config(text="")
            return

        sug = None

        if isinstance(trecho_index, str) and trecho_index.strip().isdigit():
            trecho_index = int(trecho_index.strip())

        if isinstance(trecho_index, int):
            sug = self.missing_suggestions_by_index.get(trecho_index)


        if not sug:
            self.lbl_dica_oque.config(text="")
            self.lbl_dica_pesquisar.config(text="")
            return

        desc = (sug.get("suggested_scene_description") or "").strip()
        search = (sug.get("search_suggestion") or "").strip()

        if desc:
            self.lbl_dica_oque.config(text=f"O que deve ter na cena:\n{desc}")
        else:
            self.lbl_dica_oque.config(text="")

        if search:
            self.lbl_dica_pesquisar.config(text=f"Pesquisar:\n{search}")
        else:
            self.lbl_dica_pesquisar.config(text="")

    def _ver_todas_cenas_sugeridas(self):
        """Abre uma janela com a lista de todas as sugestões de cenas faltando."""
        if not self.missing_suggestions:
            messagebox.showinfo(
                "Nenhuma sugestão",
                "Não há sugestões de cenas faltando retornadas pelo modelo.",
                parent=self.top,
            )
            return

        win = tk.Toplevel(self.top)
        win.title("Cenas sugeridas para baixar")
        win.geometry("800x500")

        txt = tk.Text(win, wrap="word")
        txt.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        scroll.pack(side="right", fill="y")
        txt.configure(yscrollcommand=scroll.set)

        # monta um texto organizado por ordem de index
        def _sort_key(sug):
            idx = sug.get("index")
            if isinstance(idx, str) and idx.strip().isdigit():
                return int(idx.strip())
            if isinstance(idx, int):
                return idx
            return 999999

        ordenadas = sorted(self.missing_suggestions, key=_sort_key)


        for sug in ordenadas:
            idx = sug.get("index")
            frag = (sug.get("script_fragment") or "").strip()
            desc = (sug.get("suggested_scene_description") or "").strip()
            search = (sug.get("search_suggestion") or "").strip()

            txt.insert("end", f"Trecho {idx}:\n")
            if frag:
                txt.insert("end", f"Roteiro:\n{frag}\n\n")
            if desc:
                txt.insert("end", f"O que deve ter na cena:\n{desc}\n\n")
            if search:
                txt.insert("end", f"Pesquisar:\n{search}\n\n")
            txt.insert("end", "-" * 80 + "\n\n")

        txt.config(state="disabled")

    def on_select_trecho(self, event=None):
        """
        Quando clica num trecho à esquerda:
        - mostra qual cena está associada,
        - marca o checkbox dessa cena,
        - rola a lista de cenas até o card correspondente,
        - atualiza a caixa de texto com o trecho completo.
        """
        idx = self._get_trecho_index()
        row = self._get_selected_row()
        if idx is None:
            return

        # texto completo do trecho
        item = self.timeline[idx]
        frag_completo = (item.get("script_fragment") or "").strip()
        self._atualizar_texto_trecho(frag_completo)

        # limpa todos os checkboxes
        for v in self.scene_vars.values():
            v.set(0)

        cena = item.get("assigned_scene")

        if cena:
            self.selected_scene_var.set(cena)
            self.lbl_cena_atual.config(text=f"Cena atual: {cena}")
            if cena in self.scene_vars:
                self.scene_vars[cena].set(1)
            # trecho com cena -> preto
            if row is not None:
                self.list_trechos.itemconfig(row, fg="black")
            # rola até o card da cena
            self._scroll_to_scene(cena)
        else:
            self.selected_scene_var.set("")
            self.lbl_cena_atual.config(text="Cena atual: (nenhuma)")
            # trecho sem cena -> vermelho
            if row is not None:
                self.list_trechos.itemconfig(row, fg="red")

        # atualiza dicas para o trecho selecionado
        self._atualizar_dica(idx)


    def _on_scene_checkbox_changed(self, scene_name: str):
        """
        Quando clica em um checkbox de cena (ou em qualquer parte do card):
        - marca só aquele,
        - atribui essa cena ao trecho atual.
        """
        idx_trecho = self._get_trecho_index()
        row = self._get_selected_row()
        if idx_trecho is None:
            # se não houver trecho selecionado, desfaz o clique
            if scene_name in self.scene_vars:
                self.scene_vars[scene_name].set(0)
            messagebox.showinfo("Selecione um trecho", "Selecione um trecho do roteiro à esquerda.")
            return

        # desmarca todos os outros checkboxes
        for name, var in self.scene_vars.items():
            if name != scene_name:
                var.set(0)

        # marca o atual
        self.scene_vars[scene_name].set(1)
        self.selected_scene_var.set(scene_name)

        # atualiza timeline
        self.timeline[idx_trecho]["assigned_scene"] = scene_name

        self.lbl_cena_atual.config(text=f"Cena atual: {scene_name}")
        if row is not None:
            self.list_trechos.itemconfig(row, fg="black")

        # se o filtro for "Mostrar Faltando Cenas", assim que você atribuir,
        # esse trecho some automaticamente da lista (faz sentido visualmente).
        self._rebuild_trechos_list()


    def play_cena(self):
        """
        Abre no player padrão a cena atualmente marcada (checkbox selecionado).
        """
        cena_name = self.selected_scene_var.get().strip()
        if not cena_name:
            messagebox.showinfo("Selecione uma cena", "Selecione uma cena (caixa de seleção) para reproduzir.")
            return

        path = self.scene_paths.get(cena_name)
        if not path or not path.exists():
            messagebox.showerror("Arquivo não encontrado", f"Arquivo da cena não foi encontrado:\n{cena_name}")
            return

        try:
            abrir_arquivo(path)
        except Exception as e:
            messagebox.showerror("Erro ao abrir arquivo", f"Não foi possível abrir a cena:\n{e}")

    def concluir(self):
        """
        Chama o callback com a timeline modificada, para o preview de vídeo e fecha a janela.
        """
        self._stop_hover_play()

        if self.on_done:
            cb = self.on_done
            self.on_done = None
            cb(self.timeline)

        self.top.destroy()

def main():
    root = tk.Tk()
    app = RenomearCenasGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()