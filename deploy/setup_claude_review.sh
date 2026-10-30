#!/usr/bin/env bash
# 서버에서 장마감 Claude 분석 자동 실행을 설정
# 사용법: 저장소 루트에서  bash deploy/setup_claude_review.sh
# 먼저 Claude Code 설치(deploy/README.md 7단계)와 인증 파일 작성이 필요해요.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="$(whoami)"
KEY="$HOME/.ssh/quant_deploy"
cd "$APP_DIR"

echo "=== [1/4] GitHub 배포 키 (서버가 분석 결과를 push할 때 사용) ==="
if [ ! -f "$KEY" ]; then
    ssh-keygen -t ed25519 -N "" -C "quant-server deploy key" -f "$KEY" > /dev/null
fi
if ! grep -q "Host github.com" "$HOME/.ssh/config" 2>/dev/null; then
    printf 'Host github.com\n  IdentityFile %s\n  IdentitiesOnly yes\n' "$KEY" >> "$HOME/.ssh/config"
    chmod 600 "$HOME/.ssh/config"
fi
ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null
sort -u -o "$HOME/.ssh/known_hosts" "$HOME/.ssh/known_hosts"

echo "=== [2/4] git push 경로와 작성자 설정 ==="
FETCH_URL="$(git remote get-url origin)"
REPO_PATH="${FETCH_URL#https://github.com/}"
git remote set-url --push origin "git@github.com:${REPO_PATH}"
git config user.name "quant-server (Claude)"
git config user.email "quant-server@users.noreply.github.com"

echo "=== [3/4] 인증 파일 폴더 ==="
mkdir -p "$HOME/.config/quant"
chmod 700 "$HOME/.config/quant"
[ -f "$HOME/.config/quant/claude.env" ] && chmod 600 "$HOME/.config/quant/claude.env"

echo "=== [4/4] systemd 타이머 등록 (평일 뉴욕 17:30) ==="
for unit in claude-review.service claude-review.timer; do
    sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__APP_USER__|$APP_USER|g" \
        "$APP_DIR/deploy/$unit" | sudo tee "/etc/systemd/system/$unit" > /dev/null
done
chmod +x "$APP_DIR/deploy/run_claude_review.sh"
sudo systemctl daemon-reload
sudo systemctl enable --now claude-review.timer

echo ""
echo "✅ 설정 완료. 다음 실행 예정:"
systemctl list-timers claude-review.timer --no-pager | head -2
echo ""
echo "아래 공개키를 GitHub 저장소 Settings → Deploy keys 에 'Allow write access'로 등록하세요:"
cat "$KEY.pub"
