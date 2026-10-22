# ============================================================
# bussola prisma — imagem da aplicação (API + painel)
# Executa como usuário não-root e respeita a variável PORT do host.
# ============================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependências primeiro: aproveita o cache de camada do Docker.
# O modo Thin do python-oracledb dispensa Oracle Instant Client,
# por isso não há instalação de biblioteca nativa aqui.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Usuário sem privilégio: se a aplicação for comprometida,
# o atacante não herda root dentro do container.
RUN useradd --create-home --uid 10001 prisma \
    && chown -R prisma:prisma /app
USER prisma

EXPOSE 8000

# Health check por TCP, não por /api/health.
# Motivo: /api/health responde 503 quando o Oracle está inacessível —
# isso é diagnóstico, não processo morto. Marcar o container como
# unhealthy por causa do banco faria o orquestrador reiniciar em loop
# uma aplicação que está funcionando e apenas sem banco.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import os,socket,sys; s=socket.create_connection(('127.0.0.1', int(os.getenv('PORT','8000'))), 4); s.close()"

# --workers 1 é proposital: o cache é em memória por processo.
# Com mais workers cada um teria cache próprio e as consultas pesadas
# se multiplicariam. Ao escalar, migre o cache para Redis.
# --proxy-headers faz o log de auditoria registrar o IP real do cliente
# quando houver proxy reverso na frente.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*' --no-access-log"]