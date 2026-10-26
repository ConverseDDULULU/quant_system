import os
import sys
import time
import json
import warnings
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from dotenv import load_dotenv

# 판다스 및 yfinance 내부 경고 전체 차단
warnings.simplefilter(action="ignore", category=Warning)

# KiwoomClient 임포트
from kiwoom_client import KiwoomClient

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../config/.env"), override=True)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
WATCHLIST = ["AAPL", "NVDA", "MSFT", "TSLA", "AMZN"]

def send_discord_message(content: str):
    """디스코드 알림 전송"""
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=5)
    except Exception as e:
        print(f"⚠️ 디스코드 전송 실패: {e}")

def get_strategy_signal(ticker: str) -> dict:
    """yfinance 기반 기술적 지표 계산 및 매매 시그널 추출 (이동평균 골든크로스 & RSI)"""
    try:
        # 단일 종목 시세 다운로드
        data = yf.download(ticker, period="3mo", interval="1d", progress=False, auto_adjust=True)
        if data.empty or len(data) < 20:
            return None

        # 컬럼 인덱스가 MultiIndex인 경우 1차원 시리즈로 안전하게 추출
        if isinstance(data.columns, pd.MultiIndex):
            if "Close" in data.columns.levels[0]:
                closes = data["Close"].iloc[:, 0]
            else:
                closes = data.iloc[:, 3]
        else:
            closes = data["Close"]

        closes = pd.Series(closes.values.flatten(), index=closes.index, dtype=float)

        # 1. 이동평균선 산출
        ma5_series = closes.rolling(window=5).mean()
        ma20_series = closes.rolling(window=20).mean()

        cur_price = float(closes.iloc[-1])
        ma5 = float(ma5_series.iloc[-1])
        ma20 = float(ma20_series.iloc[-1])

        # 2. RSI 14 산출
        delta = closes.diff()
        gain = (delta.where(delta > 0, 0.0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=14).mean()
        
        # 0으로 나누기 방지
        rs = gain / loss.replace(0, 0.00001)
        rsi_series = 100 - (100 / (1 + rs))
        rsi = float(rsi_series.iloc[-1])

        # 3. 매매 시그널 판정
        signal = "HOLD"
        if ma5 > ma20 and rsi < 55:
            signal = "BUY"
        elif ma5 < ma20 and rsi > 70:
            signal = "SELL"

        return {
            "ticker": ticker,
            "cur_price": cur_price,
            "ma5": ma5,
            "ma20": ma20,
            "rsi": rsi,
            "signal": signal
        }
    except Exception as e:
        print(f"⚠️ [{ticker}] 시세 분석 오류: {e}")
        return None

def run_trading_cycle(client: KiwoomClient, dry_run: bool = True):
    """자동매매 1회 점검 사이클 실행"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 60)
    print(f"⏰ [트레이딩 사이클 시작] {now_str} (모드: {'가상 실행(Dry-Run)' if dry_run else '실제 주문 집행'})")
    print("=" * 60)

    # 1. 예수금 조회
    deposit_info = client.get_deposit()
    print("• 키움 계좌 예수금 상태 확인 완료")

    signals_summary = []

    # 2. 감시 종목 시그널 분석
    for ticker in WATCHLIST:
        analysis = get_strategy_signal(ticker)
        if not analysis:
            continue

        sig = analysis["signal"]
        price = analysis["cur_price"]
        rsi_val = analysis["rsi"]
        ma5_val = analysis["ma5"]
        ma20_val = analysis["ma20"]
        print(f"📊 [{ticker:4s}] 현재가: ${price:7.2f} | MA5: ${ma5_val:7.2f} | MA20: ${ma20_val:7.2f} | RSI: {rsi_val:4.1f} | 판정: {sig}")

        if sig == "BUY":
            signals_summary.append(f"🟢 **[매수 신호]** {ticker} (${price:.2f}, RSI {rsi_val:.1f})")
            if not dry_run:
                res = client.order_us_stock(ticker=ticker, order_type="BUY", qty=1, price=0.0)
                print(f"   ➔ 키움 실계좌 매수 접수: {res.get('return_msg', res)}")
        elif sig == "SELL":
            signals_summary.append(f"🔴 **[매도 신호]** {ticker} (${price:.2f}, RSI {rsi_val:.1f})")
            if not dry_run:
                res = client.order_us_stock(ticker=ticker, order_type="SELL", qty=1, price=0.0)
                print(f"   ➔ 키움 실계좌 매도 접수: {res.get('return_msg', res)}")

    # 3. 디스코드 브리핑 전송
    if signals_summary:
        msg = f"🚀 **[Quant System] 전략 시그널 포착 ({now_str})**\n" + "\n".join(signals_summary)
        send_discord_message(msg)
    else:
        print("ℹ️ 현재 조건에 부합하는 매매 신호가 없습니다. (전 종목 관망/HOLD)")

    print("🏁 [트레이딩 사이클 종료]")

if __name__ == "__main__":
    print("⚡ 퀀트 트레이딩 스케줄러를 초기화합니다...")
    kiwoom = KiwoomClient()

    # 안전 점검을 위한 Dry-Run 실행
    run_trading_cycle(kiwoom, dry_run=True)