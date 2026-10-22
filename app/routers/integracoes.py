"""Endpoints do módulo Integrações e Sistemas Hospitalares.

Todos os dados vêm de views e tabelas já agregadas. A aplicação não faz
cálculo pesado em memória: o Oracle entrega o número pronto.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from app import queries
from app.cache import Chaves, cache
from app.db import BancoIndisponivel, executar, fetch_all, fetch_one
from app.queries import (
    FILTROS_INTEGRACOES,
    LIMITE_PAGINA,
    ORDENACAO_INTEGRACOES,
    PADRAO_ORDENACAO_INTEGRACOES,
    TAMANHO_PAGINA_PADRAO,
    montar_filtros,
    montar_paginacao,
)
from app.security import requer_token

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/integracoes",
    tags=["integracoes"],
    dependencies=[Depends(requer_token)],
)


# Projeção fixa da tabela de eventos. Não contém entrada de usuário —
# os filtros entram depois, sempre por bind variable.
PROJECAO_EVENTOS = """
    SELECT
        TO_CHAR(e.horario, 'DD/MM/YYYY HH24:MI:SS') AS horario,
        e.sistema_origem,
        e.sistema_destino,
        e.tipo_mensagem,
        e.id_mensagem,
        e.paciente,
        e.unidade,
        e.status,
        e.latencia_ms,
        e.detalhe
    FROM EVENTO_INTEGRACAO e
"""


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _erro_banco(exc: BancoIndisponivel) -> HTTPException:
    log.error("Consulta sem pool disponível: %s", exc)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Base de dados indisponível. Verifique o pool Oracle.",
    )


# ------------------------------------------------------------------ Sintético
@router.get("/sintetico", summary="Visão consolidada do módulo Integrações")
def sintetico():
    """KPIs, status por sistema e séries de 12 horas.

    Cacheado por CACHE_TTL_SECONDS. Vários operadores abrindo o painel ao
    mesmo tempo compartilham o mesmo resultado em vez de gerar N consultas
    agregadas no Oracle.
    """

    def produzir() -> dict:
        return {
            "geradoEm": _agora(),
            "kpis": fetch_all(queries.SQL_INT_KPIS),
            "sistemas": fetch_all(queries.SQL_INT_SISTEMAS),
            "series": {
                "porHora": fetch_all(queries.SQL_INT_SERIE_HORA),
                "pacsPorModalidade": fetch_all(queries.SQL_INT_PACS_MODALIDADE),
            },
        }

    try:
        return cache.obter_ou_calcular(Chaves.int_sintetico(), produzir)
    except BancoIndisponivel as exc:
        raise _erro_banco(exc) from exc
    except Exception:
        # Falha inesperada no produtor: cache devolve o valor anterior se
        # existir. Se chegou até aqui, não havia valor anterior.
        log.exception("Falha ao montar o sintético de integrações")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao consultar os indicadores de integração.",
        )


# ----------------------------------------------------------------- Analítico
@router.get("/analitico", summary="Eventos de integração linha a linha")
def analitico(
    page: int = Query(1, ge=1, description="Página, começando em 1"),
    size: int = Query(
        TAMANHO_PAGINA_PADRAO,
        ge=1,
        le=LIMITE_PAGINA,
        description=f"Máximo de {LIMITE_PAGINA} registros por página",
    ),
    sistema: str | None = Query(None, description="MV Soul, PACS/RIS, Oracle, HL7/FHIR"),
    status_filtro: str | None = Query(None, alias="status", description="SUCESSO, ERRO, PENDENTE"),
    busca: str | None = Query(None, description="ID da mensagem ou paciente"),
    sort: str = Query("horario", description="horario, latencia, status, sistema"),
    dir: str = Query("desc", description="asc ou desc"),
):
    """Tabela detalhada, paginada e filtrada.

    O teto de LIMITE_PAGINA protege os dois lados: o Oracle não recebe
    consulta gigante e o navegador não trava renderizando milhares linhas.
    """
    where, binds = montar_filtros(
        {"sistema": sistema, "status": status_filtro, "busca": busca},
        FILTROS_INTEGRACOES,
        where_base=["1 = 1"],
        binds_base={},
    )

    ordem = ORDENACAO_INTEGRACOES.get(sort, PADRAO_ORDENACAO_INTEGRACOES)
    corpo = f"{PROJECAO_EVENTOS} WHERE {where}"
    sql_pagina, tamanho = montar_paginacao(corpo, ordem, dir, page, size)

    try:
        total_linha = fetch_one(
            f"SELECT COUNT(*) AS total FROM EVENTO_INTEGRACAO e WHERE {where}", binds
        )
        registros = fetch_all(sql_pagina, binds)
    except BancoIndisponivel as exc:
        raise _erro_banco(exc) from exc

    return {
        "geradoEm": _agora(),
        "page": page,
        "size": tamanho,
        "total": (total_linha or {}).get("total", 0),
        "registros": registros,
    }


# --------------------------------------------------------------- Payload
@router.get("/eventos/{id_mensagem}", summary="Payload completo de uma mensagem")
def detalhe_evento(
    id_mensagem: str = Path(..., min_length=1, max_length=64),
):
    """Payload HL7/FHIR/DICOM de uma mensagem específica.

    Usado pelo modal do modo analítico. O payload vai como veio do
    barramento — a API não tenta interpretá-lo.
    """
    try:
        linha = fetch_one(queries.SQL_INT_EVENTO_DETALHE, {"id_mensagem": id_mensagem})
    except BancoIndisponivel as exc:
        raise _erro_banco(exc) from exc

    if linha is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Mensagem não encontrada.",
        )

    return linha


# ----------------------------------------------------------------- Alertas
@router.get("/alertas", summary="Alertas de integração não reconhecidos")
def alertas():
    try:
        return {"geradoEm": _agora(), "alertas": fetch_all(queries.SQL_INT_ALERTAS)}
    except BancoIndisponivel as exc:
        raise _erro_banco(exc) from exc


@router.post(
    "/alertas/{id_alerta}/reconhecer",
    summary="Reconhece um alerta e o remove da lista",
)
def reconhecer_alerta(
    request: Request,
    identidade: str = Depends(requer_token),
    id_alerta: int = Path(..., ge=1),
):
    """Marca o alerta como reconhecido.

    Grava QUEM reconheceu (impressão do token, não o segredo) na coluna
    reconhecido_por. Isso atende à trilha de auditoria exigida para
    operação com dado assistencial.
    """
    try:
        executar(
            queries.SQL_INT_ALERTA_RECONHECER,
            {"id": id_alerta, "por": identidade},
        )
    except BancoIndisponivel as exc:
        raise _erro_banco(exc) from exc

    # Se o UPDATE não encontrou linha, o alerta já foi reconhecido ou
    # não existe. Confirmar ao operador evita duplo clique silencioso.
    cache.invalidar(Chaves.INT_SINTETICO)

    log.info(
        "AUDITORIA alerta_integracao_reconhecido id=%s token=%s cliente=%s",
        id_alerta,
        identidade,
        request.client.host if request.client else "desconhecido",
    )

    return {"id": id_alerta, "reconhecido": True, "por": identidade}