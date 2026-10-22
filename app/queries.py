"""Todo o SQL da aplicação.

Duas invariantes deste módulo:

1. Nenhuma entrada de usuário é concatenada em SQL. Filtros e ordenação
   vêm de allowlists fixas definidas abaixo. Valor que não está na lista
   é ignorado e o padrão assumido.
2. Todo valor variável entra como bind variable (:nome). Isso evita SQL
   injection e permite ao Oracle reaproveitar planos de execução.
"""

from __future__ import annotations

# Teto de linhas por página. Protege os dois lados: o Oracle não recebe
# consulta gigante e o navegador não trava renderizando milhares de linhas.
LIMITE_PAGINA = 200
TAMANHO_PAGINA_PADRAO = 50


# ==============================================================
# NÍVEL DE RISCO — expressão derivada
# ==============================================================
# A view materializada do script SQL calcula SCORE_RISCO (0-100), mas não
# grava a faixa NIVEL_RISCO. Em vez de recriar a MV, o nível é derivado
# aqui por uma constante fixa — nunca por entrada de usuário.
#
# Trade-off honesto: filtrar por expressão derivada não usa índice. Com
# poucas centenas de internações em curso isso é irrelevante. Se a base
# crescer, materialize a coluna na MV e troque esta constante por
# m.nivel_risco.
NIVEL_RISCO_SQL = """
    CASE
        WHEN m.score_risco >= 70 THEN 'ALTO'
        WHEN m.score_risco >= 40 THEN 'MEDIO'
        ELSE 'BAIXO'
    END
"""


# ==============================================================
# MÓDULO INTEGRAÇÕES
# ==============================================================

SQL_INT_KPIS = """
    SELECT chave, rotulo, valor, unidade, variacao, meta, historico
    FROM MV_INTEGRACAO_KPI
    ORDER BY ordem
"""

SQL_INT_SISTEMAS = """
    SELECT nome, status, descricao, metrica_1, metrica_2, metrica_3
    FROM MV_INTEGRACAO_SISTEMA
    ORDER BY ordem
"""

SQL_INT_SERIE_HORA = """
    SELECT TO_CHAR(hora, 'HH24:MI') AS hora,
           hl7_processadas          AS hl7,
           pacs_estudos             AS pacs,
           latencia_media_ms        AS latencia_ms
    FROM MV_INTEGRACAO_SERIE_HORA
    WHERE hora >= SYSDATE - 0.5
    ORDER BY hora
"""

SQL_INT_PACS_MODALIDADE = """
    SELECT modalidade, COUNT(*) AS qtd
    FROM ESTUDO_PACS
    WHERE data_estudo >= TRUNC(SYSDATE)
    GROUP BY modalidade
    ORDER BY qtd DESC
"""

SQL_INT_ALERTAS = """
    SELECT id,
           severidade,
           titulo,
           sistema,
           TO_CHAR(horario, 'YYYY-MM-DD HH24:MI:SS') AS horario
    FROM ALERTA_INTEGRACAO
    WHERE reconhecido = 'N'
    ORDER BY CASE severidade
                        WHEN 'CRITICO' THEN 1
                        WHEN 'ATENCAO' THEN 2
                        ELSE 3
                    END,
                    horario DESC
"""

SQL_INT_EVENTO_DETALHE = """
    SELECT id_mensagem, tipo_mensagem, sistema_origem, sistema_destino,
           payload, detalhe
    FROM EVENTO_INTEGRACAO
    WHERE id_mensagem = :id_mensagem
"""

SQL_INT_ALERTA_RECONHECER = """
    UPDATE ALERTA_INTEGRACAO
       SET reconhecido = 'S',
           reconhecido_em = SYSDATE,
           reconhecido_por = :por
     WHERE id = :id
       AND reconhecido = 'N'
"""


# ==============================================================
# MÓDULO PACIENTES
# ==============================================================

SQL_PAC_KPIS = """
    SELECT
        COUNT(*)                                                   AS total_internados,
        ROUND(AVG(m.dias_permanencia), 1)                          AS permanencia_media,
        ROUND(AVG(m.dias_esperados), 1)                            AS permanencia_esperada,
        SUM(CASE WHEN m.desvio > 2 THEN 1 ELSE 0 END)              AS acima_esperado,
        ROUND(100 * SUM(CASE WHEN m.desvio > 2 THEN 1 ELSE 0 END)
                    / NULLIF(COUNT(*), 0), 1)                      AS pct_acima_esperado,
        SUM(CASE WHEN m.score_risco >= 70 THEN 1 ELSE 0 END)       AS risco_alto,
        SUM(CASE WHEN m.em_curso = 'S' THEN 1 ELSE 0 END)          AS em_curso,
        ROUND(AVG(m.score_risco), 0)                               AS score_medio,
        ROUND(SUM(GREATEST(m.desvio, 0)), 0)                       AS leitos_dia_excedentes
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.data_internacao >= TO_DATE(:de, 'YYYY-MM-DD')
"""

SQL_PAC_FAIXAS_DESVIO = """
    SELECT m.classificacao_desvio                                AS faixa,
           COUNT(*)                                              AS qtd,
           ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1)      AS pct
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.data_internacao >= TO_DATE(:de, 'YYYY-MM-DD')
    GROUP BY m.classificacao_desvio
    ORDER BY MIN(m.desvio)
"""

SQL_PAC_SERIE_MENSAL = """
    SELECT TO_CHAR(TRUNC(m.data_internacao, 'MM'), 'YYYY-MM')    AS mes,
           ROUND(AVG(m.dias_permanencia), 1)                     AS observado,
           ROUND(AVG(m.dias_esperados), 1)                       AS esperado
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.data_internacao >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -8)
    GROUP BY TRUNC(m.data_internacao, 'MM')
    ORDER BY TRUNC(m.data_internacao, 'MM')
"""

SQL_PAC_DESTAQUES = """
    SELECT * FROM (
        SELECT m.iniciais,
               m.unidade,
               m.desvio,
               m.cid_principal,
               m.dias_permanencia,
               m.dias_esperados
        FROM MV_PERMANENCIA_INTERNACAO m
        WHERE m.em_curso = 'S'
        ORDER BY m.desvio DESC
    ) WHERE ROWNUM <= 3
"""

SQL_PAC_CAPITULOS = """
    SELECT m.capitulo_cid                                        AS capitulo,
           COUNT(*)                                              AS qtd,
           ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1)      AS pct,
           ROUND(AVG(m.dias_permanencia), 1)                     AS permanencia_media
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.data_internacao >= TO_DATE(:de, 'YYYY-MM-DD')
    GROUP BY m.capitulo_cid
    ORDER BY qtd DESC
"""

SQL_PAC_FAIXAS_ETARIAS = """
    SELECT m.faixa_etaria AS faixa, COUNT(*) AS qtd
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.data_internacao >= TO_DATE(:de, 'YYYY-MM-DD')
    GROUP BY m.faixa_etaria
    ORDER BY m.faixa_etaria
"""

SQL_PAC_SEXO = """
    SELECT m.sexo AS sexo,
           COUNT(*) AS qtd,
           ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.data_internacao >= TO_DATE(:de, 'YYYY-MM-DD')
    GROUP BY m.sexo
"""

SQL_PAC_UNIDADES = """
    SELECT m.unidade AS unidade, COUNT(*) AS qtd
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.data_internacao >= TO_DATE(:de, 'YYYY-MM-DD')
    GROUP BY m.unidade
    ORDER BY qtd DESC
"""

SQL_PAC_TOP_CAUSAS = """
    SELECT * FROM (
        SELECT m.cid_principal AS cid,
               MAX(m.capitulo_cid) AS capitulo,
               COUNT(*) AS qtd,
               ROUND(AVG(m.dias_permanencia), 1) AS permanencia_media
        FROM MV_PERMANENCIA_INTERNACAO m
        WHERE m.data_internacao >= TO_DATE(:de, 'YYYY-MM-DD')
        GROUP BY m.cid_principal
        ORDER BY COUNT(*) DESC
    ) WHERE ROWNUM <= 5
"""

SQL_PAC_DISTRIBUICAO_RISCO = f"""
    SELECT {NIVEL_RISCO_SQL} AS nivel, COUNT(*) AS qtd
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.data_internacao >= TO_DATE(:de, 'YYYY-MM-DD')
    GROUP BY {NIVEL_RISCO_SQL}
"""

SQL_PAC_TOP_RISCO = f"""
    SELECT * FROM (
        SELECT m.prontuario,
               m.iniciais,
               m.idade,
               m.unidade,
               m.dias_permanencia,
               m.desvio,
               m.cid_principal,
               m.score_risco,
               {NIVEL_RISCO_SQL} AS nivel_risco,
               m.fatores_risco
        FROM MV_PERMANENCIA_INTERNACAO m
        WHERE m.em_curso = 'S'
        ORDER BY m.score_risco DESC
    ) WHERE ROWNUM <= 8
"""

SQL_PAC_FATORES = """
    SELECT 'Risco de queda'       AS fator,
           SUM(CASE WHEN m.morse  >= 45 THEN 1 ELSE 0 END) AS qtd
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.em_curso = 'S'
    UNION ALL
    SELECT 'Lesão por pressão',
           SUM(CASE WHEN m.braden <= 16 THEN 1 ELSE 0 END)
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.em_curso = 'S'
    UNION ALL
    SELECT 'NEWS2 alterado',
           SUM(CASE WHEN m.news2 >= 5 THEN 1 ELSE 0 END)
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.em_curso = 'S'
    UNION ALL
    SELECT 'Múltiplas comorbidades',
           SUM(CASE WHEN m.qtd_comorbidades >= 3 THEN 1 ELSE 0 END)
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.em_curso = 'S'
"""

SQL_PAC_ALERTAS = f"""
    SELECT m.id_internacao AS id,
           CASE
               WHEN m.desvio > 20 OR m.score_risco >= 70 THEN 'CRITICO'
               WHEN m.desvio > 10 OR m.score_risco >= 40 THEN 'ATENCAO'
               ELSE 'INFO'
           END AS severidade,
           m.iniciais     AS paciente,
           m.unidade      AS unidade,
           m.dias_permanencia,
           m.desvio,
           m.fatores_risco AS fatores
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.em_curso = 'S'
      AND (m.desvio > 10 OR m.score_risco >= 40)
    ORDER BY m.desvio DESC
"""

# Detalhe para o modal do painel.
# A tabela EVENTO_CLINICO é a linha do tempo — adapte o nome ao seu modelo.
SQL_PAC_DETALHE = f"""
    SELECT m.prontuario,
           m.iniciais,
           m.idade,
           m.sexo,
           m.unidade,
           TO_CHAR(m.data_internacao, 'DD/MM/YYYY')      AS data_internacao,
           TO_CHAR(m.data_alta, 'DD/MM/YYYY')            AS data_alta,
           m.em_curso,
           m.dias_permanencia,
           m.dias_esperados,
           m.desvio,
           m.classificacao_desvio,
           m.cid_principal,
           m.capitulo_cid,
           m.qtd_comorbidades,
           m.score_risco,
           {NIVEL_RISCO_SQL} AS nivel_risco,
           m.news2,
           m.braden,
           m.morse,
           m.fatores_risco
    FROM MV_PERMANENCIA_INTERNACAO m
    WHERE m.id_internacao = :id_internacao
"""

SQL_PAC_LINHA_DO_TEMPO = """
    SELECT TO_CHAR(data_evento, 'DD/MM/YYYY HH24:MI') AS data_evento,
           tipo_evento,
           descricao
    FROM EVENTO_CLINICO
    WHERE id_internacao = :id_internacao
    ORDER BY data_evento
"""

SQL_PAC_COMORBIDADES = """
    SELECT c.codigo AS cid, c.descricao
    FROM INTERNACAO_COMORBIDADE ic
    JOIN CID c ON c.codigo = ic.cid
    WHERE ic.id_internacao = :id_internacao
    ORDER BY c.codigo
"""


# ==============================================================
# ALLOWLIST DE FILTROS
# ==============================================================
# Cada entrada é (fragmento SQL fixo, nomes de bind que ele usa).
# Fragmento é constante de código. O valor sempre vira bind.

FILTROS_PACIENTES = {
    "unidade":  ("m.unidade = :unidade", ["unidade"]),
    "capitulo": ("m.capitulo_cid = :capitulo", ["capitulo"]),
    "faixa":    ("m.faixa_etaria = :faixa", ["faixa"]),
    "risco":    (f"({NIVEL_RISCO_SQL}) = :risco", ["risco"]),
    "situacao": (
        "((:situacao = 'ACIMA'  AND m.desvio > 2) "
        " OR (:situacao = 'DENTRO' AND m.desvio <= 2))",
        ["situacao"],
    ),
    "busca": (
        "(UPPER(m.iniciais) LIKE :busca "
        " OR m.prontuario LIKE :busca "
        " OR UPPER(m.cid_principal) LIKE :busca)",
        ["busca"],
    ),
}

FILTROS_INTEGRACOES = {
    "sistema": ("e.sistema_origem = :sistema", ["sistema"]),
    "status":  ("e.status = :status", ["status"]),
    "busca":   (
        "(UPPER(e.id_mensagem) LIKE :busca OR UPPER(e.paciente) LIKE :busca)",
        ["busca"],
    ),
}

# Colunas ordenáveis. Qualquer valor fora do mapa cai no padrão definido
# pelo router — nunca vai para o SQL.
ORDENACAO_PACIENTES = {
    "prontuario":  "m.prontuario",
    "idade":       "m.idade",
    "permanencia": "m.dias_permanencia",
    "desvio":      "m.desvio",
    "score":       "m.score_risco",
    "internacao":  "m.data_internacao",
}

ORDENACAO_INTEGRACOES = {
    "horario":  "e.horario",
    "latencia": "e.latencia_ms",
    "status":   "e.status",
    "sistema":  "e.sistema_origem",
}

PADRAO_ORDENACAO_PACIENTES = "m.desvio"
PADRAO_ORDENACAO_INTEGRACOES = "e.horario"

# Valores aceitos em filtros de lista fechada.
SITUACOES_PERMANENCIA = {"ACIMA", "DENTRO"}
NIVEIS_RISCO = {"ALTO", "MEDIO", "BAIXO"}


# ==============================================================
# CONSTRUÇÃO DE WHERE E PAGINAÇÃO
# ==============================================================

def montar_filtros(
    filtros: dict,
    catalogo: dict,
    where_base: list[str],
    binds_base: dict,
) -> tuple[str, dict]:
    """Monta a cláusula WHERE a partir da allowlist.

    Filtro não reconhecido é ignorado. Filtro vazio, None ou 'TODOS'/
    'TODAS' é ignorado. Isso mantém a consulta válida mesmo com query
    string adulterada pelo cliente.
    """
    where = list(where_base)
    binds = dict(binds_base)

    for chave, (fragmento, nomes_bind) in catalogo.items():
        valor = filtros.get(chave)
        if valor in (None, "", "TODOS", "TODAS"):
            continue

        where.append(fragmento)
        for nome in nomes_bind:
            binds[nome] = (
                f"%{str(valor).upper()}%"
                if nome == "busca"
                else valor
            )

    return " AND ".join(where), binds


def montar_paginacao(
    corpo_sql: str,
    ordem: str,
    direcao: str,
    page: int,
    size: int,
) -> tuple[str, int]:
    """Acrescenta ORDER BY e OFFSET/FETCH com valores já validados.

    Oracle 12.1 não aceita bind variables em OFFSET/FETCH. Por isso os
    números são convertidos para int aqui — int() falha em qualquer coisa
    que não seja número, o que garante que nada além de dígito chega ao
    SQL.
    """
    pagina = max(1, int(page))
    tamanho = min(LIMITE_PAGINA, max(1, int(size)))
    deslocamento = (pagina - 1) * tamanho

    sentido = "ASC" if str(direcao).lower() == "asc" else "DESC"

    sql = (
        f"{corpo_sql}\n"
        f"ORDER BY {ordem} {sentido}\n"
        f"OFFSET {deslocamento} ROWS FETCH NEXT {tamanho} ROWS ONLY"
    )
    return sql, tamanho