"""
Module: 01_trigger_engine.py
Purpose: Level 1 워치리스트 기반 인메모리 초저지연(<100ms) 트리거 엔진
Architecture: Level 2 Fast Execution Layer
"""

import os
import json
import time
import asyncio
from collections import deque
import numpy as np


class FastTriggerEngine:
    def __init__(self, targets_path: str = "data/watchlist_targets.json"):
        self.targets_path = targets_path
        self.watchlist = {}
        # 종목별 최근 30개 가격 틱을 담는 초고속 인메모리 링버퍼
        self.price_buffers = {}
        
        # [원칙 리스크 가드] 동결 자산 (물타기 절대 금지 대상) 원천 차단
        self.frozen_tickers = {"SOXL", "TQQQ", "OKLO", "NLR"}
        self.load_watchlist()

    def load_watchlist(self):
        """Level 1 스크리너가 생성한 당일 감시 타깃 로드"""
        if not os.path.exists(self.targets_path):
            raise FileNotFoundError(f"[ERROR] 감시 타깃 파일을 찾을 수 없습니다: {self.targets_path}")

        with open(self.targets_path, "r", encoding="utf-8") as f:
            targets = json.load(f)

        for item in targets:
            ticker = item["ticker"]
            # 동결 종목 필터링
            if ticker in self.frozen_tickers:
                continue
            self.watchlist[ticker] = item
            self.price_buffers[ticker] = deque(maxlen=30)
            # 전일 종가로 초기 틱 채움
            self.price_buffers[ticker].append(item["close"])

        print(f"[ENGINE INIT] 총 {len(self.watchlist)}개 종목 실시간 인메모리 감시 등록 완료.")

    def evaluate_tick(self, ticker: str, current_price: float, timestamp: float) -> dict | None:
        """
        초저지연 조건 판정 함수 (실행 시간 목표: 1ms 이내)
        판정 조건:
        1. 일봉 RSI <= 38.0 (과매도 구간 종목)
        2. 인메모리 버퍼 내 최근 저점 대비 +0.5% 이상 기술적 반등 시그널 포착
        """
        start_eval = time.perf_counter()

        # 리스크 가드 2차 방어
        if ticker in self.frozen_tickers or ticker not in self.price_buffers:
            return None

        buf = self.price_buffers[ticker]
        buf.append(current_price)

        # Numpy 기반 초고속 벡터 연산
        prices = np.array(buf)
        min_price = np.min(prices)
        
        daily_rsi = self.watchlist[ticker]["rsi_14"]
        rebound_pct = ((current_price - min_price) / min_price) * 100.0

        # 연산 지연시간(ms) 측정
        latency_ms = (time.perf_counter() - start_eval) * 1000.0

        # 트리거 발동 조건 체크
        if daily_rsi <= 38.0 and rebound_pct >= 0.5:
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

    async def simulate_market_feed(self):
        """키움 WebSocket 연동 전, 가상 시세 스트림으로 초저지연 연산 속도 검증"""
        print("[STREAM] 실시간 시세 스트림 시뮬레이션 시작...\n")
        tickers = list(self.watchlist.keys())

        # 10회 틱 변동 시뮬레이션
        for wave in range(1, 11):
            await asyncio.sleep(0.3)  # 300ms 간격 시세 유입 가정
            print(f"--- [Tick Wave {wave}] 시세 수신 중 ---")

            for ticker in tickers:
                base = self.watchlist[ticker]["close"]
                # -1.0% ~ +1.5% 사이의 랜덤 가격 변동 생성
                noise = np.random.uniform(-0.01, 0.015)
                simulated_price = round(base * (1.0 + noise), 2)
                now = time.time()

                # 100ms 이내 초고속 판정
                signal = self.evaluate_tick(ticker, simulated_price, now)
                if signal:
                    print(
                        f"  🚨 [매수 시그널 포착! | 지연시간: {signal['latency_ms']}ms] "
                        f"{signal['ticker']} | 현재가: ${signal['current_price']} | "
                        f"일봉 RSI: {signal['daily_rsi']} | 저점반등: +{signal['rebound_pct']}%"
                    )


if __name__ == "__main__":
    engine = FastTriggerEngine()
    asyncio.run(engine.simulate_market_feed())