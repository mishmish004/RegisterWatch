-- 0002 — intentionally empty.
--
-- This file used to seed a UKGC source row. Sources are now upserted by the
-- ingest itself from src/registerwatch/registers/*, so the code is the only
-- place a register's slug, name and config are defined and a seed cannot drift
-- from it. Kept (empty) because migration history already references the name.
SELECT 1;
