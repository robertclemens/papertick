#!/bin/sh
set -e

if [ "$1" = "api" ]; then
  python -m app.init_db
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --proxy-headers
fi

exec "$@"
