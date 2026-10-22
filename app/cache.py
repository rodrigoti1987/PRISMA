"""Cache em memória com TTL para as leituras consolidadas.

Por que existe: o modo sintético é aberto por vários operadores ao mesmo
tempo. Sem cache, cada tela vira um conjunto de consultas agregadas no
Oracle. Com TTL de 30 s, N operadores compartilham o mesmo resultado.

Limite conhecido: é cache de processo. Com --workers maior que 1 cada
processo tem o seu, e as consultas se multiplicam. Ao escalar, troque por
Redis mantendo a mesma interface (get / set / obter_ou_calcular).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from app.config import settings

log = logging.getLogger(__name__)


class TTLCache:
    """Cache de dicionário com expiração por tempo e proteção contra
    recarga simultânea da mesma chave (thundering herd).
    """

    def __init__(self, ttl_seconds: int = 30, max_itens: int = 512):
        self._ttl = max(1, int(ttl_seconds))
        self._max = max(16, int(max_itens))

        self._dados: dict[str, tuple[float, Any]] = {}
        self._travas: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

        self._hits = 0
        self._misses = 0
        self._obsoletos = 0

    # ------------------------------------------------------------ internos
    def _trava_da_chave(self, chave: str) -> threading.Lock:
        with self._lock:
            if chave not in self._travas:
                self._travas[chave] = threading.Lock()
            return self._travas[chave]

    def _podar(self) -> None:
        """Expulsa itens expirados; se ainda exceder, remove o mais antigo.

        Chamado dentro de self._lock.
        """
        agora = time.monotonic()
        expirados = [k for k, (t, _) in self._dados.items() if agora - t > self._ttl]
        for chave in expirados:
            self._dados.pop(chave, None)

        if len(self._dados) >= self._max:
            mais_antigo = min(self._dados.items(), key=lambda kv: kv[1][0])[0]
            self._dados.pop(mais_antigo, None)

    # -------------------------------------------------------------- leitura
    def get(self, chave: str) -> Any | None:
        """Valor válido ou None. Contabiliza hits e misses."""
        with self._lock:
            item = self._dados.get(chave)
            if item is None:
                self._misses += 1
                return None

            carimbo, valor = item
            if time.monotonic() - carimbo > self._ttl:
                self._misses += 1
                return None

            self._hits += 1
            return valor

    def get_obsoleto(self, chave: str) -> Any | None:
        """Último valor gravado, mesmo expirado.

        Usado quando a atualização falha: o painel continua mostrando o
        número anterior em vez de esvaziar a tela do operador.
        """
        with self._lock:
            item = self._dados.get(chave)
            return item[1] if item else None

    # ------------------------------------------------------------- escrita
    def set(self, chave: str, valor: Any) -> None:
        with self._lock:
            if chave not in self._dados and len(self._dados) >= self._max:
                self._podar()
            self._dados[chave] = (time.monotonic(), valor)

    # ------------------------------------------------ padrão principal
    def obter_ou_calcular(
        self,
        chave: str,
        produtor: Callable[[], Any],
        permitir_obsoleto: bool = True,
    ) -> Any:
        """Devolve o valor em cache ou executa o produtor e o armazena.

        Dupla checagem com trava por chave: se dez requisições chegarem
        juntas com cache vazio, apenas uma consulta o Oracle.

        Em caso de erro do produtor, devolve o valor anterior se existir
        (permitir_obsoleto=True) e registra aviso. Se não existir, propaga
        a exceção — é melhor erro visível que número inventado.
        """
        valor = self.get(chave)
        if valor is not None:
            return valor

        with self._trava_da_chave(chave):
            valor = self.get(chave)          # outra thread pode ter preenchido
            if valor is not None:
                return valor

            try:
                novo = produtor()
            except Exception:
                if permitir_obsoleto:
                    anterior = self.get_obsoleto(chave)
                    if anterior is not None:
                        with self._lock:
                            self._obsoletos += 1
                        log.warning(
                            "Falha ao atualizar '%s'. Servindo valor anterior "
                            "e mantendo o painel em operação.",
                            chave,
                        )
                        return anterior
                raise

            self.set(chave, novo)
            return novo

    # ---------------------------------------------------------- invalidação
    def invalidar(self, chave: str) -> None:
        with self._lock:
            self._dados.pop(chave, None)

    def invalidar_prefixo(self, prefixo: str) -> int:
        """Remove todas as chaves que começam com o prefixo.

        Útil após reconhecer um alerta: invalida 'pac:sin:*' sem tocar nas
        chaves de integrações.
        """
        with self._lock:
            alvo = [k for k in self._dados if k.startswith(prefixo)]
            for chave in alvo:
                self._dados.pop(chave, None)
            return len(alvo)

    def limpar(self) -> None:
        with self._lock:
            self._dados.clear()
        log.info("Cache limpo manualmente.")

    # ------------------------------------------------------- observabilidade
    def estatisticas(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "itens": len(self._dados),
                "ttl_segundos": self._ttl,
                "max_itens": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "obsoletos_servidos": self._obsoletos,
                "taxa_acerto": round(self._hits / total, 3) if total else None,
            }


# Instância única da aplicação. Um TTL para todas as chaves.
cache = TTLCache(ttl_seconds=settings.cache_ttl_seconds)


# ---------------------------------------------------------------- chaves
# Centralizadas para não haver divergência entre quem grava e quem invalida.
class Chaves:
    INT_SINTETICO = "int:sin"
    PAC_SINTETICO = "pac:sin"

    @staticmethod
    def int_sintetico() -> str:
        return Chaves.INT_SINTETICO

    @staticmethod
    def pac_sintetico(dias: int) -> str:
        return f"{Chaves.PAC_SINTETICO}:{int(dias)}"

    @staticmethod
    def pac_detalhe(id_internacao: int) -> str:
        return f"pac:det:{int(id_internacao)}"