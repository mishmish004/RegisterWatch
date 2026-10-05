-- 0003 — remove source rows that no register writes to.
--
-- Earlier seeds created 'ukgc_businesses' (bulk/zip mode, never written) and
-- 'ukgc' (renamed gb_ukgc when every register moved to <country>_<regulator>
-- slugs). Delete them where nothing references them: raw_snapshots.source_id is
-- ON DELETE RESTRICT, and evidence is never deleted to tidy a config table.
-- Where they do hold snapshots, disable them so `status` stops reporting them.

DELETE FROM sources s
 WHERE s.slug IN ('ukgc_businesses', 'ukgc')
   AND NOT EXISTS (SELECT 1 FROM raw_snapshots r WHERE r.source_id = s.id);

UPDATE sources SET enabled = false WHERE slug IN ('ukgc_businesses', 'ukgc');
