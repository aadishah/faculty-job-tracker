#!/bin/bash
# Run the tracker automatically every day on this Mac.
#
#   ./schedule.sh install            check daily, results stay on this Mac
#   ./schedule.sh install publish    check daily and update the shared web page
#                                    (needs permission to push to the GitHub repository)
#   ./schedule.sh install publish 07:30    choose the time (default 09:30)
#   ./schedule.sh status             show whether it is scheduled and the last run
#   ./schedule.sh remove             stop the daily run
#
# If the Mac is asleep at the set time, the check runs when it next wakes.
set -e

LABEL="com.faculty-job-tracker.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/faculty-job-tracker.log"
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$HOME/.faculty-job-tracker"
DOMAIN="gui/$(id -u)"

unload() { launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true; }

case "${1:-}" in
install)
  MODE="${2:-local}"
  TIME="${3:-09:30}"
  if [[ "$MODE" != "local" && "$MODE" != "publish" ]] || [[ ! "$TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    echo "Usage: ./schedule.sh install [local|publish] [HH:MM]"; exit 1
  fi
  command -v python3 >/dev/null || { echo "Python is missing. Run: xcode-select --install"; exit 1; }

  # macOS does not let background jobs read Desktop, Documents or Downloads,
  # so a tracker stored there is scheduled from its own copy in the home folder.
  DIR="$HERE"
  case "$HERE/" in
  "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*)
    DIR="$RUNNER"
    if [[ -d "$RUNNER/.git" ]]; then
      git -C "$RUNNER" pull -q --rebase origin main || true
    else
      git clone -q "$(git -C "$HERE" remote get-url origin)" "$RUNNER"
    fi
    echo "This folder is in a protected location, so the daily run uses a copy at $RUNNER"
    ;;
  esac

  if [[ "$MODE" == "publish" ]]; then
    COMMAND="./run_local.sh"
  else
    COMMAND="echo \"--- \$(date '+%Y-%m-%d %H:%M') check\"; python3 tracker.py"
  fi

  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  unload
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-c</string><string>cd "$DIR" &amp;&amp; $COMMAND</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$((10#${TIME%%:*}))</integer><key>Minute</key><integer>$((10#${TIME##*:}))</integer></dict>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "Scheduled: every day at $TIME ($MODE mode)."
  echo "Dashboard on this Mac: $DIR/docs/index.html"
  echo "Log: $LOG"
  ;;
status)
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Scheduled."
    launchctl print "$DOMAIN/$LABEL" | grep -E "last exit code|runs =" | sed 's/^[[:space:]]*/  /'
  else
    echo "Not scheduled."
  fi
  [[ -f "$LOG" ]] && { echo "Last log lines:"; tail -n 8 "$LOG"; }
  ;;
remove)
  unload
  rm -f "$PLIST"
  echo "Daily run removed. The tracker files were left in place."
  ;;
*)
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  ;;
esac
