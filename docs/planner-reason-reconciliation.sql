-- 只读复算 phaseb-sweep.md「线上核验二」的固定水位，不读取消息正文。
-- sqlite3 -readonly data/rubenbot.db < docs/planner-reason-reconciliation.sql
-- 2026-11-24 只读核对：88 行 = 7 条 401 + 57 条满额候选 + 3 条操作 + 21 条未知。
-- 截至 3804 的历史段，68 条 fallback 中 61 条唯一匹配且成功，54 条用满 256；
-- 这只是截断线索，不能证明是哪一个 JSON 字段丢失，也不能据此回填 truncated。
-- 先在所有策划行中排除多对一和一对多；不能先过滤空理由，否则会造出假唯一。
WITH plans AS (
    SELECT id, created_at, json_extract(factors,'$.planner.reason') AS model_reason,
           json_extract(factors,'$.expression_decision.source') AS source,
           json_extract(factors,'$.expression_decision.intent') AS intent
    FROM decision_log
    WHERE id BETWEEN 3124 AND 3935 AND json_type(factors,'$.planner')='object'
), candidates AS (
    SELECT p.id, u.id AS usage_id, u.ok, u.completion_tokens,
           count(*) OVER (PARTITION BY p.id) AS decisions_matches,
           count(*) OVER (PARTITION BY u.id) AS usage_matches
    FROM plans p JOIN llm_usage u ON u.task='gen_stage1'
      AND u.ts-u.latency_ms/1000.0-p.created_at BETWEEN -0.05 AND 1
), log_401(id, usage_id) AS (
    -- 报告已从轮转日志核实状态；只靠用量表的 ok=0 不能认定为 401。
    VALUES (3924,3048),(3925,3050),(3926,3051),(3928,3054),
           (3929,3056),(3932,3057),(3935,3062)
)
SELECT p.id, c.usage_id, c.ok, c.completion_tokens,
       CASE WHEN e.id IS NOT NULL AND c.ok=0 THEN 'http_401'
            WHEN p.source='fast' AND p.intent='操作电脑' THEN 'legacy_computer_reason_lost'
            WHEN p.source='fallback' AND c.ok=1 AND c.completion_tokens=256 THEN 'legacy_limit_hit_candidate'
            ELSE 'legacy_unknown' END AS evidence
FROM plans p LEFT JOIN candidates c ON c.id=p.id AND c.decisions_matches=1 AND c.usage_matches=1
LEFT JOIN log_401 e ON (e.id,e.usage_id)=(p.id,c.usage_id)
WHERE p.model_reason='' ORDER BY p.id;
