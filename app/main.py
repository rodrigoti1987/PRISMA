"""app/main.py — composição da aplicação.

Único ponto que amarra: logging, pool Oracle, routers dos dois módulos,
painel estático e tratamento centralizado de erro.

Rodar:
    uvicorn app.main:app --host 0.0.0.0 --port $PORT

Decisões de arquitetura embutidas aqui:
- API e painel na MESMA origem: sem CORS, sem token no navegador.
- Falha fechada: rotas protegidas exigem API_TOKEN configurado.
- A aplicação sobe mesmo sem Oracle, para permitir diagnóstico por
  /api/health em vez de loop de restart.
- Swagger/OpenAPI desligados: não expor schema de API com dado sensível.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, db
from app.cache import cache
from app.config import settings
from app.db import BancoIndisponivel
from app.routers import integracoes, pacientes
from app.security import token_configurado

# ------------------------------------------------------------------ Logging
# Configurado antes de qualquer import que emita log de módulo.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("prisma")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"


# --------------------------------------------------------------- Ciclo de vida
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Sobe o pool antes de aceitar tráfego e o encerra no shutdown."""
    db.abrir_pool()

    # Configuração incompleta não derruba a aplicação, mas precisa gritar
    # no log: em produção isso é falha de deploy, não bug de código.
    if not settings.oracle_configurado:
        log.warning(
            "Oracle NÃO configurado. Defina ORACLE_USER, ORACLE_PASSWORD e "
            "ORACLE_DSN. /api/health vai responder 503 até isso ser corrigido."
        )
    if not token_configurado():
        log.error(
            "API_TOKEN ausente ou curto demais. As rotas de dados vão "
            "responder 503 até que um token válido seja configurado."
        )

    log.info(
        "Aplicação pronta | versão=%s | painel=%s | cache_ttl=%ss",
        __version__,
        "instalado" if INDEX_HTML.exists() else "AUSENTE (./static/index.html)",
        settings.cache_ttl_seconds,
    )

    try:
        yield
    finally:
        db.fechar_pool()


# ------------------------------------------------------------------- Aplicação
app = FastAPI(
    title="bussola prisma — Ciência de Dados em Saúde",
    description="API intermediária entre o painel de operação e o Oracle.",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,        # Swagger desligado
    redoc_url=None,
    openapi_url=None,     # nem o schema JSON é servido
)


# ------------------------------------------------------ Cabeçalhos de segurança
@app.middleware("http")
async def cabecalhos_de_seguranca(request: Request, call_next):
    """Cabeçalhos aplicados a toda resposta, inclusive estáticos e erros."""
    resposta = await call_next(request)

    resposta.headers.setdefault("X-Content-Type-Options", "nosniff")
    resposta.headers.setdefault("X-Frame-Options", "DENY")
    resposta.headers.setdefault("Referrer-Policy", "no-referrer")
    resposta.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")

    # Painel exibe prontuário, CID e iniciais. Não pode ficar em cache de
    # navegador compartilhado em posto de enfermagem.
    resposta.headers.setdefault("Cache-Control", "no-store")
    return resposta


# ------------------------------------------------- Tratamento centralizado de erro
@app.exception_handler(BancoIndisponivel)
async def tratar_banco_indisponivel(request: Request, exc: BancoIndisponivel):
    """Pool ausente ou fechado → 503 com mensagem operacional, não stack."""
    log.error("Banco indisponível em %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "erro": "Base de dados indisponível.",
            "detalhe": "Verifique o pool Oracle em /api/health.",
        },
    )


@app.exception_handler(RequestValidationError)
async def tratar_validacao(request: Request, exc: RequestValidationError):
    """Parâmetro inválido → 422 com os campos que falharam, sem ecoar valores.

    Valores não são devolvidos de propósito: em filtro de paciente, o termo
    de busca pode ser um prontuário ou trecho de nome.
    """
    campos = [
        {"campo": ".".join(str(p) for p in erro.get("loc", [])[1:]), "motivo": erro.get("msg")}
        for erro in exc.errors()
    ]
    log.info("Requisição inválida em %s: %s", request.url.path, campos)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"erro": "Parâmetros inválidos.", "campos": campos},
    )


@app.exception_handler(Exception)
async def tratar_erro_interno(request: Request, exc: Exception):
    """Última barreira. Loga stack completo, devolve mensagem genérica.

    O detalhe da exceção nunca vai para o cliente: mensagem do Oracle pode
    conter nome de tabela, coluna e até fragmento de SQL.
    """
    log.exception("Erro interno em %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "erro": "Falha ao processar a requisição.",
            "tipo": type(exc).__name__,
        },
    )


# ---------------------------------------------------------------- Routers
# Ordem importa: integracoes e pacientes já declaram o prefixo /api/<modulo>
# e a dependência de token no próprio APIRouter.
app.include_router(integracoes.router)
app.include_router(pacientes.router)


# ------------------------------------------------------- Endpoints públicos
@app.get("/api/health", tags=["diagnostico"], summary="Diagnóstico primário")
def health():
    """A aplicação está viva? O Oracle responde?

    Primeiro teste a rodar depois do deploy. Se retornar oracle=false com
    detalhe 'OperationalError', o problema é rede/firewall/VPN — não código.

    Público de propósito: precisa responder mesmo sem token, senão não serve
    para diagnosticar falha de configuração.
    """
    ok, detalhe = db.ping()
    corpo = {
        "status": "ok" if ok else "degradado",
        "oracle": ok,
        "versao": __version__,
        "horario": datetime.now(timezone.utc).isoformat(),
        "dsn": settings.oracle_dsn or None,
        "token_configurado": token_configurado(),
        "pool_pronto": db.pool_pronto(),
    }
    if detalhe:
        corpo["detalhe"] = detalhe

    return JSONResponse(corpo, status_code=200 if ok else 503)


@app.get("/api/versao", tags=["diagnostico"], summary="Identificação do produto")
def versao():
    return {
        "produto": "bussola prisma",
        "descritor": "Ciência de Dados em Saúde",
        "versao": __version__,
        "modulos": ["integracoes", "pacientes"],
    }


@app.get("/api/cache", tags=["diagnostico"], summary="Estatísticas do cache")
def estatisticas_cache():
    """Observabilidade do cache: taxa de acerto e itens em memória.

    Cache com taxa de acerto baixa significa TTL curto demais para o padrão
    de uso real — ajuste CACHE_TTL_SECONDS com base nesse número.
    """
    return cache.estatisticas()


@app.delete("/api/cache", tags=["diagnostico"], summary="Limpa o cache")
def limpar_cache():
    """Força recarga no próximo acesso. Útil após carga de dados ou patch."""
    cache.limpar()
    return {"cache": "limpo"}


# ------------------------------------------------------------------ Front-end
# Montado por último: mounts são avaliados depois das rotas registradas, e
# /static não deve capturar nada da API.
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    log.warning("Pasta ./static criada vazia. O painel não será servido até "
                "que index.html seja colocado nela.")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def painel():
    """Serve o painel na raiz. Mesma origem da API: sem CORS, sem token no JS."""
    if not INDEX_HTML.exists():
        return PlainTextResponse(
            "Painel não instalado. Coloque o arquivo em ./static/index.html\n"
            "API de diagnóstico: /api/health\n",
            status_code=status.HTTP_200_OK,
        )
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Evita 404 ruidoso no log: navegador pede favicon em toda sessão."""
    arquivo = STATIC_DIR / "favicon.ico"
    if arquivo.exists():
        return FileResponse(arquivo)
    return PlainTextResponse("", status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------- Execução direta (dev only)
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=settings.log_level.lower(),
    )