#!/bin/bash
set -euo pipefail

label="org.pinpoint.community.backend"
domain="gui/$(id -u)"
service="$domain/$label"
plist="$HOME/Library/LaunchAgents/$label.plist"
logs_dir="$HOME/Library/Logs/PinPoint"
action=${1:-status}

case "$action" in
  start)
    [[ -f "$plist" ]] || { echo "Run ./scripts/setup.sh first." >&2; exit 1; }
    if ! launchctl print "$service" >/dev/null 2>&1; then
      launchctl bootstrap "$domain" "$plist"
    fi
    launchctl kickstart -k "$service"
    ;;
  stop)
    if launchctl print "$service" >/dev/null 2>&1; then
      launchctl bootout "$domain" "$plist"
    fi
    ;;
  restart)
    if launchctl print "$service" >/dev/null 2>&1; then
      launchctl bootout "$domain" "$plist"
    fi
    launchctl bootstrap "$domain" "$plist"
    ;;
  status)
    if launchctl print "$service" >/dev/null 2>&1; then
      echo "PinPoint backend is loaded."
      curl --fail --silent http://127.0.0.1:8787/healthz || true
      echo
    else
      echo "PinPoint backend is stopped."
      exit 1
    fi
    ;;
  logs)
    tail -n 100 "$logs_dir/backend-error.log" "$logs_dir/backend.log" 2>/dev/null || true
    ;;
  *)
    echo "Usage: $0 start|stop|restart|status|logs" >&2
    exit 2
    ;;
esac
