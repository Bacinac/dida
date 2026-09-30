-- Block-hash insert deduplication for the two history firehoses.
--
-- HistoryWriter re-buffers a batch and retries on ANY insert exception. On an
-- AMBIGUOUS failure — ClickHouse committed the block but the HTTP response was
-- lost (a CH restart or a cut mid-insert) — the retry inserts a SECOND identical
-- copy of up to 50 000 rows. For state_history that permanently double-counts the
-- 1h/1d AggregatingMergeTree rollups (a materialized view's output can't be
-- de-duplicated after the fact); for command_history it doubles audit rows.
--
-- `insert_deduplicate` only takes effect for Replicated engines UNLESS a
-- non-replicated dedup window is set. These tables are plain (non-replicated)
-- MergeTree, so we set it explicitly: a retried insert forms a byte-identical
-- block whose hash is remembered for the last 100 blocks, so the duplicate is
-- dropped while a genuinely new batch (different rows) still lands. Idempotent.
ALTER TABLE state_history MODIFY SETTING non_replicated_deduplication_window = 100;
ALTER TABLE command_history MODIFY SETTING non_replicated_deduplication_window = 100;
