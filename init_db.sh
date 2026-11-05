#!/bin/bash
set -eu

: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD for the local database}"

if [[ ! -f manage.py ]]; then
    echo "No manage.py, wrong location"
    exit 1
fi

sleep 2
docker rm -f oj-postgres-dev oj-redis-dev >/dev/null 2>&1 || true
docker run -d -e POSTGRES_DB=onlinejudge -e POSTGRES_USER=onlinejudge -e POSTGRES_PASSWORD -p 127.0.0.1:5435:5432 --name oj-postgres-dev postgres:10
docker run -d -p 127.0.0.1:6380:6379 --name oj-redis-dev redis:4.0-alpine

if [ "$1" = "--migrate" ]; then
    sleep 3
    umask 077
    openssl rand -hex 32 > data/config/secret.key
    python manage.py migrate
    python manage.py bootstrap_admin
fi
