#!/usr/bin/env bash
# GitHub 최신 코드 반영 후 서비스 재시작
# 사용법: 저장소 루트에서  bash deploy/update.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

git pull --ff-only
.venv/bin/pip install -r requirements.txt --quiet
sudo systemctl restart quant-scheduler quant-api

echo "✅ 업데이트 완료"
systemctl --no-pager status quant-scheduler quant-api | grep -E "●|Active:"
