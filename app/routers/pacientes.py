"""Endpoints do módulo Análise de Pacientes.

Eixos: permanência ajustada por CID + faixa etária, perfil epidemiológico
e risco clínico.

Privacidade: nomes trafegam apenas como iniciais, já mascarados na view
materializada. A API nunca consulta o nome completo do paciente.

ORDEM DAS ROTAS IMPORTA: rotas literais (/sintetico, /analitico, /alertas)
devem ser declaradas ANTES do path param /{id_internacao}. Caso contrário
o FastAPI tenta converter "alertas" em int e responde 422.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from app import queries
from app.cache import Chaves, cache
from app.db import BancoIndisponivel, fetch_all, fetch_one
from app.queries import (
    FILTROS_PACIENTES,
    LIMITE_PAGINA,
    ORDENACAO_PACIENTES,
    PADRAO_ORDENACAO_PACIENTES,
    TAMANHO_PAGINA_PADRAO,
    montar_filtros,
    montar_paginacao,
)
from app.security import registrar_acesso, requer_token

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/pacientes",
    tags=["pacientes"],
    dependencies=[Depends(requer_token)],
)


PROJECAO_PACIENTES = """
    SELECT
        m.prontuario,
        m.iniciais,
        m.idade,
        m.sexo,
        m.unidade,
        TO_CHAR(m.data_internacao, 'YYYY-MM-DD') AS data_internacao,
        m.dias_permanencia,
        m.dias_esperados,
        m.desvio,
        m.classificacao_desvio,
        m.cid_principal,
        m.capitulo_cid,
        m.qtd_comorbidades,
        m.score_risco,
        m.fatores_risco,
        m.em_curso
    FROM MV_PERMANENCIA_INTERNACAO m
"""


def _agora() -> str:
    """Timestamp em UTC. A conversão para America/Sao_Paulo é na interface."""
    return datetime.now(timezone.utc).isoformat()


def _erro_banco(exc: BancoIndisponivel) -> HTTPException:
    log.error("Consulta sem pool disponível: %s", exc)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Base de dados indisponível. Verifique o pool Oracle.",
    )


def _periodo(dias: int) -> str:
    return (date.today() - timedelta(days=dias)).isoformat()


# ==========================================================
# 1) SINTÉTICO
# ==========================================================
@router.get("/sintetico", summary="Visão consolidada do módulo Pacientes")
def sintetico(dias: int = Query(30, ge=1, le=365, description="Janela em dias")):
    """KPIs, permanência, epidemiologia e risco em uma única chamada."""
    de = _periodo(dias)

    def produzir() -> dict:
        binds = {"de": de}
        return {
            "geradoEm": _agora(),
            "periodo": {"de": de, "dias": dias},
            "kpis": fetch_one(queries.SQL_PAC_KPIS, binds) or {},
            "permanencia": {
                "faixasDesvio": fetch_all(queries.SQL_PAC_FAIXAS_DESVIO, binds),
                "serieMensal": fetch_all(queries.SQL_PAC_SERIE_MENSAL, binds),
                "destaques": fetch_all(queries.SQL_PAC_DESTAQUES),
            },
            "epidemiologia": {
                "porCapituloCid": fetch_all(queries.SQL_PAC_CAPITULOS, binds),
                "porFaixaEtaria": fetch_all(queries.SQL_PAC_FAIXAS_ETARIAS, binds),
                "porSexo": fetch_all(queries.SQL_PAC_SEXO, binds),
                "porUnidade": fetch_all(queries.SQL_PAC_UNIDADES, binds),
                "topCausas": fetch_all(queries.SQL_PAC_TOP_CAUSAS, binds),
            },
            "risco": {
                "distribuicao": fetch_all(queries.SQL_PAC_DISTRIBUICAO_RISCO, binds),
                "topPacientes": fetch_all(queries.SQL_PAC_TOP_RISCO),
                "fatoresPrevalentes": fetch_all(queries.SQL_PAC_FATORES),
            },
        }

    try:
        return cache.obter_ou_calcular(Chaves.pac_sintetico(dias), produzir)
    except BancoIndisponivel as exc:
        raise _erro_banco(exc) from exc
    except Exception:
        log.exception("Falha ao montar o sintético de pacientes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao consultar os indicadores de pacientes.",
        )


# ==========================================================
# 2) ANALÍTICO
# ==========================================================
@router.get("/analitico", summary="Pacientes linha a linha")
def analitico(
    request: Request,
    identidade: str = Depends(requer_token),
    page: int = Query(1, ge=1),
    size: int = Query(TAMANHO_PAGINA_PADRAO, ge=1, le=LIMITE_PAGINA),
    dias: int = Query(30, ge=1, le=365),
    unidade: str | None = Query(None, description="CER, Mariska, Paulino, Ambulatório"),
    capitulo: str | None = Query(None, description="Capítulo CID-10"),
    faixa: str | None = Query(None, description="0-14, 15-29, 30-44, 45-59, 60-74, 75+"),
    risco: str | None = Query(None, description="ALTO, MEDIO, BAIXO"),
    situacao: str | None = Query(None, description="ACIMA ou DENTRO do esperado"),
    busca: str | None = Query(None, description="Iniciais, prontuário ou CID"),
    sort: str = Query("desvio", description="prontuario, idade, permanencia, desvio, score"),
    dir: str = Query("desc"),
):
    """Tabela detalhada por paciente, com filtros e ordenação por allowlist."""
    registrar_acesso(
        request,
        identidade,
        {
            "unidade": unidade,
            "capitulo": capitulo,
            "faixa": faixa,
            "risco": risco,
            "situacao": situacao,
            "dias": dias,
            "busca": bool(busca),   # registra se houve busca, nunca o termo
        },
    )

    de = _periodo(dias)

    where, binds = montar_filtros(
        {
            "unidade": unidade,
            "capitulo": capitulo,
            "faixa": faixa,
            "risco": risco,
            "situacao": situacao,
            "busca": busca,
        },
        FILTROS_PACIENTES,
        where_base=["m.data_internacao >= TO_DATE(:de, 'YYYY-MM-DD')"],
        binds_base={"de": de},
    )

    ordem = ORDENACAO_PACIENTES.get(sort, PADRAO_ORDENACAO_PACIENTES)
    corpo = f"{PROJECAO_PACIENTES} WHERE {where}"
    sql_pagina, tamanho = montar_paginacao(corpo, ordem, dir, page, size)

    try:
        total_linha = fetch_one(
            f"SELECT COUNT(*) AS total FROM MV_PERMANENCIA_INTERNACAO m WHERE {where}",
            binds,
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


# ==========================================================
# 3) ALERTAS — rota literal, ANTES do path param
# ==========================================================
@router.get("/alertas", summary="Alertas clínicos e de permanência")
def alertas():
    """Alertas ativos, ordenados por desvio de permanência.

    O front-end usa a severidade para decidir qual som disparar:
    CRITICO, ATENCAO ou INFO.
    """
    try:
        return {"geradoEm": _agora(), "alertas": fetch_all(queries.SQL_PAC_ALERTAS)}
    except BancoIndisponivel as exc:
        raise _erro_banco(exc) from exc


# ==========================================================
# 4) DETALHE — path param numérico, declarado por último
# ==========================================================
@router.get(
    "/{id_internacao}",
    summary="Detalhe da internação: escalas, linha do tempo e comorbidades",
)
def detalhe_paciente(
    request: Request,
    identidade: str = Depends(requer_token),
    id_internacao: int = Path(..., ge=1, le=999_999_999),
):
    """Alimenta o modal do modo analítico.

    Devolve a composição do score (NEWS2, Braden, Morse, comorbidades,
    idade) para que a equipe saiba POR QUE o paciente é risco alto — um
    score sem decomposição não é acionável.
    """
    registrar_acesso(request, identidade, {"id_internacao": id_internacao})

    chave = Chaves.pac_detalhe(id_internacao)

    def produzir() -> dict:
        binds = {"id_internacao": id_internacao}
        return {
            "geradoEm": _agora(),
            "paciente": fetch_one(queries.SQL_PAC_DETALHE, binds),
            "linhaDoTempo": fetch_all(queries.SQL_PAC_LINHA_DO_TEMPO, binds),
            "comorbidades": fetch_all(queries.SQL_PAC_COMORBIDADES, binds),
        }

    try:
        payload = cache.obter_ou_calcular(chave, produzir, permitir_obsoleto=False)
    except BancoIndisponivel as exc:
        raise _erro_banco(exc) from exc
    except Exception:
        log.exception("Falha ao montar detalhe da internação %s", id_internacao)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao consultar o detalhe da internação.",
        )

    if not payload.get("paciente"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Internação não encontrada.",
        )

    return payload