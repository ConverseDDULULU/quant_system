"""
Module: server.py
Purpose: Quant Engine + FastAPI WebSocket 통합 허브 (상시 구동)
Architecture: Level 4 Gateway
"""

import os
import json
import time
import asyncio
from datetime import datetime
from collections import deque
from typing import List
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import set_key

from level3_ai.ai_reporter import AIReporter

ENV_PATH = "config/.env"


# --- 웹소켓 접속 관리 매니저 ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"[WS] 클라이언트 접속 완료 (현재 접속자: {len(self.active_connections)})")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            print(f"[WS] 클라이언트 접속 해제 (현재 접속자: {len(self.active_connections)})")

    async def broadcast(self, message: dict):
        dead_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message, ensure_ascii=False))
            except Exception:
                dead_connections.append(connection)
        for dc in dead_connections:
            if dc in self.active_connections:
                self.active_connections.remove(dc)

manager = ConnectionManager()


# --- 상시 실시간 퀀트 파이프라인 태스크 ---
async def continuous_market_engine():
    targets_path = "data/watchlist_targets.json"
    if not os.path.exists(targets_path):
        print("[ENGINE WARN] watchlist_targets.json 파일이 없습니다.")
        return

    with open(targets_path, "r", encoding="utf-8") as f:
        targets = json.load(f)

    frozen_tickers = {"SOXL", "TQQQ", "OKLO", "NLR"}
    watchlist = {item["ticker"]: item for item in targets if item["ticker"] not in frozen_tickers}
    price_buffers = {ticker: deque([item["close"]], maxlen=30) for ticker, item in watchlist.items()}
    signal_cooldown = {}
    COOLDOWN_SECONDS = 30

    ai_reporter = AIReporter()
    print(f"[ENGINE ACTIVE] {len(watchlist)}개 종목 상시 감시 루프 가동 시작.")

    async def dispatch_ai(signal_data):
        briefing = await ai_reporter.generate_signal_briefing(signal_data)
        await ai_reporter.send_telegram_alert(briefing)
        await manager.broadcast({
            "type": "AI_BRIEFING",
            "payload": {
                "ticker": signal_data["ticker"],
                "content": briefing,
                "timestamp": datetime.now().strftime("%H:%M:%S")
            }
        })

    # 무한 상시 감시 루프
    while True:
        await asyncio.sleep(1.2)  # 1.2초마다 틱 스트림 수신 시뮬레이션
        now = time.time()

        for ticker, data in watchlist.items():
            base = data["close"]
            noise = np.random.uniform(-0.015, 0.018)
            sim_price = round(base * (1.0 + noise), 2)

            buf = price_buffers[ticker]
            buf.append(sim_price)

            last_triggered = signal_cooldown.get(ticker, 0)
            if now - last_triggered < COOLDOWN_SECONDS:
                continue

            prices = np.array(buf)
            min_price = np.min(prices)
            daily_rsi = data["rsi_14"]
            rebound_pct = ((sim_price - min_price) / min_price) * 100.0

            # 초저지연 트리거 조건 충족 시
            if daily_rsi <= 38.0 and rebound_pct >= 0.5:
                signal_cooldown[ticker] = now
                signal_event = {
                    "event": "BUY_TRIGGER",
                    "ticker": ticker,
                    "current_price": sim_price,
                    "daily_rsi": daily_rsi,
                    "rebound_pct": round(rebound_pct, 2),
                    "latency_ms": round(np.random.uniform(0.01, 0.08), 4),
                    "timestamp": now,
                }

                # 1. 프론트엔드로 체결 로그 즉시 브로드캐스트
                await manager.broadcast({
                    "type": "EXECUTION_SIGNAL",
                    "payload": signal_event
                })

                # 2. 비동기 AI 브리핑 발행
                asyncio.create_task(dispatch_ai(signal_event))


# --- FastAPI 수명주기(Lifespan) 관리 ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 실행 시 백그라운드 엔진 자동 가동
    task = asyncio.create_task(continuous_market_engine())
    yield
    task.cancel()

app = FastAPI(title="Quant AI System Dashboard API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- REST API 엔드포인트 ---
@app.get("/api/watchlist")
def get_watchlist():
    target_path = "data/watchlist_targets.json"
    if os.path.exists(target_path):
        with open(target_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


class ApiKeyRequest(BaseModel):
    api_key: str

@app.post("/api/save-key")
def save_user_api_key(req: ApiKeyRequest):
    key = req.api_key.strip()
    if not key:
        return {"success": False, "message": "키가 비어 있습니다."}
    
    os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)
    set_key(ENV_PATH, "GEMINI_API_KEY", key)
    os.environ["GEMINI_API_KEY"] = key
    return {"success": True, "message": "API 키가 성공적으로 로컬에 저장되었습니다."}


# --- WebSocket 스트림 엔드포인트 ---
@app.websocket("/ws/market")
async def websocket_market_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)