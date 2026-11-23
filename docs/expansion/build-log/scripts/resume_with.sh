#!/bin/bash
# Resume a finished Kiro job's session with a new instruction file, then watch it.
# Usage: bash resume_with.sh <job> <unix-cwd> <windows-cwd> <message-file>
set -u
JOB=$1; CWD=$2; WCWD=$3; MSG=$4
W=$HOME/Downloads/kiro-wave-20261122-runway
SK=$HOME/.gemini/antigravity/scratch/ai-skills/capabilities/kiro-cli-agents/scripts
export MSYS_NO_PATHCONV=1
KIRO="${LOCALAPPDATA}/Kiro-Cli/kiro-cli.exe"
n=1; while [ -e "$W/$JOB/REPORT-$n.md" ]; do n=$((n+1)); done
[ -e "$W/$JOB/REPORT.md" ] && mv "$W/$JOB/REPORT.md" "$W/$JOB/REPORT-$n.md"
sid=$(cd "$CWD" && "$KIRO" chat --list-sessions -f json 2>/dev/null | py "$(cygpath -m "$SK")/kiro_latest_session.py" "$WCWD")
echo "$(date '+%Y-%m-%d %H:%M') resume_with $JOB: session ${sid:-newest} with $(basename "$MSG")" >> "$W/LAUNCH_LOG.md"
if [ -n "$sid" ]; then how=(--resume-id "$sid"); else how=(--resume); fi
( cd "$CWD" && "$KIRO" chat "${how[@]}" --no-interactive --trust-all-tools --model claude-opus-5.5 --effort high "$(cat "$MSG")" ) >> "$W/$JOB/kiro.log" 2>&1 &
sleep 40
printf '%s|%s|%s|%s|adopt\n' "$JOB" "$CWD" "$WCWD" "$MSG" > "$W/jobs-adopt-$JOB.txt"
REPORT_ROOT=$W LOCK_DIR=$W/heavy.lock KIRO_MODEL=claude-opus-5.5 KIRO_EFFORT=high STUCK_SECS=1500 \
  bash "$SK/kiro_watchdog.sh" "$W/jobs-adopt-$JOB.txt"
