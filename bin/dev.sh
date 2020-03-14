#!/usr/bin/env bash

# NOTE: do not start and kill in quick succession, appears to not kill all processes

# defaults
TAIL_LENGTH=10

# NOTE: run from root
PATH_ROOT=$(pwd)

# make directory for logs and create log files
mkdir -p logs
echo "" > $PATH_ROOT/logs/api.txt
echo "" > $PATH_ROOT/logs/web.txt
echo "Starting @ $(date)" > $PATH_ROOT/logs/dev.txt

# run flask
cd src/
flask run &> $PATH_ROOT/logs/api.txt & # run in bg and port logs
FLASK_PID=($!)
echo "FLASK_PID: $FLASK_PID" >> $PATH_ROOT/logs/dev.txt
cd ..

# run frontend
cd src/yacs-web
npm run serve &> $PATH_ROOT/logs/web.txt &
WEB_PID=($!)
echo "WEB_PID: $WEB_PID" >> $PATH_ROOT/logs/dev.txt
cd ../..

# kill processes on ctrl-c
function cleanup() {
  echo "------------------------------------"
  echo "cleaning up..."
  echo "killing(FLASK_PID:$FLASK_PID)"
  kill -9 $FLASK_PID
  echo "killing(WEB_PID:$WEB_PID)"
  kill -9 $WEB_PID
}
trap cleanup EXIT

while [[ true ]]
do
  clear
  echo "-----------------------------------"
  echo "dev runner logs"
  echo "-----------------------------------"
  tail -n $TAIL_LENGTH $PATH_ROOT/logs/dev.txt
  echo "-----------------------------------"
  echo "api logs: [last $TAIL_LENGTH lines]"
  echo "-----------------------------------"
  tail -n $TAIL_LENGTH $PATH_ROOT/logs/api.txt
  echo "-----------------------------------"
  echo "web logs: [last $TAIL_LENGTH lines]"
  echo "-----------------------------------"
  tail -n $TAIL_LENGTH $PATH_ROOT/logs/web.txt
  sleep 0.5
done
