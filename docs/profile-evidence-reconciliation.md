# 画像事实悬空：存量只读对照

归纳事务的保护只防新增，不会改旧库。阶段 B 排查快照中，167 次 `memory:*` 引用有 62 次找不到同人的原事实，分属 49 条证据记录、54 个不同引用。62 次都能在同人 `merge` / `drop` 日志的 `before_json` 找到旧 ID。**命中旧 ID 是候选，不等于可以恢复。** 以下流程不在启动、升级或日常归纳时执行。

在一致的 SQLite 备份上用 `sqlite3 -readonly 备份文件` 打开（不能只复制运行中数据库的主文件而漏掉 WAL）。以下查询每个悬空引用按数组位置列出；左连接保留无日志匹配的项，多候选也不去重。不输出事实正文。

```sql
WITH refs AS (
  SELECT e.id AS evidence_id, e.person_id, r.key AS pos, r.value AS ref
  FROM profile_evidence AS e, json_each(e.evidence_refs) AS r
  WHERE r.type = 'text' AND r.value GLOB 'memory:*'
), missing AS (
  SELECT r.* FROM refs AS r
  WHERE NOT EXISTS (
    SELECT 1 FROM memories AS m
    WHERE m.person_id = r.person_id AND 'memory:' || m.id = r.ref
  )
), snapshots AS (
  SELECT l.id AS log_id, l.person_id, l.action, l.reverted,
         'memory:' || json_extract(b.value, '$.id') AS ref
  FROM memory_log AS l, json_each(l.before_json) AS b
  WHERE l.action IN ('merge', 'drop')
    AND json_extract(b.value, '$.person_id') = l.person_id
)
SELECT m.evidence_id, m.pos, m.ref, s.log_id, s.action, s.reverted
FROM missing AS m LEFT JOIN snapshots AS s
  ON s.person_id = m.person_id AND s.ref = m.ref
ORDER BY m.evidence_id, m.pos, s.log_id;
```

- 计数在连接日志**之前**做：将末尾 `SELECT` 换成 `SELECT count(*) FROM refs` 得到总引用次数；换成 `SELECT count(*), count(DISTINCT evidence_id), count(DISTINCT ref) FROM missing` 得到三个悬空计数。上述数量仅为排查时的基线。抽查证据行 26 / `memory:13`、27 / `memory:12` 应对应未撤销的 merge 日志 15，1419 / `memory:22` 对应 merge 日志 18。
- 在备份内逐项核对原行、原 ID、同人、正文、来源及受众、敏感度；检查 `after_json` 确认旧 ID 当时确实被移除（排除同 ID 原地更新），并排除已撤销、后续清空、不记、手动删除、冲突快照或 ID 已被占用。核对后续追封，旧敏感度不是解封依据。不能唯一确认的不处理，继续按未核验封存。
- 另行授权后才在副本演练修复：按同人原 ID 去重，只补确实缺失的原快照，保留出处、时间、受众、有效期并重施现行封存；不改 `evidence_refs`，不拿 `after_json` 的合并句代替，也不整笔 `restore`（会连带删除合并产物，可能破坏另一条证据）。复跑只读对照，逐条验证画像能回查同人原事实、隐私审查不放宽，再提交清单供审批。线上写入需单独批准、备份，并在写事务内重新核对上述条件；本次不执行。
