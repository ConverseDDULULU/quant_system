import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv

# config/.env 파일 경로 명시적 로드
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../config/.env"))

class AIReporter:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.discord_webhook = os.getenv("DISCORD_WEBHOOK_URL")

    def _generate_briefing(self, ticker: str, current_price: float, rsi: float, volume_spike: float) -> str:
        """기계적 리스크 브리핑 메시지 생성"""
        return (
            f"**• 체결 종목/단가**: {ticker} @ ${current_price:,.2f}\n"
            f"**• 지표 상태**: RSI(14) {rsi:.2f} | 거래량 {volume_spike:.2f}배 급증\n"
            f"**• 운용 원칙**: 사전 설정 조건 부합, 1차 분할 진입 집행\n"
            f"**• 행동 수칙**: 단기 노이즈에 반응 금지. 목표가 및 손절 밴드 내 기계적 홀딩"
        )

    async def analyze_and_brief(self, ticker: str, current_price: float, rsi: float, volume_spike: float) -> str:
        briefing = self._generate_briefing(ticker, current_price, rsi, volume_spike)
        # 디스코드 실시간 알림 발송
        await self.send_discord_alert(briefing, ticker, current_price)
        return briefing

    async def send_discord_alert(self, message: str, ticker: str = "", price: float = 0.0) -> None:
        """디스코드 웹훅으로 임베드 알림 전송"""
        if not self.discord_webhook or "discord.com" not in self.discord_webhook:
            print(f"[DISCORD WARN] 유효한 웹훅 주소가 없습니다. 현재 값: {self.discord_webhook}")
            return

        import requests

        payload = {
            "username": "Quant Sentinel Engine",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/3135/3135715.png",
            "embeds": [
                {
                    "title": f"⚡ [체결 신호 감지] {ticker} @ ${price:,.2f}",
                    "description": message,
                    "color": 3066993,  # 초록색
                    "footer": {
                        "text": "Quant Execution & Risk Sentinel"
                    },
                    "timestamp": datetime.utcnow().isoformat()
                }
            ]
        }

        try:
            res = await asyncio.to_thread(requests.post, self.discord_webhook, json=payload, timeout=5)
            if res.status_code in [200, 204]:
                print(f"[DISCORD SUCCESS] {ticker} 디스코드 알림 발송 완료! (상태 코드: {res.status_code})")
            else:
                print(f"[DISCORD FAIL] 전송 실패: HTTP {res.status_code} - {res.text}")
        except Exception as e:
            print(f"[DISCORD ERROR] 예외 발생: {e}")

if __name__ == "__main__":
    print("디스코드 웹훅 알림 테스트 시작...")
    reporter = AIReporter()
    asyncio.run(reporter.analyze_and_brief("GOOGL", 328.95, 33.46, 2.85))