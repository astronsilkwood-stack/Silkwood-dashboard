#!/bin/bash
# Keep-alive: starts server + public tunnel if not running. Safe to run repeatedly.
cd /workspace/dashboard/app
pgrep -f "dashboard/app/server.py" >/dev/null || nohup python3 /workspace/dashboard/app/server.py >> server.log 2>&1 &
