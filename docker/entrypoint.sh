#!/usr/bin/env sh
# Wait for Postgres, optionally migrate, then exec the service command.
#
# Only ONE service runs the migration (RUN_MIGRATIONS=true on orders), so three
# containers do not race each other to create the same tables.
set -eu

echo "[entrypoint] ${SERVICE_NAME:-service}: waiting for the database..."
python - <<'PY'
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

URL = os.environ["DATABASE_URL"]
ATTEMPTS = int(os.environ.get("DB_WAIT_ATTEMPTS", "40"))
DELAY = float(os.environ.get("DB_WAIT_DELAY", "1.0"))


# Errors that will never resolve by waiting. Retrying a wrong password for 40
# seconds makes the "bad config" scenario look like a crashed service for its
# first minute, which blurs the one distinction that scenario exists to teach:
# the container is UP and /health reports the real reason.
FATAL_SUBSTRINGS = (
    "password authentication failed",
    "role \"",
    "database \"",
    "no pg_hba.conf entry",
)


def is_fatal(message: str) -> bool:
    lowered = message.lower()
    return any(marker.lower() in lowered for marker in FATAL_SUBSTRINGS)


async def main() -> None:
    for attempt in range(1, ATTEMPTS + 1):
        engine = create_async_engine(URL)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            print(f"[entrypoint] database reachable after {attempt} attempt(s)")
            return
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            print(f"[entrypoint] attempt {attempt}/{ATTEMPTS}: {message}")
            if is_fatal(message):
                print(
                    "[entrypoint] this is a configuration error, not a cold "
                    "start - not retrying. Starting so /health can report it.",
                    file=sys.stderr,
                )
                return
            await asyncio.sleep(DELAY)
        finally:
            await engine.dispose()
    # Do NOT exit non-zero: the "bad config" scenario deliberately gives a
    # service wrong credentials, and the service must still start so that
    # /health can report the failure. Exiting here would turn that scenario
    # into a crash loop, which is a different incident entirely.
    print(
        "[entrypoint] database never became reachable - starting anyway so "
        "/health can report it",
        file=sys.stderr,
    )


asyncio.run(main())
PY

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  echo "[entrypoint] running alembic migrations..."
  alembic upgrade head || echo "[entrypoint] migrations failed (continuing so /health can report it)"
fi

echo "[entrypoint] starting: $*"
exec "$@"
