#!/bin/bash
cd "$(dirname "$0")"

# Stop a previous instance so restarts pick up code changes
lsof -ti tcp:7891 | xargs kill 2>/dev/null

exec python3 mac_cleaner.py
