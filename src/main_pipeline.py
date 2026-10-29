import asyncio
import os
import sys
import time
# 프로젝트 루트 경로 등록
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from kiwoom_client import KiwoomClient
from level3_ai.ai_reporter import AIReporter

class NasdaqQuantEngine:
    def __init__(self):
        self.kiwoom = KiwoomClient()
        self.ai_reporter = AIReporter()
        
        # 감시 대상: 나스닥 대표 종목들 (티커, 거래소)
        self.target_universe = [
            {"ticker": "NVDA", "exchange": "NAS"},
            {"ticker": "AAPL", "exchange": "NAS"},
            {"ticker": "GOOGL", "exchange": "NAS"},
            {"ticker": "TSLA", "exchange": "NAS"}
        ]

    async def evaluate_and_trade(self, item: dict):
        ticker = item["ticker"]
        exchange = item["exchange"]

        # 1. 키움 해외주식(나스닥) 시세 수신
        quote = self.kiwoom.get_overseas_price(ticker, exchange)
        price = quote.get("price", 0.0)
        volume = quote.get("volume", 0)
        status = quote.get("status")

        print(f"\n[NASDAQ STREAM | {status}] {ticker} 실시간 단가: ${price:,.2f} (거래량: {volume:,})")

        # 2. 임의 지표 계산 (시뮬레이션 조건 충족 가정)
        # 추후 여기에 Level 1/2 기술적 지표(RSI, 이동평균, 볼린저밴드 등) 실시간 연산 결합
        mock_rsi = 29.4
        mock_volume_spike = 3.10

        # 3. 매수 전략 트리거 (RSI 30 이하 과매도 구간)
        if mock_rsi <= 30.0:
            print(f"🔥 [STRATEGY TRIGGER] {ticker} 매수 조건 부합! 나스닥 주문 실행")

            # 4. 키움 나스닥 매수 주문 전송
            order_res = self.kiwoom.send_overseas_order(
                ticker=ticker,
                qty=5,
                price=price,
                exchange=exchange,
                order_type="BUY"
            )

            # 5. 디스코드 비동기 브리핑 카드 발송 (메인 스레드 멈춤 없이 백그라운드 전송)
            asyncio.create_task(
                self.ai_reporter.analyze_and_brief(
                    ticker=f"{ticker} ({exchange})",
                    current_price=price,
                    rsi=mock_rsi,
                    volume_spike=mock_volume_spike
                )
            )

    async def start_loop(self):
        print("=" * 65)
        print("🚀 [NASDAQ QUANT SENTINEL] 나스닥 실시간 감시 & 디스코드 엔진 가동")
        print("=" * 65)

        for item in self.target_universe:
            await self.evaluate_and_trade(item)
            await asyncio.sleep(2)  # 종목 간 호출 텀

if __name__ == "__main__":
    engine = NasdaqQuantEngine()
    asyncio.run(engine.start_loop())