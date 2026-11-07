#!/usr/bin/env bash
# GitHub 최신 코드 반영 후 서비스 재시작
# 사용법: 저장소 루트에서  bash deploy/update.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

git pull --ff-only
.venv/bin/pip install -r requirements.txt --quiet
sudo systemctl restart quant-scheduler quant-api
# SOXL 모의 매매(실주문 없음)는 등록된 서버에서만 함께 재시작
if systemctl is-enabled --quiet quant-soxl-paper 2>/dev/null; then
    sudo systemctl restart quant-soxl-paper
fi

echo "✅ 업데이트 완료"
systemctl --no-pager status quant-scheduler quant-api | grep -E "●|Active:"
if systemctl is-enabled --quiet quant-soxl-paper 2>/dev/null; then
    systemctl --no-pager status quant-soxl-paper | grep -E "●|Active:"
fi
