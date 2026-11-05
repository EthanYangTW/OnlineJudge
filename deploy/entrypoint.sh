#!/bin/sh
set -e
umask 077

APP=/app
DATA=/data

mkdir -p "$DATA/log" "$DATA/config" "$DATA/ssl" "$DATA/test_case" "$DATA/public/upload" "$DATA/public/avatar" "$DATA/public/website"

if [ ! -f "$DATA/config/secret.key" ]; then
    openssl rand -hex 32 > "$DATA/config/secret.key"
fi

if [ ! -f "$DATA/public/avatar/default.png" ]; then
    cp data/public/avatar/default.png $DATA/public/avatar
fi

if [ ! -f "$DATA/public/website/favicon.ico" ]; then
    cp data/public/website/favicon.ico $DATA/public/website
fi

SSL="$DATA/ssl"
if [ ! -f "$SSL/server.key" ]; then
    openssl req -x509 -newkey rsa:2048 -keyout "$SSL/server.key" -out "$SSL/server.crt" -days 1000 \
        -subj "/C=CN/ST=Beijing/L=Beijing/O=Beijing OnlineJudge Technology Co., Ltd./OU=Service Infrastructure Department/CN=`hostname`" -nodes
fi

cd $APP/deploy/nginx
ln -sf locations.conf https_locations.conf
if [ -z "$FORCE_HTTPS" ]; then
    ln -sf locations.conf http_locations.conf
else
    ln -sf https_redirect.conf http_locations.conf
fi

if [ -n "${LOWER_IP_HEADER:-}" ]; then
    sed -i "s/__IP_HEADER__/\$http_$LOWER_IP_HEADER/g" api_proxy.conf;
else
    sed -i "s/__IP_HEADER__/\$remote_addr/g" api_proxy.conf;
fi

if [ -z "${MAX_WORKER_NUM:-}" ]; then
    export CPU_CORE_NUM=$(grep -c ^processor /proc/cpuinfo)
    if [ "$CPU_CORE_NUM" -lt 2 ]; then
        export MAX_WORKER_NUM=2
    else
        export MAX_WORKER_NUM=$(($CPU_CORE_NUM))
    fi
fi

cd $APP/dist
if [ -n "${STATIC_CDN_HOST:-}" ]; then
    find . -name "*.*" -type f -exec sed -i "s/__STATIC_CDN_HOST__/\/$STATIC_CDN_HOST/g" {} \;
else
    find . -name "*.*" -type f -exec sed -i "s/__STATIC_CDN_HOST__\///g" {} \;
fi

cd $APP

case "${JUDGE_SERVER_TOKEN:-}:${FCU_TEACHER_PASSWORD:-}:${POSTGRES_PASSWORD:-}" in
    *REPLACE_WITH*)
        echo "Replace all example credentials before starting the FCU service" >&2
        exit 1
        ;;
esac
if [ "${#JUDGE_SERVER_TOKEN}" -lt 24 ] || [ "${#POSTGRES_PASSWORD}" -lt 16 ]; then
    echo "JUDGE_SERVER_TOKEN (24+ characters) and POSTGRES_PASSWORD (16+ characters) are required" >&2
    exit 1
fi

n=0
migrated=false
while [ "$n" -lt 5 ]; do
    if python manage.py migrate --no-input; then
        migrated=true
        break
    fi
    n=$((n+1))
    echo "Migration failed, retrying..." >&2
    sleep 8
done
if [ "$migrated" != true ]; then
    echo "Database migration failed" >&2
    exit 1
fi

python manage.py bootstrap_admin
python manage.py shell -c 'import os; from options.options import SysOptions; SysOptions.judge_server_token = os.environ["JUDGE_SERVER_TOKEN"]'
python manage.py shell -c 'from conf.models import JudgeServer; JudgeServer.objects.update(task_number=0)'

chown -R server:spj $DATA $APP/dist
chmod 750 $DATA
chmod -R u=rwX,g=rX,o= $DATA/public
find $DATA/test_case -type d -exec chmod 710 {} \;
find $DATA/test_case -type f -exec chmod 640 {} \;
exec supervisord -c /app/deploy/supervisord.conf
