# GCP 24시간 가동 가이드

PC를 꺼도 매매 엔진(`quant_scheduler.py`)과 API 서버(`api_server.py`)가 구글 클라우드 VM에서 계속 돌도록 설정하는 방법입니다.
두 프로그램은 systemd 서비스로 등록되어, 오류로 죽거나 VM이 재부팅돼도 10초 후 자동으로 다시 켜집니다.

## 1. VM 만들기 (GCP 콘솔)

[Compute Engine → VM 인스턴스 → 인스턴스 만들기](https://console.cloud.google.com/compute/instancesAdd)

| 항목 | 권장값 | 이유 |
|---|---|---|
| 리전 | `asia-northeast3` (서울) | 키움 API 서버가 한국에 있어 주문 지연이 가장 짧음 |
| 머신 유형 | `e2-small` (2 vCPU 공유, 2GB) | pandas·yfinance 사용 시 1GB(e2-micro)는 메모리 부족 위험 |
| 부팅 디스크 | Ubuntu 24.04 LTS, 20GB | |
| 방화벽 | HTTP/HTTPS 허용 **체크하지 않음** | 외부에 포트를 열 필요 없음 (SSH만 사용) |

> 비용: 서울 e2-small 상시 가동 시 대략 월 $15~20 수준입니다. 정확한 금액은 콘솔의 예상 비용을 확인하세요.

## 2. 고정 IP 예약 + 키움에 IP 등록

VM의 외부 IP는 기본적으로 재시작 시 바뀔 수 있습니다. 키움 REST API는 허용 IP 기반이라 IP가 바뀌면 인증이 실패합니다.

1. [VPC 네트워크 → IP 주소](https://console.cloud.google.com/networking/addresses/list)에서 VM의 외부 IP를 **고정(Static)으로 승격**
2. 키움 REST API 관리 페이지에서 이 IP를 **허용 IP로 등록**

## 3. 서버에 코드와 환경변수 올리기

로컬 PC에 [gcloud CLI](https://cloud.google.com/sdk/docs/install)를 설치한 뒤 (`<VM이름>`은 1단계에서 만든 이름):

```bash
gcloud compute ssh <VM이름> --zone asia-northeast3-a --command "git clone https://github.com/ConverseDDULULU/quant_system.git"
```

```bash
gcloud compute scp config/.env <VM이름>:~/quant_system/config/.env --zone asia-northeast3-a
```

`config/.env`는 git에 올라가지 않으므로 반드시 이렇게 직접 복사해야 합니다.
기존 매매 기록(`data/` 폴더)도 이어서 쓰려면 같은 방식으로 복사하세요:

```bash
gcloud compute scp --recurse data <VM이름>:~/quant_system/ --zone asia-northeast3-a
```

## 4. 설치 실행

```bash
gcloud compute ssh <VM이름> --zone asia-northeast3-a --command "cd quant_system && bash deploy/setup_server.sh"
```

끝에 `Active: active (running)`이 두 줄 나오면 성공입니다.

## 5. 일상 운영

| 하고 싶은 것 | 명령 (VM에 SSH 접속 후) |
|---|---|
| 매매 엔진 실시간 로그 | `journalctl -u quant-scheduler -f` |
| API 서버 로그 | `journalctl -u quant-api -f` |
| 상태 확인 | `systemctl status quant-scheduler quant-api` |
| 코드 업데이트 반영 | `cd quant_system && bash deploy/update.sh` |
| 매매 긴급 중지 | `sudo systemctl stop quant-scheduler` |
| 다시 시작 | `sudo systemctl start quant-scheduler` |

## 6. 대시보드를 로컬에서 보기

API 서버는 보안을 위해 VM 내부(127.0.0.1:8000)에서만 열려 있습니다. SSH 터널을 열어두면 로컬 대시보드가 그대로 동작합니다:

```bash
gcloud compute ssh <VM이름> --zone asia-northeast3-a -- -L 8000:localhost:8000
```

이 창을 켜둔 채로 로컬에서 `frontend` 폴더의 `npm run dev`를 실행하면 됩니다.

## 7. 장마감 Claude 분석 자동 실행 (PC 없이)

평일 뉴욕 17:30(장마감 1시간 30분 뒤)에 서버가 Claude Code를 실행해 그날 매매를 분석하고, 익절·손절 수치와 매수 조건을 조정해 반영한 뒤 디스코드로 알려요. 지시문은 [claude_review_prompt.md](claude_review_prompt.md)에 있어요.

1. **Claude Code 설치** (서버에서 한 번):
   ```bash
   gcloud compute ssh quant-server --zone asia-northeast3-a --command "curl -fsSL https://claude.ai/install.sh | bash"
   ```
2. **인증 파일 만들기**: 서버의 `~/.config/quant/claude.env`에 둘 중 하나를 넣어요.
   - API 키(사용량만큼 과금): `ANTHROPIC_API_KEY=sk-ant-...`
   - Claude 구독 사용: 서버에서 `claude setup-token`을 실행해 받은 토큰을 `CLAUDE_CODE_OAUTH_TOKEN=...`으로
3. **설정 스크립트 실행**:
   ```bash
   gcloud compute ssh quant-server --zone asia-northeast3-a --command "cd quant_system && bash deploy/setup_claude_review.sh"
   ```
   마지막에 출력되는 공개키를 GitHub 저장소 **Settings → Deploy keys → Add deploy key**에 **Allow write access**를 체크해서 등록하세요.
4. **바로 시험해 보기**: `sudo systemctl start claude-review.service` 후 `ls ~/quant_system/logs/claude_review/`에서 로그를 확인해요.

| 하고 싶은 것 | 명령 (서버) |
|---|---|
| 다음 실행 시각 | `systemctl list-timers claude-review.timer` |
| 자동 분석 끄기 | `sudo systemctl disable --now claude-review.timer` |
| 수치 변경 되돌리기 | `cd quant_system && PYTHONPATH=src:. .venv/bin/python src/strategy_params.py rollback` |

## ⚠️ 주의

- **로컬 PC와 서버에서 동시에 엔진을 켜지 마세요.** 같은 계좌로 주문이 두 번 나갑니다. 서버 가동 후에는 `run_all.bat`의 매매 엔진(3번)을 실행하지 마세요.
- 이 GitHub 저장소는 **공개(public)** 상태입니다. 비밀키는 올라가지 않지만 매매 전략 코드는 누구나 볼 수 있습니다.
