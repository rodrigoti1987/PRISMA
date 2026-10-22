"""Autenticação por Bearer token e trilha de auditoria de acesso.

Postura adotada: falha fechada. Se API_TOKEN não estiver configurado, a
API nega as rotas protegidas em vez de liberá-las. Uma aplicação que
expõe prontuário e CID nunca deve subir em modo aberto por descuido de
configuração.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Header, HTTPException, Request, status

from app.config import settings

log = logging.getLogger(__name__)

# Comprimento mínimo aceitável para um token de serviço.
TAMANHO_MINIMO_TOKEN = 32


def token_configurado() -> bool:
    """True quando há token com tamanho razoável configurado."""
    return bool(settings.api_token) and len(settings.api_token) >= TAMANHO_MINIMO_TOKEN


def impressao_do_token(token: str) -> str:
    """Identificador curto e irreversível do token.

    Serve para correlacionar chamadas no log de auditoria sem gravar o
    token em texto puro — nunca registre o segredo.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _extrair_token(authorization: str) -> str:
    if not authorization:
        return ""

    partes = authorization.split(" ", 1)
    if len(partes) != 2 or partes[0].lower() != "bearer":
        return ""

    return partes[1].strip()


def verificar_credencial(authorization: str) -> str:
    """Valida o header e devolve a impressão do token. Levanta 401 se falhar."""
    enviado = _extrair_token(authorization)

    if not enviado:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token ausente. Envie o header Authorization: Bearer <token>.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # compare_digest evita vazamento por diferença de tempo de resposta.
    # A comparação em bytes também dispensa checagem manual de tamanho.
    if not hmac.compare_digest(enviado.encode("utf-8"), settings.api_token.encode("utf-8")):
        log.warning("Tentativa de acesso com token inválido: %s", impressao_do_token(enviado))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return impressao_do_token(enviado)


async def requer_token(authorization: str = Header(default="")) -> str:
    """Dependência das rotas protegidas.

    Uso no router:
        dependencies=[Depends(requer_token)]

    O valor devolvido é a impressão do token, para correlacionar no log.
    """
    if not token_configurado():
        log.error(
            "API_TOKEN ausente ou menor que %s caracteres. Acesso bloqueado.",
            TAMANHO_MINIMO_TOKEN,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "API não configurada. Defina API_TOKEN com no mínimo "
                f"{TAMANHO_MINIMO_TOKEN} caracteres."
            ),
        )

    return verificar_credencial(authorization)


# ------------------------------------------------------------------ Auditoria
# Rotas cuja consulta expõe dado assistencial. Toda chamada é registrada.
ROTAS_AUDITADAS = (
    "/api/pacientes/",
)


def registrar_acesso(
    request: Request,
    identidade: str,
    filtros: dict | None = None,
) -> None:
    """Registra a consulta na trilha de auditoria.

    Regra: registra QUEM acessou, QUAL rota e QUAIS filtros usou. Nunca
    registra conteúdo de paciente — prontuário, iniciais ou CID não vão
    para o log. Isso atende ao princípio de minimização da LGPD e permite
    rastrear acesso indevido depois.

    Em produção, troque o log por insert em tabela de auditoria no Oracle
    para ter retenção e consulta próprias.
    """
    if not request.url.path.startswith(ROTAS_AUDITADAS):
        return

    # Atrás de proxy reverso, o IP real vem no X-Forwarded-For.
    encaminhado = request.headers.get("x-forwarded-for")
    cliente = (
        encaminhado.split(",")[0].strip()
        if encaminhado
        else (request.client.host if request.client else "desconhecido")
    )

    log.info(
        "AUDITORIA rota=%s metodo=%s token=%s cliente=%s filtros=%s",
        request.url.path,
        request.method,
        identidade,
        cliente,
        filtros or {},
    )