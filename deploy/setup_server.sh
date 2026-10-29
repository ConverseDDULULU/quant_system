#!/usr/bin/env bash
# GCP VM(Ubuntu/Debian) 최초 설치 스크립트
# 사용법: 저장소를 clone 한 뒤, 저장소 루트에서  bash deploy/setup_server.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="$(whoami)"

echo "=== [1/5] 시스템 패키지 설치 ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo "=== [2/5] 서버 시간대를 Asia/Seoul 로 설정 (로컬 PC와 동일하게) ==="
sudo timedatectl set-timezone Asia/Seoul

echo "=== [3/5] 파이썬 가상환경 및 패키지 설치 ==="
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p data logs

echo "=== [4/5] 환경변수 파일 확인 ==="
if [ ! -f "$APP_DIR/config/.env" ]; then
    echo "❌ config/.env 가 없습니다. 로컬 PC에서 먼저 업로드하세요 (deploy/README.md 3단계)."
    exit 1
fi
chmod 600 "$APP_DIR/config/.env"

echo "=== [5/5] systemd 서비스 등록 (재부팅/오류 시 자동 재시작) ==="
for svc in quant-scheduler quant-api; do
    sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__APP_USER__|$APP_USER|g" \
        "$APP_DIR/deploy/$svc.service" | sudo tee "/etc/systemd/system/$svc.service" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now quant-scheduler quant-api

echo ""
echo "✅ 설치 완료"
systemctl --no-pager status quant-scheduler quant-api | grep -E "●|Active:"
echo "로그 보기: journalctl -u quant-scheduler -f"
