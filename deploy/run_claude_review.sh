#!/usr/bin/env bash
# 장마감 후 Claude 분석·조건 조정 (claude-review.timer가 실행)
# 인증: ~/.config/quant/claude.env 에 ANTHROPIC_API_KEY=... 또는 CLAUDE_CODE_OAUTH_TOKEN=...
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
PY=".venv/bin/python"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
AUTH_FILE="$HOME/.config/quant/claude.env"
LOG_DIR="$APP_DIR/logs/claude_review"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y%m%d_%H%M).log"

notify() {
    printf '%s\n' "$1" > /tmp/claude_review_notice.txt
    PYTHONPATH=src:. "$PY" deploy/discord_send.py /tmp/claude_review_notice.txt > /dev/null
}

# 오늘(뉴욕)이 휴장일이면 건너뜀
if ! PYTHONPATH=src:. "$PY" -c "import sys, market_calendar as m; sys.exit(0 if m.is_trading_day(m.now_ny().date()) else 1)"; then
    echo "휴장일이라 건너뜀"
    exit 0
fi

if [ ! -x "$CLAUDE_BIN" ]; then
    notify "⚠️ [Claude 장마감 분석] 서버에 Claude Code가 설치되어 있지 않아 실행하지 못했어요."
    exit 1
fi
if [ ! -f "$AUTH_FILE" ]; then
    notify "⚠️ [Claude 장마감 분석] 인증 파일(~/.config/quant/claude.env)이 없어 실행하지 못했어요."
    exit 1
fi
set -a
# shellcheck disable=SC1090
. "$AUTH_FILE"
set +a

"$CLAUDE_BIN" -p "$(cat deploy/claude_review_prompt.md)" \
    --allowedTools \
        "Read" "Grep" "Glob" "Edit" "Write" \
        "Bash(git pull:*)" "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" \
        "Bash(git add:*)" "Bash(git commit:*)" "Bash(git push origin main)" \
        "Bash(git reset --hard origin/main)" "Bash(git revert:*)" \
        "Bash(journalctl:*)" "Bash(systemctl is-active:*)" "Bash(bash deploy/update.sh)" \
        "Bash(PYTHONPATH=src:. .venv/bin/python:*)" "Bash(.venv/bin/python:*)" \
        "Bash(grep:*)" "Bash(tail:*)" "Bash(head:*)" "Bash(cat:*)" "Bash(ls:*)" \
        "Bash(wc:*)" "Bash(sleep:*)" "Bash(date:*)" \
    > "$LOG_FILE" 2>&1
status=$?

# 30일 넘은 실행 로그 정리
find "$LOG_DIR" -name '*.log' -mtime +30 -delete

if [ "$status" -ne 0 ]; then
    notify "⚠️ [Claude 장마감 분석] 실행이 실패했어요(종료 코드 $status). 서버 로그: $LOG_FILE"
fi
exit "$status"
