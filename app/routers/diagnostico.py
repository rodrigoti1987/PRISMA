"""Diagnóstico operacional do Oracle.

Motivo de existir: o sexto KPI do painel (sessões ativas) não pode vir
de view materializada — V$SESSION é dinâmica. Este router consulta as
views de performance sob demanda.

Postura adotada: tolerância a falta de privilégio. Se um GRANT não foi
concedido, aquele bloco volta vazio com o motivo, e o painel exibe
"indisponível" em vez de quebrar a tela inteira.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app import queries
from app.cache import TTLCache
from app.db import fetch_all, fetch_one
from app.security import requer_token

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/diagnostico",
    tags=["diagnostico"],
    dependencies=[Depends(requer_token)],
)

# TTL curto: diagnóstico é dado vivo. 15 s evita martelar o banco se
# vários operadores abrirem o painel ao mesmo tempo, sem ficar obsoleto.
_cache = TTLCache(ttl_seconds=15)

# Códigos que significam "falta permissão", não "erro de aplicação".
PREFIXOS_SEM_PRIVILEGIO = ("ORA-00942", "ORA-01031")


def _tentar(sql: str):
    """Executa e devolve (linhas, aviso). Nunca levanta exceção."""
    try:
        return fetch_all(sql), None
    except Exception as exc:
        texto = str(exc)
        log.warning("Consulta de diagnóstico falhou: %s", texto)

        if any(p in texto for p in PREFIXOS_SEM_PRIVILEGIO):
            # Extrai o nome do objeto para orientar a correção no log.
            alvo = re.search(r"ORA-009\d\d:\s*([\w$#\.]+)", texto)
            return [], (
                "Privilégio insuficiente"
                + (f" em {alvo.group(1)}" if alvo else "")
                + ". Conceda SELECT ao usuário da aplicação."
            )

        return [], "Falha na consulta de diagnóstico."


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/oracle", summary="Sessões, tablespace e eventos de espera")
def diagnostico_oracle():
    """Alimenta o sexto KPI e o bloco de saúde do banco.

    Devolve também um objeto 'kpi' já no formato consumido pelo painel,
    para o front-end não precisar montar o cartão manualmente.
    """
    em_cache = _cache.get("oracle")
    if em_cache:
        return em_cache

    avisos: list[str] = []

    sessoes, aviso = _tentar(queries.SQL_DIAG_SESSOES)
    if aviso:
        avisos.append(f"sessoes: {aviso}")

    tablespace, aviso = _tentar(queries.SQL_DIAG_TABLESPACE)
    if aviso:
        avisos.append(f"tablespace: {aviso}")

    eventos, aviso = _tentar(queries.SQL_DIAG_EVENTOS)
    if aviso:
        avisos.append(f"eventos: {aviso}")

    linha_ses = sessoes[0] if sessoes else {}
    limite = int(linha_ses.get("limite") or 0)
    ativas = int(linha_ses.get("ativas") or 0)
    usuario = int(linha_ses.get("total_usuario") or 0)
    pct = round(100 * usuario / limite, 1) if limite else None

    # Tablespace: destaca o mais crítico para o cartão resumido.
    ts_critico = tablespace[0] if tablespace else {}

    payload = {
        "geradoEm": _agora(),
        "disponivel": bool(sessoes) or bool(tablespace),
        "sessoes": {
            "ativas": ativas,
            "totalUsuario": usuario,
            "limite": limite,
            "pctUso": pct,
        },
        "tablespace": [
            {
                "nome": t.get("nome"),
                "alocadoGb": t.get("alocado_gb"),
                "tetoGb": t.get("teto_gb"),
                "livreGb": t.get("livre_gb"),
                "pctUsado": t.get("pct_usado"),
            }
            for t in tablespace
        ],
        "eventosEspera": [
            {
                "classe": e.get("classe"),
                "evento": e.get("evento"),
                "sessoes": e.get("sessoes"),
                "esperaMediaS": e.get("espera_media_s"),
            }
            for e in eventos
        ],
        # Cartão pronto para o painel — mesmo contrato dos demais KPIs.
        "kpi": {
            "chave": "ses",
            "rotulo": "Sessões ativas Oracle",
            "valor": str(usuario),
            "unidade": f"/{limite}" if limite else "",
            "variacao": 0,
            "meta": str(limite) if limite else "",
            "historico": None,
            "detalhe": (
                f"{ativas} ativas · "
                f"tablespace {ts_critico.get('nome', '—')} "
                f"{ts_critico.get('pct_usado', 0)}%"
                if tablespace else f"{ativas} ativas"
            ),
        },
        "avisos": avisos,
    }

    _cache.set("oracle", payload)
    return payload