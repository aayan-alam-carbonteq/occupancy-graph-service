-- The records DDL now lives in ../../ddl/ and is shared with the persistent
-- clone (clone/docker-compose.clone.yml), so the two can never drift.
-- conftest.py loads ddl/*.sql before this file. Anything remaining here is
-- fixture-only.

-- The full production index set now lives in ddl/002_indexes.sql (loaded
-- before this file -- see the numeric-prefix contract in tests/conftest.py).

-- silver now lives in ddl/003_silver.sql (loaded before this file).
