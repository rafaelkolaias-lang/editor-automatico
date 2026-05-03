"""
Modulo de autenticacao do Editor Premiere Premium.
Conecta ao mesmo banco MySQL do app de referencia (cronometro-web).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

ARQUIVO_LOGIN_SALVO = Path.home() / ".credenciais_rk.json"

_DB_HOST = "76.13.112.108"
_DB_PORTA = 3306
_DB_NOME = "dados"
_DB_USUARIO = "kolaias"
_DB_SENHA = "kolaias"


class RepositorioAuth:
    """Conexao por thread com o banco MySQL para autenticacao."""

    def __init__(self) -> None:
        self._local = threading.local()

    def _obter_conexao(self):
        import pymysql

        conn = getattr(self._local, "conexao", None)
        if conn is not None:
            try:
                conn.ping(reconnect=True)
                return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conexao = None

        conn = pymysql.connect(
            host=_DB_HOST,
            port=_DB_PORTA,
            database=_DB_NOME,
            user=_DB_USUARIO,
            password=_DB_SENHA,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=8,
        )
        self._local.conexao = conn
        return conn

    def autenticar_usuario(self, user_id: str, chave: str) -> dict | None:
        user_id = (user_id or "").strip().lower()
        chave = (chave or "").strip()
        if not user_id or not chave:
            return None

        conn = self._obter_conexao()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, nome_exibicao FROM usuarios "
                "WHERE user_id = %s AND chave = %s AND status_conta = 'ativa' LIMIT 1",
                [user_id, chave],
            )
            row = cur.fetchone()

        if not row:
            return None

        return {
            "user_id": str(row["user_id"]),
            "nome_exibicao": str(row.get("nome_exibicao") or row["user_id"]),
        }


def ler_login_salvo() -> dict | None:
    try:
        if not ARQUIVO_LOGIN_SALVO.exists():
            return None
        dados = json.loads(ARQUIVO_LOGIN_SALVO.read_text(encoding="utf-8"))
        uid = str(dados.get("user_id") or "").strip()
        chave = str(dados.get("chave") or "").strip()
        if uid and chave:
            return {"user_id": uid, "chave": chave}
    except Exception:
        pass
    return None


def salvar_login(user_id: str, chave: str) -> None:
    try:
        ARQUIVO_LOGIN_SALVO.write_text(
            json.dumps(
                {"user_id": (user_id or "").strip(), "chave": (chave or "").strip()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass
