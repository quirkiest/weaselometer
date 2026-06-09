#!/bin/sh
# Redeploy WeaselOMeter (mirrors ARMR.sh). Rebuilds the image and restarts.
cd "`dirname $0`"

docker compose down -v --rmi 'all'
docker compose up -d
