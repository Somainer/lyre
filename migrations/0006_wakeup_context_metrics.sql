-- 0006_wakeup_context_metrics.sql
-- Per-wakeup telemetry for in-wakeup context compaction.
--
-- `context_peak_tokens` is the largest input_tokens value the API reported
-- across the wakeup's turns. Since each API call resends the whole messages
-- list, the per-turn `input_tokens` IS the running context size — this
-- column captures the max so the dashboard can flag wakeups that ran
-- close to the model's context window.
--
-- `compaction_count` is how many times the wakeup auto-compacted its
-- history mid-flight (>0 means we hit the 70% threshold at least once).
-- More than one or two = wakeup probably should have been split via
-- dispatch_task instead.

ALTER TABLE wakeups ADD COLUMN context_peak_tokens INTEGER;
ALTER TABLE wakeups ADD COLUMN compaction_count INTEGER NOT NULL DEFAULT 0;
