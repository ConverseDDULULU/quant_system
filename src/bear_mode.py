"""하락장 대응: 1배 인버스 ETF 진입 판단

키움 일반 계좌는 공매도가 안 되므로, 하락장에서는 지수와 반대로 움직이는 1배 인버스 ETF를 산다.
3배 상품(SOXS, SQQQ 등)은 쓰지 않는다 (사용자 결정 2026-10-31, 레버리지로 위험을 키우지 않는다는 원칙).

진입 조건 (모두 충족):
  1. 하락장: QQQ 완성된 5분봉 종가가 EMA20보다 BEAR_EMA_GAP 이상 아래 + EMA20 하락 중 + EMA60 아래
  2. 악재 확인: Gemini 뉴스 판정에서 QQQ 또는 SPY가 "악재" (판정 실패·중립이면 진입 안 함)
  3. 인버스 ETF 자체도 상승 탄력: 1분봉 양봉, VWAP 위, MA5 > EMA20, OBV 유입
손절·익절·서킷브레이커·종목당 투자금 상한은 일반 매수와 똑같이 적용된다 (스케줄러에서 처리).

.env BEAR_MODE=off 이면 끈다 (기본 on).
"""

import os
import threading
import time

import numpy as np

from overnight_hold import get_news_verdict
from trading_safety import is_momentum_continuing, kiwoom_minute_rows_to_frame

INVERSE_ETFS = (("PSQ", "NY"), ("SH", "NY"))  # 나스닥100 1배 인버스, S&P500 1배 인버스
BEAR_EMA_GAP = 0.005  # QQQ가 EMA20보다 0.5% 이상 아래
TREND_CACHE_SECONDS = 300
NEWS_CACHE_SECONDS = 1800

_lock = threading.Lock()
_trend_cache = (0.0, (False, "아직 하락장 판단 전"))
_news_cache = (0.0, (False, "아직 악재 확인 전"))


def is_enabled() -> bool:
    return os.getenv("BEAR_MODE", "on").strip().lower() != "off"


def _completed_frame(client, ticker: str, exchange: str, interval: str):
    rows = client.get_us_stock_minute_chart(ticker, exchange=exchange, interval=interval, max_pages=1)
    df = kiwoom_minute_rows_to_frame(rows)
    if df.empty:
        return df
    df[["Open", "High", "Low", "Close"]] = df[["Open", "High", "Low", "Close"]].abs()
    return df.iloc[:-1]  # 진행 중인 마지막 봉 제외


def bear_trend(close) -> tuple[bool, str]:
    """QQQ 5분봉 종가 시리즈로 하락장 여부 판정 (순수 함수, 테스트용)"""
    if len(close) < 64:
        return False, "QQQ 5분봉 데이터 부족"
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema60 = close.ewm(span=60, adjust=False).mean()
    last = float(close.iloc[-1])
    gap = last / float(ema20.iloc[-1]) - 1
    falling = ema20.iloc[-1] < ema20.iloc[-4]
    below_long = last < float(ema60.iloc[-1])
    if gap <= -BEAR_EMA_GAP and falling and below_long:
        return True, f"QQQ EMA20 대비 {gap * 100:+.2f}%, EMA20 하락, EMA60 아래"
    return False, f"하락장 아님 (QQQ EMA20 대비 {gap * 100:+.2f}%)"


def detect_bear_market(client) -> tuple[bool, str]:
    global _trend_cache
    with _lock:
        cached_at, result = _trend_cache
        if time.monotonic() - cached_at < TREND_CACHE_SECONDS:
            return result
        try:
            df = _completed_frame(client, "QQQ", "ND", "5")
            result = bear_trend(df["Close"].dropna()) if not df.empty else (False, "QQQ 5분봉 없음")
        except Exception as e:
            result = (False, f"QQQ 조회 실패: {e}")
        _trend_cache = (time.monotonic(), result)
        return result


def confirm_bad_news() -> tuple[bool, str]:
    """QQQ·SPY 뉴스 중 하나라도 Gemini가 '악재'로 판정하면 True (30분 캐시)"""
    global _news_cache
    with _lock:
        cached_at, result = _news_cache
        if time.monotonic() - cached_at < NEWS_CACHE_SECONDS:
            return result
    verdicts = []
    for ticker in ("QQQ", "SPY"):
        verdict, reason = get_news_verdict(ticker)
        verdicts.append((ticker, verdict, reason))
        if verdict == "악재":
            break
    bad = [v for v in verdicts if v[1] == "악재"]
    if bad:
        result = (True, f"{bad[0][0]} 악재: {bad[0][2]}")
    else:
        result = (False, " / ".join(f"{t} {v}({r})" for t, v, r in verdicts))
    with _lock:
        _news_cache = (time.monotonic(), result)
    return result


def inverse_momentum(df) -> bool:
    """인버스 ETF 1분봉이 상승 탄력을 보이는지 (순수 함수, 테스트용)"""
    if len(df) < 25:
        return False
    closes, volumes = df["Close"], df["Volume"]
    typical = (df["High"] + df["Low"] + closes) / 3
    vwap = float((typical * volumes).sum() / (volumes.sum() + 1e-9))
    obv = (np.sign(closes.diff()) * volumes).fillna(0).cumsum()
    return is_momentum_continuing(
        price=float(closes.iloc[-1]),
        vwap=vwap,
        short_ma=float(closes.rolling(5).mean().iloc[-1]),
        long_ma=float(closes.ewm(span=20, adjust=False).mean().iloc[-1]),
        obv=float(obv.iloc[-1]),
        obv_ma=float(obv.rolling(5).mean().iloc[-1]),
        candle_green=float(closes.iloc[-1]) >= float(df["Open"].iloc[-1]),
    )


def pick_inverse_etf(client, exclude: set) -> dict | None:
    """상승 탄력이 있는 인버스 ETF 하나를 고른다. 없으면 None"""
    for ticker, exchange in INVERSE_ETFS:
        if ticker in exclude:
            continue
        try:
            df = _completed_frame(client, ticker, exchange, "1")
        except Exception as e:
            print(f"⚠️ [하락장 대응] {ticker} 분봉 조회 실패: {e}")
            continue
        if inverse_momentum(df):
            return {"ticker": ticker, "exchange": exchange, "price": float(df["Close"].iloc[-1])}
        print(f"   ⏸️ [하락장 대응] {ticker} 상승 탄력 없음, 진입 보류")
    return None
