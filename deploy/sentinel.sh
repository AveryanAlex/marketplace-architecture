#!/bin/sh
set -eu
if [ ! -f /data/sentinel.conf ]; then
  cat > /data/sentinel.conf <<CONFIG
port 26379
bind 0.0.0.0
protected-mode no
dir /data
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel announce-ip ${SENTINEL_HOST}
sentinel monitor flights redis-master 6379 2
sentinel down-after-milliseconds flights 3000
sentinel failover-timeout flights 15000
sentinel parallel-syncs flights 1
CONFIG
fi
exec redis-server /data/sentinel.conf --sentinel
