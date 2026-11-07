#!/usr/bin/env bash
# 서버에서 SOXL 모의 매매(실주문 없음)를 systemd 서비스로 등록
# 사용법: 저장소 루트에서  bash deploy/setup_soxl_paper.sh
# 끄기:   sudo systemctl disable --now quant-soxl-paper
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="$(whoami)"
cd "$APP_DIR"

sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__APP_USER__|$APP_USER|g" \
    "$APP_DIR/deploy/quant-soxl-paper.service" | sudo tee /etc/systemd/system/quant-soxl-paper.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now quant-soxl-paper

echo "✅ SOXL 모의 매매 등록 완료"
systemctl --no-pager status quant-soxl-paper | grep -E "●|Active:"
echo "로그 보기: journalctl -u quant-soxl-paper -f"
echo "성적 보기: PYTHONPATH=src:. .venv/bin/python src/soxl_paper.py --report"
