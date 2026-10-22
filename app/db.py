"""Pool de conexões Oracle e helpers de consulta.

Regra invariável deste módulo: o SQL recebido aqui já vem pronto, com
apenas fragmentos fixos definidos no código (ver app/queries.py). Os
valores sempre entram como bind variables. Nenhuma entrada de usuário é
concatenada em SQL.
"""

from __future__ import annotations

import logging
import threading

import oracledb

from app.config import settings

log = logging.getLogger(__name__)

# CLOB/BLOB chegam como str/bytes em vez de objeto LOB — serializa em JSON
# sem camada de conversão.
oracledb.defaults.fetch_lobs = False

# Timeout de rede por chamada: impede que um Oracle lento prenda o worker
# até o navegador do operador desistir primeiro.
oracledb.defaults.call_timeout = settings.oracle_call_timeout_ms

_pool: oracledb.ConnectionPool | None = None
_lock = threading.Lock()


# ---------------------------------------------------------------- Ciclo de vida
def abrir_pool() -> None:
    """Cria o pool uma única vez, no startup da aplicação."""
    global _pool

    with _lock:
        if _pool is not None:
            log.debug("Pool já inicializado, ignorando nova abertura.")
            return

        if not settings.oracle_configurado:
            log.warning(
                "Credenciais Oracle ausentes. A API sobe degradada — "
                "configure ORACLE_USER, ORACLE_PASSWORD e ORACLE_DSN."
            )
            return

        try:
            _pool = oracledb.create_pool(
                user=settings.oracle_user,
                password=settings.oracle_password,
                dsn=settings.oracle_dsn,
                min=settings.oracle_pool_min,
                max=settings.oracle_pool_max,
                increment=1,
                getmode=oracledb.POOL_GETMODE_WAIT,  # espera conexão livre
                timeout=60,                          # devolve ociosa ao pool
                stmtcachesize=40,                    # reaproveita planos
            )
            log.info(
                "Pool Oracle criado: %s (min=%s, max=%s)",
                settings.oracle_dsn,
                settings.oracle_pool_min,
                settings.oracle_pool_max,
            )
        except Exception:
            log.exception(
                "Falha ao criar o pool Oracle. Verifique firewall, VPN e DSN. "
                "A aplicação continua no ar para diagnóstico via /api/health."
            )


def fechar_pool() -> None:
    """Encerra o pool no shutdown da aplicação."""
    global _pool

    with _lock:
        if _pool is None:
            return
        try:
            _pool.close(force=True)
            log.info("Pool Oracle encerrado.")
        except Exception:
            log.exception("Falha ao encerrar o pool Oracle.")
        finally:
            _pool = None


def pool_pronto() -> bool:
    return _pool is not None


# ------------------------------------------------------------------- Consultas
class BancoIndisponivel(RuntimeError):
    """Levantado quando não há pool — vira HTTP 503 na camada de rotas."""


def _curso():
    if _pool is None:
        raise BancoIndisponivel("Pool Oracle não inicializado.")
    return _pool.acquire()


def fetch_all(sql: str, binds: dict | None = None) -> list[dict]:
    """Executa um SELECT e devolve lista de dicionários com chaves minúsculas."""
    with _curso() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, binds or {})
            colunas = [d[0].lower() for d in cur.description]
            return [dict(zip(colunas, linha)) for linha in cur.fetchall()]


def fetch_one(sql: str, binds: dict | None = None) -> dict | None:
    """Executa um SELECT e devolve a primeira linha, ou None."""
    with _curso() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, binds or {})
            colunas = [d[0].lower() for d in cur.description]
            linha = cur.fetchone()
            return dict(zip(colunas, linha)) if linha else None


def executar(sql: str, binds: dict | None = None) -> None:
    """Para DML — reconhecer alerta, registrar auditoria. Faz commit explícito."""
    with _curso() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, binds or {})
        conn.commit()


# ------------------------------------------------------------------ Diagnóstico
def ping() -> tuple[bool, str | None]:
    """Testa a conexão. Devolve (ok, detalhe_do_erro).

    Usado pelo /api/health. Se retornar False com ORA-12541 ou timeout,
    o problema é rede/firewall — não código.
    """
    if _pool is None:
        return False, "pool_nao_inicializado"

    try:
        linha = fetch_one("SELECT 1 AS ok FROM DUAL")
        return (linha is not None), None
    except Exception as exc:
        log.warning("Ping ao Oracle falhou: %s", exc)
        return False, type(exc).__name__