"""
Module: ai_reporter.py
Purpose: 비동기 워커 기반 Gemini 시황 브리핑 및 멘탈 가이드 생성 (과부하 자동 대응)
Architecture: Level 3 AI Reporter & Async Notification
"""

import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv

# 사용자 키 입력 및 검증 게이트웨이 임포트
from common.auth_guard import ensure_gemini_api_key

load_dotenv("config/.env")


class AIReporter:
    def __init__(self): 
        self.api_key = ensure_gemini_api_key()
        self.client = None

        if self.api_key:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
                print("[AI INIT] Gemini 클라이언트 연결 완료.")
            except Exception as e:
                print(f"[AI WARN] 클라이언트 초기화 실패 ({e}). 모의 브리퍼 모드로 동작합니다.")
        else:
            print("[AI INIT] 키 미등록 상태. 로컬 Mock 브리퍼 모드로 대기합니다.")

    async def generate_signal_briefing(self, signal_data: dict) -> str:
        ticker = signal_data["ticker"]
        price = signal_data["current_price"]
        rsi = signal_data["daily_rsi"]
        rebound = signal_data["rebound_pct"]
        latency = signal_data["latency_ms"]

        if not self.client:
            return (
                f"📊 **[퀀트 시스템 가이드 | {ticker}]**\n"
                f"• 체결 단가: ${price} (RSI {rsi}, 저점 대비 +{rebound}%)\n"
                f"• 알고리즘 지연: {latency}ms (초저지연 충족)\n"
                f"• 리스크 관리: 과매도 반등 국면 기계적 분할 진입. 동결 자산(레버리지 등) 뇌동매매 엄금."
            )

        prompt = f"""
당신은 엄격한 퀀트 트레이딩 리스크 관리자입니다.
방금 시스템에서 다음 매수 시그널이 발생했습니다:
- 종목: {ticker}
- 현재가: ${price}
- 14일 일봉 RSI: {rsi} (과매도 기준 38 이하 충족)
- 인메모리 단기 저점 대비 반등폭: +{rebound}%
- 알고리즘 판단 지연시간: {latency}ms

다음 원칙을 지켜 텔레그램 전송용 메시지를 작성하세요:
1. 마크다운 형식으로 가독성 있게 3줄 내외 요약.
2. 감정적 몰빵/뇌동매매를 차단하고 기계적 분할 매수 원칙 강조.
3. 말투는 군더더기 없고 단호한 시니어 퀀트 톤으로 작성.
"""

        candidate_models = ["gemini-2.0-flash", "gemini-1.5-flash"]

        for model_name in candidate_models:
            try:
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=model_name,
                    contents=prompt,
                )
                if response and response.text:
                    return response.text.strip()
            except Exception as e:
                err_str = str(e)
                # 503 과부하, 404 모델 없음 시 다음 모델 시도
                if "503" in err_str or "404" in err_str:
                    continue
                # 429 Quota 초과 시 곧바로 정갈한 정량 fallback으로 즉시 탈출
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    break

        # 모든 모델 실패 또는 Quota 한도 도달 시 깔끔한 마크다운 대체 가이드
        return (
            f"⚡ **[퀀트 신호 리스크 가이드 | {ticker}]**\n"
            f"• **체결 단가**: ${price} (일봉 RSI {rsi} 과매도 / 반등 +{rebound}%)\n"
            f"• **운용 원칙**: 1차 분할 매수 구간이며, 손절 라인은 직전 저점 이탈 시 기계적 집행.\n"
            f"• **멘탈 제어**: AI 일시 한도와 무관하게 뇌동매매 및 레버리지 추격을 금지합니다."
        )

    async def send_telegram_alert(self, message: str) -> None:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not bot_token or not chat_id:
            print("\n" + "=" * 50)
            print("[TELEGRAM DISPATCH PREVIEW]")
            print(message)
            print("=" * 50 + "\n")
            return

        import requests
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        await asyncio.to_thread(requests.post, url, json=payload, timeout=5)


async def test_worker():
    reporter = AIReporter()
    sample_signal = {
        "ticker": "GOOGL",
        "current_price": 328.95,
        "daily_rsi": 33.46,
        "rebound_pct": 2.24,
        "latency_ms": 0.0634,
    }
    
    print("[TEST] Level 3 비동기 브리핑 태스크 발행...")
    briefing = await reporter.generate_signal_briefing(sample_signal)
    await reporter.send_telegram_alert(briefing)


if __name__ == "__main__":
    asyncio.run(test_worker())