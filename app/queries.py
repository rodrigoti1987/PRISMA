# ==============================================================
# DIAGNÓSTICO ORACLE
# ==============================================================
# Pré-requisitos de privilégio (executar como DBA, uma única vez):
#   GRANT SELECT ON v_$session      TO app_painel;
#   GRANT SELECT ON v_$parameter    TO app_painel;
#   GRANT SELECT ON dba_data_files  TO app_painel;
#   GRANT SELECT ON dba_free_space  TO app_painel;
# Sem esses grants o endpoint responde com "disponivel": false e o
# motivo — não derruba o painel, apenas oculta o sexto KPI.

SQL_DIAG_SESSOES = """
    SELECT COUNT(*)                                                   AS limite,
           SUM(CASE WHEN s.type = 'USER' THEN 1 ELSE 0 END)           AS total_usuario,
           SUM(CASE WHEN s.type = 'USER'
                     AND s.status = 'ACTIVE' THEN 1 ELSE 0 END)       AS ativas
    FROM v$session s
    CROSS JOIN (
        SELECT TO_NUMBER(p.value) AS limite
          FROM v$parameter p
         WHERE p.name = 'sessions'
    ) l
"""

SQL_DIAG_TABLESPACE = """
    SELECT df.tablespace_name AS nome,
           ROUND(SUM(df.bytes) / 1073741824, 2) AS alocado_gb,
           ROUND(SUM(CASE WHEN df.autoextensible = 'YES'
                          THEN df.maxbytes ELSE df.bytes END) / 1073741824, 2) AS teto_gb,
           ROUND(NVL(MAX(fs.livre), 0) / 1073741824, 2) AS livre_gb,
           ROUND(100 * (SUM(df.bytes) - NVL(MAX(fs.livre), 0))
                     / NULLIF(SUM(CASE WHEN df.autoextensible = 'YES'
                                       THEN df.maxbytes ELSE df.bytes END), 0), 1) AS pct_usado
      FROM dba_data_files df
      LEFT JOIN (
            SELECT file_id, SUM(bytes) AS livre
              FROM dba_free_space
             GROUP BY file_id
      ) fs ON fs.file_id = df.file_id
     GROUP BY df.tablespace_name
     ORDER BY pct_usado DESC
"""

SQL_DIAG_EVENTOS = """
    SELECT * FROM (
        SELECT s.wait_class                AS classe,
               s.event                     AS evento,
               COUNT(*)                    AS sessoes,
               ROUND(AVG(s.seconds_in_wait)) AS espera_media_s
          FROM v$session s
         WHERE s.type = 'USER'
           AND s.status = 'ACTIVE'
           AND s.wait_class <> 'Idle'
         GROUP BY s.wait_class, s.event
         ORDER BY COUNT(*) DESC
    ) WHERE ROWNUM <= 5
"""