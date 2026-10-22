"""Configuração central. Lê variáveis de ambiente ou o arquivo .env.

Ordem de precedência: variável de ambiente > .env > valor padrão aqui.
Em produção (botkeep, container, VM) use variáveis de ambiente — o .env
é para desenvolvimento local.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------- Oracle
    oracle_user: str = ""
    oracle_password: str = ""
    oracle_dsn: str = ""          # HOST:PORTA/SERVICO
    oracle_pool_min: int = 2
    oracle_pool_max: int = 10
    oracle_call_timeout_ms: int = 15000

    # --------------------------------------------------------- Segurança
    api_token: str = ""

    # ------------------------------------------------------- Comportamento
    cache_ttl_seconds: int = 30
    log_level: str = "INFO"

    # ----------------------------------------------------------- Derivados
    @property
    def oracle_configurado(self) -> bool:
        """True quando há credenciais completas para tentar conectar.

        A aplicação sobe mesmo sem isso — o objetivo é permitir
        diagnosticar firewall pelo /api/health em vez de ver o processo
        morrer em loop de restart.
        """
        return bool(self.oracle_user and self.oracle_password and self.oracle_dsn)


settings = Settings()