"""
Cliente de consumo das credenciais cifradas hospedadas no painel cronometro-web.

Politica operacional (manual-credenciais, secao 9.7):
- user_id + chave ficam apenas em memoria;
- valor decifrado fica em cache apenas em memoria, descartado ao encerrar;
- nada persistido em disco;
- nada logado (valor, cipher, nonce, user_id, chave).

API publica:
    set_credenciais_usuario(user_id, chave)
    obter_credencial(identificador) -> str
    listar_credenciais() -> list[dict]
    status_credencial(identificador) -> (ok, mensagem)
    limpar_cache()

Identificadores (slugs) do servidor:
    SLUG_OPENAI      = "chatgpt"
    SLUG_ASSEMBLY    = "assembly"
    SLUG_GEMINI      = "gemini"
"""
from __future__ import annotations

import base64
import time
import threading
from typing import Optional

import requests
from nacl.secret import SecretBox
from nacl.exceptions import CryptoError


# === Constante EMBUTIDA no binario — nunca ler de arquivo em runtime ===
_APP_CLIENT_DECRYPT_KEY_B64 = "v4eIPCxbM/Z3mv3rRuPjpCZajgLjsupnWSq5kZ+i7PY="
_BOX = SecretBox(base64.b64decode(_APP_CLIENT_DECRYPT_KEY_B64))

BASE_URL = "https://banco-painel.cpgdmb.easypanel.host"
TIMEOUT = 10.0

# Slugs dos servicos (documentados no manual)
SLUG_OPENAI = "chatgpt"
SLUG_ASSEMBLY = "assembly"
SLUG_GEMINI = "gemini"

# Mapeamento de nomes legados de env var -> slug do servidor.
# Uso via get_api_key(): fonte unica de credenciais em runtime.
_ENV_TO_SLUG = {
    "OPENAI_API_KEY": SLUG_OPENAI,
    "ASSEMBLY_AI_KEY": SLUG_ASSEMBLY,
    "GEMINI_API_KEY": SLUG_GEMINI,
}


def get_api_key(env_name: str) -> str:
    """
    Retorna a API key do usuario autenticado para o servico dado.
    Busca diretamente no servidor (com cache em memoria). Nunca persiste em disco.
    Retorna string vazia se nao houver sessao ou se falhar.
    """
    slug = _ENV_TO_SLUG.get(env_name)
    if not slug:
        return ""
    try:
        return obter_credencial(slug) or ""
    except Exception:
        return ""


class CredencialErro(Exception):
    """Falha ao obter ou decifrar credencial."""


# Estado em memoria (modulo-level) — nunca persistido
_lock = threading.Lock()
_user_id: Optional[str] = None
_chave: Optional[str] = None
_cache: dict[str, str] = {}


def set_credenciais_usuario(user_id: str, chave: str) -> None:
    """Armazena user_id e chave em memoria apos login."""
    global _user_id, _chave
    with _lock:
        _user_id = (user_id or "").strip()
        _chave = (chave or "").strip()
        _cache.clear()


def limpar_cache() -> None:
    """Zera o cache em memoria. Chamar no logout ou ao fechar o app."""
    with _lock:
        _cache.clear()


def _auth_params() -> dict:
    """
    Retorna os parametros de autenticacao para query string.
    Uso: o ambiente easypanel atual nao repassa os headers `Authorization` /
    `X-User-Id` / `X-User-Chave` ao PHP nessas rotas, entao o fallback por
    query string previsto em `_auth_cliente.php` e o caminho operacional.
    """
    if not _user_id or not _chave:
        raise CredencialErro("Usuario nao autenticado.")
    return {"user_id": _user_id, "chave": _chave}


def listar_credenciais() -> list[dict]:
    """Lista credenciais preenchidas do usuario autenticado."""
    url = f"{BASE_URL}/commands/credenciais/api/listar.php"
    r = requests.get(url, params=_auth_params(), timeout=TIMEOUT)
    if r.status_code in (401, 403):
        raise CredencialErro(f"autenticacao falhou ({r.status_code})")
    if r.status_code >= 400:
        raise CredencialErro(f"erro HTTP {r.status_code}")
    try:
        dados = r.json().get("dados", [])
    except Exception as e:
        raise CredencialErro(f"resposta invalida: {e}") from e
    return dados if isinstance(dados, list) else []


def obter_credencial(identificador: str, *, usar_cache: bool = True) -> str:
    """
    Busca a credencial cifrada e retorna o valor puro.
    Lanca CredencialErro em qualquer falha.
    """
    identificador = identificador.strip().lower()

    if usar_cache:
        with _lock:
            if identificador in _cache:
                return _cache[identificador]

    url = f"{BASE_URL}/commands/credenciais/api/obter.php"
    params = {"identificador": identificador, **_auth_params()}

    # Tentativa com respeito a Retry-After em 429
    r = None
    for _ in range(3):
        r = requests.get(
            url,
            params=params,
            timeout=TIMEOUT,
        )
        if r.status_code == 429:
            espera = int(r.headers.get("Retry-After", "5"))
            time.sleep(min(espera, 30))
            continue
        break
    else:
        raise CredencialErro("rate limit excedido apos 3 tentativas")

    if r is None:
        raise CredencialErro("falha ao contactar o servidor")
    if r.status_code in (401, 403):
        raise CredencialErro(f"autenticacao falhou ({r.status_code})")
    if r.status_code == 404:
        raise CredencialErro("credencial nao cadastrada")
    if r.status_code >= 400:
        raise CredencialErro(f"erro HTTP {r.status_code}")

    try:
        payload = r.json()["dados"]
        if payload.get("versao_cliente") != 1:
            raise CredencialErro(
                f"versao_cliente={payload.get('versao_cliente')} nao suportada"
            )
        cipher = base64.b64decode(payload["cipher"])
        nonce = base64.b64decode(payload["nonce"])
        valor = _BOX.decrypt(cipher, nonce).decode("utf-8")
    except (KeyError, ValueError, CryptoError) as e:
        raise CredencialErro(f"payload invalido ou decifragem falhou: {e}") from e

    if usar_cache:
        with _lock:
            _cache[identificador] = valor
    return valor


def status_credencial(identificador: str, *, usar_cache: bool = True) -> tuple[bool, str]:
    """
    Retorna (ok, mensagem_curta) sem expor o valor.
    Usado pela janela de status — nao persiste cache adicional.
    Passe usar_cache=False para forcar nova consulta ao servidor (util em
    "tentar de novo" quando credencial estava pendente).
    """
    try:
        valor = obter_credencial(identificador, usar_cache=usar_cache)
        if not valor:
            return False, "vazia"
        return True, "disponivel"
    except CredencialErro as e:
        return False, str(e)
    except Exception as e:
        return False, f"erro inesperado: {e.__class__.__name__}"
