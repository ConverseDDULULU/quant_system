"""
Module: main_pipeline.py
Purpose: Level 2(초저지연 트리거) + Level 3(비동기 AI 리포터) + WebSocket 대시보드 송출
Architecture: Core Real-time Engine
"""

import os
import sys
import json
import time
import asyncio
from datetime import datetime
from collections import deque
import numpy as np

from level3_ai.ai_reporter import AIReporter
from api.server import manager  # 웹소켓 브로드캐스트 매니저 임포트


class IntegratedQuantRunner:
    def __init__(self, targets_path: str = "data/watchlist_targets.json"):
        self.targets_path = targets_path
        self.watchlist = {}
        self.price_buffers = {}
        
        # [원칙 리스크 가드] 동결 자산 필터링
        self.frozen_tickers = {"SOXL", "TQQQ", "OKLO", "NLR"}
        
        # 중복 발송 방지용 쿨다운
        self.signal_cooldown = {}
        self.COOLDOWN_SECONDS = 60
        self.background_tasks = set()
        
        self.ai_reporter = AIReporter()
        self.load_watchlist()

    def load_watchlist(self):
        if not os.path.exists(self.targets_path):
            raise FileNotFoundError(f"[ERROR] 감시 타깃 파일 없음: {self.targets_path}")

        with open(self.targets_path, "r", encoding="utf-8") as f:
            targets = json.load(f)

        for item in targets:
            ticker = item["ticker"]
            if ticker in self.frozen_tickers:
                continue
            self.watchlist[ticker] = item
            self.price_buffers[ticker] = deque(maxlen=30)
            self.price_buffers[ticker].append(item["close"])

        print(f"[RUNNER INIT] {len(self.watchlist)}개 종목 실시간 감시 파이프라인 준비 완료.")

    def evaluate_tick(self, ticker: str, current_price: float, timestamp: float) -> dict | None:
        start_eval = time.perf_counter()

        if ticker in self.frozen_tickers or ticker not in self.price_buffers:
            return None

        last_triggered = self.signal_cooldown.get(ticker, 0)
        if timestamp - last_triggered < self.COOLDOWN_SECONDS:
            return None

        buf = self.price_buffers[ticker]
        buf.append(current_price)

        prices = np.array(buf)
        min_price = np.min(prices)
        
        daily_rsi = self.watchlist[ticker]["rsi_14"]
        rebound_pct = ((current_price - min_price) / min_price) * 100.0

        latency_ms = (time.perf_counter() - start_eval) * 1000.0

        if daily_rsi <= 38.0 and rebound_pct >= 0.5:
            self.signal_cooldown[ticker] = timestamp
            return {
                "event": "BUY_TRIGGER",
                "ticker": ticker,
                "current_price": current_price,
                "daily_rsi": daily_rsi,
                "rebound_pct": round(rebound_pct, 2),
                "latency_ms": round(latency_ms, 4),
                "timestamp": timestamp,
            }

        return None

    async def _dispatch_ai_worker(self, signal: dict):
        briefing = await self.ai_reporter.generate_signal_briefing(signal)
        
        # 1. 터미널 로깅 및 텔레그램 발송
        await self.ai_reporter.send_telegram_alert(briefing)
        
        # 2. 웹 대시보드로 실시간 AI 브리핑 브로드캐스트
        await manager.broadcast({
            "type": "AI_BRIEFING",
            "payload": {
                "ticker": signal["ticker"],
                "content": briefing,
                "timestamp": datetime.now().strftime("%H:%M:%S")
            }
        })

    async def run_market_loop(self):
        print("[RUNNER] 실시간 통합 파이프라인 가동 시작...\n")
        tickers = list(self.watchlist.keys())

        # 무한 감시 루프 (테스트를 위해 15초 동안 틱 스트림 지속 발생)
        for wave in range(1, 20):
            await asyncio.sleep(1.0)
            print(f">>> [Stream Wave {wave}] 틱 수신 중...")

            for ticker in tickers:
                base = self.watchlist[ticker]["close"]
                noise = np.random.uniform(-0.012, 0.018)
                simulated_price = round(base * (1.0 + noise), 2)
                now = time.time()

                signal = self.evaluate_tick(ticker, simulated_price, now)

                if signal:
                    print(
                        f"  ⚡ [0.1초 체결 트리거!] {signal['ticker']} | "
                        f"${signal['current_price']} (반등 +{signal['rebound_pct']}%) | "
                        f"판정지연: {signal['latency_ms']}ms"
                    )

                    # 웹 대시보드로 실시간 체결 로그 브로드캐스트
                    await manager.broadcast({
                        "type": "EXECUTION_SIGNAL",
                        "payload": signal
                    })

                    # 비동기 AI 브리핑 태스크 발행
                    task = asyncio.create_task(self._dispatch_ai_worker(signal))
                    self.background_tasks.add(task)
                    task.add_done_callback(self.background_tasks.discard)

        if self.background_tasks:
            await asyncio.gather(*list(self.background_tasks), return_exceptions=True)


if __name__ == "__main__":
    runner = IntegratedQuantRunner()
    asyncio.run(runner.run_market_loop())