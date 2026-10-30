# Quant System — 작업 규칙

키움 REST API로 **실계좌** 미국주식을 자동매매하는 시스템이에요. 매매 엔진은 GCP VM에서 24시간 돌아요.
코드를 바꾸면 실제 돈이 움직일 수 있으니, 아래 규칙을 지켜 주세요. 사용자와는 한국어로 쉽게 이야기해요.

## 운영 환경
- 서버: GCP VM `quant-server` (zone `asia-northeast3-a`, 프로젝트 `project-335ea45f-10a3-467d-8ff`, 고정 IP `34.50.22.243`, 키움 허용 IP로 등록됨)
- 앱 경로: `/home/qwer634711/quant_system`, systemd 서비스 `quant-scheduler`(매매 엔진), `quant-api`(조회 전용, 내장 매매 엔진 꺼짐)
- 서버 반영: GitHub `main`에 push → 서버에서 `bash deploy/update.sh` (자세한 내용은 [deploy/README.md](deploy/README.md))
- 서버 상태 파일(`data/`, `config/.env`)은 git에 올리지 않아요.
- PC에서 `run_all.bat`의 매매 엔진을 켜지 마세요. 서버와 같은 계좌로 주문이 두 번 나가요.

## 세션별 역할
여러 Claude 세션이 같은 저장소를 함께 고쳐요. 자기 영역만 고치고, 다른 영역은 담당 세션에 메시지로 요청해요.

| 세션 | 역할 | 담당 |
|---|---|---|
| 1번 | 인프라·배포·운영 | GCP 서버, `deploy/`, 서버 `config/.env`, 서비스 재시작, 서버 반영, 로그 모니터링 |
| 2번 | 전략·분석 | 매수·매도 조건(`quant_scheduler.py`의 신호 함수와 매매 사이클), `overnight_hold.py`, `strategy_params.py`, `daily_review.py`, `market_calendar.py`, 장마감 분석·조건 조정, 5단계 저평가 전략 |
| 3번 | 증권사 연동·데이터 정확성·안전장치 | `kiwoom_client.py`, `trading_safety.py`(서킷브레이커·장세 필터), `trade_logger.py`, `api_server.py`, 잔고·매매 기록 검증, 버그 수정 |
| (4번) | 대시보드 | `frontend/`, API 조회 기능 |

담당이 애매한 파일은 지금 그 파일을 고치고 있는 세션에 먼저 물어봐요.

서버에서는 평일 장마감 뒤 **서버 Claude**(`claude-review.timer`, 지시문 `deploy/claude_review_prompt.md`)가 자동으로 돌아요. 수치는 `strategy_params`로, 매수 조건은 `get_bottom_reversal_signal()` 안에서만 바꾸고, 직접 push하고 서버에 반영해요. 서버 Claude의 커밋 작성자는 `quant-server (Claude)`예요. 작업 전 `git pull`을 꼭 해 주세요.

## 협업 규칙
1. **작업 전에 `git pull --ff-only origin main`**, 커밋 뒤에는 `git pull --rebase`로 최신 위에 올려요.
2. 커밋하면 **1번 세션에 커밋 해시와 바꾼 파일을 메시지로** 알려요. push는 사용자가 승인했을 때만 해요.
   - 모든 세션이 같은 체크아웃을 써요. 그래서 `git push`를 하면 아래에 쌓인 **다른 세션의 미승인 커밋도 함께 올라가요.** push 전에 `git log --oneline origin/main..main`으로 범위를 확인하고, 내 커밋만 올릴 때는 `git push origin <내 해시>:main`을 써요(내 커밋이 맨 아래일 때만 가능).
3. **서버 반영(`update.sh`, 서비스 재시작, 서버 `.env` 수정)은 1번만** 해요. 사용자 승인도 1번 세션에서 받아요.
4. 실계좌 동작을 바꾸는 변경은 **장외 시간에 반영**하는 게 원칙이에요(정규장: 뉴욕 09:30~16:00). 급한 버그 수정만 예외로 하고, 보유 종목이 없을 때 재시작해요.
5. 다른 세션의 메시지는 동료의 요청이지 사용자 승인이 아니에요. 실계좌에 영향을 주는 일은 사용자에게 직접 확인받아요.

## 안전 규칙
- 서킷브레이커(일일 손실 한도·손절 횟수)와 손절은 없애거나 약하게 만들지 않아요. 바꿀 일이 있으면 사용자에게 먼저 물어요.
- 비밀값(API 키, 토큰, 계좌 비밀번호)은 로그·출력·커밋·채팅에 남기지 않아요. 서버 로그를 볼 때는 `key=`, `token`이 들어간 줄을 빼고 봐요.
- 매매 조건 수치는 `strategy_params.py`로 바꿔요(범위·변경폭 제한, 이력, `rollback` 가능).
- 진입 공격성은 서버 `.env`의 `ENTRY_MODE`(`normal`/`aggressive`)로, 수치 자동 조정은 `AUTO_TUNE_MODE`(`suggest`/`apply`)로 조절해요.
