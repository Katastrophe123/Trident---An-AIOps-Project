-- Create the role the three services actually connect as.
--
-- Why this file exists at all: the database-exhaustion scenario did not work
-- without it. POSTGRES_USER=aiops is a **superuser**, and PostgreSQL never
-- enforces max_connections against superusers - they are always allowed into
-- the slots held back by superuser_reserved_connections. So the server could be
-- sitting at its cap, with psql reporting "sorry, too many clients already",
-- while all three services cheerfully opened fresh connections and reported
-- themselves healthy. The fault was real and completely invisible.
--
-- `aiops_app` is NOSUPERUSER, so once client backends reach
-- (max_connections - superuser_reserved_connections) its new connections are
-- refused with "too many clients already" - which is the symptom the scenario
-- is supposed to produce.
--
-- A useful side effect, and how a real incident goes: an operator can still get
-- in with psql as the superuser to diagnose the problem, because those reserved
-- slots are exactly what they are for.

CREATE ROLE aiops_app LOGIN PASSWORD 'aiops' NOSUPERUSER NOCREATEDB NOCREATEROLE;

GRANT ALL ON DATABASE aiops TO aiops_app;

-- PostgreSQL 15+ no longer lets non-owners create objects in `public`, and
-- alembic (running as this role) has to create the demo tables.
ALTER SCHEMA public OWNER TO aiops_app;
GRANT ALL ON SCHEMA public TO aiops_app;
