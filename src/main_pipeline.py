"""
Module: main_pipeline.py
Purpose: Level 2(초저지연 트리거) + Level 3(비동기 AI 리포터) 통합 런타임
Architecture: Core Real-time Engine
"""

import os
import sys
import json
import time
import asyncio
from collections import deque
import numpy as np

from level3_ai.ai_reporter import AIReporter


class IntegratedQuantRunner:
    def __init__(self, targets_path: str = "data/watchlist_targets.json"):
        self.targets_path = targets_path
        self.watchlist = {}
        self.price_buffers = {}
        
        # [원칙 리스크 가드] 동결 자산 필터링
        self.frozen_tickers = {"SOXL", "TQQQ", "OKLO", "NLR"}
        
        # 신호 중복 발송 방지용 쿨다운 타이머
        self.signal_cooldown = {}
        self.COOLDOWN_SECONDS = 60
        
        # 백그라운드 AI 태스크 목록 추적
        self.background_tasks = set()
        
        # Level 3 AI 브리퍼
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
        await self.ai_reporter.send_telegram_alert(briefing)

    async def run_market_loop(self):
        print("[RUNNER] 실시간 통합 파이프라인 가동 시작...\n")
        tickers = list(self.watchlist.keys())

        # 6개 파동 시세 스트림 시뮬레이션
        for wave in range(1, 7):
            await asyncio.sleep(0.5)
            print(f">>> [Stream Wave {wave}] 틱 데이터 도착")

            for ticker in tickers:
                base = self.watchlist[ticker]["close"]
                noise = np.random.uniform(-0.008, 0.016)
                simulated_price = round(base * (1.0 + noise), 2)
                now = time.time()

                signal = self.evaluate_tick(ticker, simulated_price, now)

                if signal:
                    print(
                        f"  ⚡ [0.1초 체결 트리거!] {signal['ticker']} | "
                        f"${signal['current_price']} (반등 +{signal['rebound_pct']}%) | "
                        f"판정지연: {signal['latency_ms']}ms"
                    )

                    # 백그라운드 태스크 등록 및 세트에 보관
                    task = asyncio.create_task(self._dispatch_ai_worker(signal))
                    self.background_tasks.add(task)
                    task.add_done_callback(self.background_tasks.discard)

        # 스트림 종료 후 실행 중인 AI 리포트 태스크 완료 대기
        if self.background_tasks:
            print("\n[RUNNER] 발행된 백그라운드 AI 브리핑 태스크 완료 대기 중...")
            await asyncio.gather(*list(self.background_tasks), return_exceptions=True)
            print("[RUNNER] 모든 AI 브리핑 출력 완료.")


if __name__ == "__main__":
    runner = IntegratedQuantRunner()
    asyncio.run(runner.run_market_loop())