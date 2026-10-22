"""Routers da aplicação, um por módulo do painel.

- integracoes: MV Soul, PACS/RIS, Oracle e barramento HL7/FHIR
- pacientes:   permanência ajustada, perfil epidemiológico e risco clínico

Cada router declara o prefixo /api/<modulo> e a dependência de token no
próprio APIRouter, de modo que nenhuma rota fica exposta por esquecimento.
"""

from app.routers import integracoes, pacientes

__all__ = ["integracoes", "pacientes"]