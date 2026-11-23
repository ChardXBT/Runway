#!/bin/bash
export REPORT_ROOT=$HOME/Downloads/kiro-wave-20261122-runway
export LOCK_DIR=$HOME/Downloads/kiro-wave-20261122-runway/heavy.lock
export KIRO_MODEL=claude-opus-5.5 KIRO_EFFORT=high STUCK_SECS=1500
bash $HOME/.gemini/antigravity/scratch/ai-skills/capabilities/kiro-cli-agents/scripts/kiro_watchdog.sh $HOME/Downloads/kiro-wave-20261122-runway/jobs-wave1.txt
