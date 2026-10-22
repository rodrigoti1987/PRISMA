/* ============================================================
   03_benchmark_permanencia.sql
   Objetivo: popular TB_PERM_ESPERADA com benchmark calculado a
             partir do histórico real de internações encerradas.
   Pré-requisito: 01_permanencia_ajustada.sql já executado.
   Oracle 12c+  |  Executar como owner do schema assistencial.
   ============================================================ */

/* ------------------------------------------------------------
   1) Função de faixa etária — fonte única de verdade.
   A view materializada deve usar esta mesma função, senão o
   benchmark e o cálculo do desvio divergem.
   ------------------------------------------------------------ */
CREATE OR REPLACE FUNCTION FN_FAIXA_ETARIA (p_idade IN NUMBER)
RETURN VARCHAR2 DETERMINISTIC IS
BEGIN
  IF p_idade IS NULL   THEN RETURN '60-74'; END IF;  -- faixa modal
  IF p_idade <= 14     THEN RETURN '0-14';
  ELSIF p_idade <= 29  THEN RETURN '15-29';
  ELSIF p_idade <= 44  THEN RETURN '30-44';
  ELSIF p_idade <= 59  THEN RETURN '45-59';
  ELSIF p_idade <= 74  THEN RETURN '60-74';
  ELSE                      RETURN '75+';
  END IF;
END;
/

/* ------------------------------------------------------------
   2) Colunas de rastreabilidade do benchmark
   AMOSTRA: quantas internações sustentam aquele valor.
   METODO:  qual nível hierárquico gerou o número.
   Sem isso o benchmark é um número sem procedência e ninguém
   consegue auditá-lo depois.
   ------------------------------------------------------------ */
DECLARE
  v_existe NUMBER;
BEGIN
  FOR c IN (
    SELECT 'AMOSTRA' AS col, 'NUMBER(8)' AS tip FROM dual UNION ALL
    SELECT 'METODO',          'VARCHAR2(40)'    FROM dual
  ) LOOP
    SELECT COUNT(*) INTO v_existe
      FROM user_tab_cols
     WHERE table_name = 'TB_PERM_ESPERADA'
       AND column_name = c.col;

    IF v_existe = 0 THEN
      EXECUTE IMMEDIATE 'ALTER TABLE TB_PERM_ESPERADA ADD (' || c.col || ' ' || c.tip || ')';
    END IF;
  END LOOP;
END;
/

/* ------------------------------------------------------------
   3) Tabela de estágio — permite inspecionar o resultado antes
   de aplicar. O benchmark não vai direto para produção.
   ------------------------------------------------------------ */
CREATE TABLE TB_PERM_BENCHMARK_STAGE (
  CID_PRINCIPAL  VARCHAR2(10) NOT NULL,
  FAIXA_ETARIA   VARCHAR2(10) NOT NULL,
  DIAS_ESPERADOS NUMBER(6,2)  NOT NULL,
  AMOSTRA        NUMBER(8),
  METODO         VARCHAR2(40)
);


/* ------------------------------------------------------------
   4) Procedure de cálculo — hierarquia de 4 níveis
   Nível 1: CID exato + faixa etária        (N >= 15)
   Nível 2: CID exato, valor único p/ todas as faixas (N >= 30)
   Nível 3: '*' + faixa etária              (N >= 40)
   Nível 4: '*' + '*' com padrão conservador (garante que nunca
            exista internação sem benchmark)
   ------------------------------------------------------------ */
CREATE OR REPLACE PROCEDURE PR_CALCULAR_BENCHMARK_PERMANENCIA (
  p_min_cid_faixa IN NUMBER DEFAULT 15,
  p_min_cid       IN NUMBER DEFAULT 30,
  p_min_faixa     IN NUMBER DEFAULT 40,
  p_meses_hist    IN NUMBER DEFAULT 24
) IS
  c_faixas CONSTANT VARCHAR2(200) := q'['0-14','15-29','30-44','45-59','60-74','75+']';
BEGIN
  DELETE FROM TB_PERM_BENCHMARK_STAGE;

  /* ==========================================================
     BASE: internações encerradas, com CID e duração válida.
     Corte do decil superior por grupo elimina a cauda longa
     (casos sociais, transferências prolongadas, longa permanência
     crônica) que distorceria o benchmark para cima.

     Idade apurada na data de alta — mesma referência usada pela
     view materializada, o que mantém benchmark e desvio coerentes.
     ========================================================== */
  INSERT INTO TB_PERM_BENCHMARK_STAGE
    (CID_PRINCIPAL, FAIXA_ETARIA, DIAS_ESPERADOS, AMOSTRA, METODO)
  WITH base AS (
    SELECT /*+ MATERIALIZE */
           i.cid_principal,
           FN_FAIXA_ETARIA(
             TRUNC(MONTHS_BETWEEN(TRUNC(i.data_alta), p.data_nascimento) / 12)
           ) AS faixa_etaria,
           TRUNC(i.data_alta) - TRUNC(i.data_internacao) AS dias
      FROM INTERNACAO i
      JOIN PACIENTE  p ON p.id_paciente = i.id_paciente
     WHERE i.data_alta IS NOT NULL
       AND i.data_alta >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -p_meses_hist)
       AND i.cid_principal IS NOT NULL
       AND TRUNC(i.data_alta) > TRUNC(i.data_internacao)   -- descarta 0 dia e datas invertidas
       AND TRUNC(i.data_alta) - TRUNC(i.data_internacao) <= 180
  ),
  com_corte AS (
    SELECT b.*,
           PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY b.dias)
             OVER (PARTITION BY b.cid_principal, b.faixa_etaria) AS p90
      FROM base b
  ),
  trimado AS (
    SELECT * FROM com_corte WHERE dias <= p90
  )
  SELECT cid_principal,
         faixa_etaria,
         ROUND(MEDIAN(dias), 2),
         COUNT(*),
         'CID+FAIXA'
    FROM trimado
   GROUP BY cid_principal, faixa_etaria
  HAVING COUNT(*) >= p_min_cid_faixa;

  /* ---------- Nível 2: CID com valor único para todas as faixas ----------
     Aplicado só onde o par CID+faixa não atingiu amostra mínima.
     Evita deixar o paciente sem benchmark apenas por ter idade rara
     naquele diagnóstico. */
  INSERT INTO TB_PERM_BENCHMARK_STAGE
    (CID_PRINCIPAL, FAIXA_ETARIA, DIAS_ESPERADOS, AMOSTRA, METODO)
  WITH base AS (
    SELECT /*+ MATERIALIZE */
           i.cid_principal,
           TRUNC(i.data_alta) - TRUNC(i.data_internacao) AS dias
      FROM INTERNACAO i
     WHERE i.data_alta IS NOT NULL
       AND i.data_alta >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -p_meses_hist)
       AND i.cid_principal IS NOT NULL
       AND TRUNC(i.data_alta) > TRUNC(i.data_internacao)
       AND TRUNC(i.data_alta) - TRUNC(i.data_internacao) <= 180
  ),
  com_corte AS (
    SELECT b.*,
           PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY b.dias)
             OVER (PARTITION BY b.cid_principal) AS p90
      FROM base b
  ),
  por_cid AS (
    SELECT cid_principal, ROUND(MEDIAN(dias), 2) AS mediana, COUNT(*) AS n
      FROM com_corte
     WHERE dias <= p90
     GROUP BY cid_principal
    HAVING COUNT(*) >= p_min_cid
  ),
  faixas AS (
    SELECT regexp_substr(c_faixas, '[^,]+', 1, LEVEL) AS faixa
      FROM dual
   CONNECT BY regexp_substr(c_faixas, '[^,]+', 1, LEVEL) IS NOT NULL
  )
  SELECT c.cid_principal,
         REPLACE(f.faixa, '''', ''),
         c.mediana,
         c.n,
         'CID'
    FROM por_cid c
   CROSS JOIN faixas f
   WHERE NOT EXISTS (
     SELECT 1 FROM TB_PERM_BENCHMARK_STAGE s
      WHERE s.cid_principal = c.cid_principal
        AND s.faixa_etaria  = REPLACE(f.faixa, '''', '')
   );

  /* ---------- Nível 3: fallback global por faixa etária ---------- */
  INSERT INTO TB_PERM_BENCHMARK_STAGE
    (CID_PRINCIPAL, FAIXA_ETARIA, DIAS_ESPERADOS, AMOSTRA, METODO)
  WITH base AS (
    SELECT /*+ MATERIALIZE */
           FN_FAIXA_ETARIA(
             TRUNC(MONTHS_BETWEEN(TRUNC(i.data_alta), p.data_nascimento) / 12)
           ) AS faixa_etaria,
           TRUNC(i.data_alta) - TRUNC(i.data_internacao) AS dias
      FROM INTERNACAO i
      JOIN PACIENTE  p ON p.id_paciente = i.id_paciente
     WHERE i.data_alta IS NOT NULL
       AND i.data_alta >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -p_meses_hist)
       AND TRUNC(i.data_alta) > TRUNC(i.data_internacao)
       AND TRUNC(i.data_alta) - TRUNC(i.data_internacao) <= 180
  ),
  com_corte AS (
    SELECT b.*,
           PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY b.dias)
             OVER (PARTITION BY b.faixa_etaria) AS p90
      FROM base b
  ),
  por_faixa AS (
    SELECT faixa_etaria, ROUND(MEDIAN(dias), 2) AS mediana, COUNT(*) AS n
      FROM com_corte
     WHERE dias <= p90
     GROUP BY faixa_etaria
    HAVING COUNT(*) >= p_min_faixa
  )
  SELECT '*', faixa_etaria, mediana, n, 'FAIXA'
    FROM por_faixa;

  /* ---------- Nível 4: padrão conservador para faixas sem histórico ----------
     Só entra se o nível 3 não cobriu a faixa. Valor alto de propósito:
     na dúvida, é melhor não sinalizar desvio do que acusar
     permanentemente um paciente sem referência. */
  INSERT INTO TB_PERM_BENCHMARK_STAGE
    (CID_PRINCIPAL, FAIXA_ETARIA, DIAS_ESPERADOS, AMOSTRA, METODO)
  SELECT '*', f.faixa, f.dias_padrao, NULL, 'PADRAO'
    FROM (
      SELECT '''0-14'''  AS faixa, 3.5 AS dias_padrao FROM dual UNION ALL
      SELECT '''15-29''', 4.5 FROM dual UNION ALL
      SELECT '''30-44''', 5.0 FROM dual UNION ALL
      SELECT '''45-59''', 5.5 FROM dual UNION ALL
      SELECT '''60-74''', 6.5 FROM dual UNION ALL
      SELECT '''75+''',   7.5 FROM dual
    ) f
   WHERE NOT EXISTS (
     SELECT 1 FROM TB_PERM_BENCHMARK_STAGE s
      WHERE s.cid_principal = '*'
        AND s.faixa_etaria  = REPLACE(f.faixa, '''', '')
   );

  COMMIT;
END;
/


/* ------------------------------------------------------------
   5) Aplicar o estágio em TB_PERM_ESPERADA
   MERGE preserva linhas de benchmark específico que o analista
   tenha ajustado manualmente (METODO = 'MANUAL').
   ------------------------------------------------------------ */

/* Execute o cálculo primeiro */
BEGIN
  PR_CALCULAR_BENCHMARK_PERMANENCIA(
    p_min_cid_faixa => 15,
    p_min_cid       => 30,
    p_min_faixa     => 40,
    p_meses_hist    => 24
  );
END;
/

/* Conferir o resultado antes de aplicar */
SELECT cid_principal, faixa_etaria, dias_esperados, amostra, metodo
  FROM TB_PERM_BENCHMARK_STAGE
 ORDER BY metodo, cid_principal, faixa_etaria;

/* Aplicar */
MERGE INTO TB_PERM_ESPERADA t
USING (
  SELECT s.cid_principal, s.faixa_etaria, s.dias_esperados, s.amostra, s.metodo
    FROM TB_PERM_BENCHMARK_STAGE s
) o
ON (t.cid_principal = o.cid_principal AND t.faixa_etaria = o.faixa_etaria)
WHEN MATCHED THEN UPDATE SET
     t.dias_esperados = CASE WHEN t.metodo = 'MANUAL' THEN t.dias_esperados
                             ELSE o.dias_esperados END,
     t.amostra        = o.amostra,
     t.metodo         = CASE WHEN t.metodo = 'MANUAL' THEN t.metodo
                             ELSE o.metodo END,
     t.origem         = 'BENCHMARK_HISTORICO',
     t.atualizado_em  = SYSDATE
WHEN NOT MATCHED THEN INSERT
     (cid_principal, faixa_etaria, dias_esperados, amostra, metodo,
      origem, atualizado_em)
VALUES
     (o.cid_principal, o.faixa_etaria, o.dias_esperados, o.amostra, o.metodo,
      'BENCHMARK_HISTORICO', SYSDATE);

COMMIT;


/* ------------------------------------------------------------
   6) Validação — o cálculo só é confiável se estes testes passarem
   ------------------------------------------------------------ */

-- 6.1 Nenhuma internação pode ficar sem benchmark
SELECT COUNT(*) AS sem_benchmark
  FROM (
    SELECT DISTINCT cid_principal,
           FN_FAIXA_ETARIA(
             TRUNC(MONTHS_BETWEEN(TRUNC(NVL(data_alta, SYSDATE)), p.data_nascimento) / 12)
           ) AS faixa_etaria
      FROM INTERNACAO i
      JOIN PACIENTE  p ON p.id_paciente = i.id_paciente
     WHERE i.data_alta IS NULL
        OR i.data_alta >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -6)
  ) d
 WHERE NOT EXISTS (
   SELECT 1 FROM TB_PERM_ESPERADA e
    WHERE e.cid_principal = d.cid_principal
      AND e.faixa_etaria  = d.faixa_etaria
 );

-- 6.2 Distribuição do benchmark por faixa: valores devem crescer com a idade
SELECT faixa_etaria,
       COUNT(*)                  AS grupos,
       ROUND(AVG(dias_esperados), 2) AS media_dias,
       ROUND(MIN(dias_esperados), 2) AS minimo,
       ROUND(MAX(dias_esperados), 2) AS maximo
  FROM TB_PERM_ESPERADA
 WHERE cid_principal = '*'
 GROUP BY faixa_etaria
 ORDER BY faixa_etaria;

-- 6.3 Grupos com amostra baixa — revisar manualmente ou desconsiderar
SELECT cid_principal, faixa_etaria, dias_esperados, amostra, metodo
  FROM TB_PERM_ESPERADA
 WHERE NVL(amostra, 0) < 15
   AND metodo <> 'PADRAO'
 ORDER BY amostra;


/* ------------------------------------------------------------
   7) Recalcular a view materializada com o novo benchmark.
   Sem este passo o painel continua mostrando o desvio antigo.
   ------------------------------------------------------------ */
BEGIN
  DBMS_MVIEW.REFRESH('MV_PERMANENCIA_INTERNACAO', 'C');
END;
/


/* ------------------------------------------------------------
   8) Agendamento mensal do recálculo
   Benchmark é referência móvel: recalcular todo mês evita que o
   indicador envelheça e passe a apontar desvios falsos.
   ------------------------------------------------------------ */
BEGIN
  DBMS_SCHEDULER.CREATE_JOB(
    job_name        => 'JOB_BENCHMARK_PERMANENCIA',
    job_type        => 'PLSQL_BLOCK',
    job_action      => 'BEGIN
                          PR_CALCULAR_BENCHMARK_PERMANENCIA(15, 30, 40, 24);
                          MERGE INTO TB_PERM_ESPERADA t
                          USING (SELECT cid_principal, faixa_etaria, dias_esperados,
                                        amostra, metodo
                                   FROM TB_PERM_BENCHMARK_STAGE) o
                             ON (t.cid_principal = o.cid_principal
                             AND t.faixa_etaria  = o.faixa_etaria)
                          WHEN MATCHED THEN UPDATE SET
                               t.dias_esperados = CASE WHEN t.metodo = ''MANUAL''
                                                       THEN t.dias_esperados
                                                       ELSE o.dias_esperados END,
                               t.amostra = o.amostra,
                               t.atualizado_em = SYSDATE
                          WHEN NOT MATCHED THEN INSERT
                               (cid_principal, faixa_etaria, dias_esperados,
                                amostra, metodo, origem, atualizado_em)
                          VALUES
                               (o.cid_principal, o.faixa_etaria, o.dias_esperados,
                                o.amostra, o.metodo, ''BENCHMARK_HISTORICO'', SYSDATE);
                          COMMIT;
                          DBMS_MVIEW.REFRESH(''MV_PERMANENCIA_INTERNACAO'', ''C'');
                        END;',
    start_date      => TRUNC(SYSDATE, 'MM') + INTERVAL '1' DAY,
    repeat_interval => 'FREQ=MONTHLY; BYMONTHDAY=2; BYHOUR=3; BYMINUTE=0',
    enabled         => TRUE,
    comments        => 'Recalcula o benchmark de permanência e atualiza a MV.'
  );
END;
/