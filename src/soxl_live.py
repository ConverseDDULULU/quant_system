"""SOXL·SOXS 단타 실거래 (스위치: 서버 config/.env의 SOXL_LIVE=on, 기본 꺼짐)

사용자 결정(2026-11-11, 1번 세션에서 확인): SOXL·SOXS 단타를 실거래로 시작.
  · 수량 1주 고정, 손절 수익률 -2.5%, 익절 수익률 +5% (키움 잔고 수익률 pl_rt 기준, 수수료 포함)
  · 서킷브레이커(하루 손실 한도·손절 횟수)는 그대로 적용, SOXL 손절도 똑같이 센다
기본값(1번 제안):
  · 진입 신호는 soxl_paper와 같은 로직(SOXX 5분봉 추세 속 눌림, soxl_paper.signal_direction 공용)
  · 개장 15분 뒤 ~ 마감 90분 전 봉에서만 판단, SOXL과 SOXS 중 한 번에 하나만 보유
  · 하루 최대 2번 진입: 종목별 당일 재진입 차단(trading_safety)을 그대로 따르므로 SOXL 1번 + SOXS 1번까지
  · 마감 10분 전 정리, 오버나잇 홀딩 안 함, 보유 칸(max_holdings) 1칸 사용
  · 1주 고정이라 진입 단가 $50 상한·최소 주문 금액 예외. 종목당 계좌 1/2 상한은 지킨다
스위치가 꺼져 있으면 이 모듈은 아무것도 하지 않고, SOXL을 들고 있더라도 엔진의 일반 기준으로 관리된다.
"""

import os

import pandas as pd
import yfinance as yf

import soxl_paper
from market_calendar import NY_TZ, now_ny, session_bounds

TICKERS = ("SOXL", "SOXS")
EXCHANGE = "NA"  # 키움 거래소 코드 (AMEX·NYSE Arca)
BRANCH = "soxl"
QTY = 1
STOP_LOSS_PCT = -2.5
TAKE_PROFIT_PCT = 5.0
MAX_ENTRIES_PER_DAY = 2
BUY_LIMIT_PREMIUM = 1.002  # 지정가 매수를 현재가보다 0.2% 위에 내 체결되게 (실제 체결은 매도호가)

_last_checked_bar = None  # 이미 판단한 SOXX 5분봉 (같은 봉으로 두 번 진입하지 않게)


def is_enabled() -> bool:
    return os.getenv("SOXL_LIVE", "off").strip().lower() == "on"


def manages(ticker: str) -> bool:
    """이 모듈의 손절·익절·청산 기준을 쓸 종목인지 (스위치가 켜졌을 때만)"""
    return is_enabled() and str(ticker).upper().strip() in TICKERS


def exit_rule(profit_rate: float) -> str | None:
    """키움 잔고 수익률(pl_rt) 기준. 엔진의 get_exit_rule과 같은 이름을 돌려준다"""
    if profit_rate <= STOP_LOSS_PCT:
        return "STOP_LOSS"
    if profit_rate >= TAKE_PROFIT_PCT:
        return "TAKE_PROFIT"
    return None


def label(reason: str) -> str:
    """매매 기록·디스코드에서 일반 매매와 구분되는 청산 이유 (예: 손절(soxl))"""
    return f"{reason}({BRANCH})"


def _soxx_frame(now) -> pd.DataFrame | None:
    """완성된 SOXX 5분봉 (정규장) + 지표. 실패하면 None (재시도 없음: 다음 스캔에 다시 본다)"""
    try:
        df = yf.Ticker(soxl_paper.BASE).history(period="5d", interval="5m", prepost=False, auto_adjust=False)
    except Exception as e:
        print(f"⚠️ [SOXL 실거래] SOXX 분봉 조회 실패: {type(e).__name__}: {e}")
        return None
    if df is None or df.empty:
        print("⚠️ [SOXL 실거래] SOXX 분봉 없음, 이번 스캔 진입 판단 건너뜀")
        return None
    df = df.tz_convert(NY_TZ)
    df = df[df.index + soxl_paper.BAR + soxl_paper.BAR_SETTLE <= now]
    df = soxl_paper.regular_only(df)
    if len(df) < 25:
        return None
    return soxl_paper.indicators(df)


def check_signal(now=None, frame_fn=_soxx_frame) -> dict | None:
    """마지막 완성 봉에서 진입 신호를 본다. {"ticker", "bar", "reason"} 또는 None. 같은 봉은 한 번만 판단"""
    global _last_checked_bar
    now = now or now_ny()
    bounds = session_bounds(now.date())
    if not bounds:
        return None
    base = frame_fn(now)
    if base is None or base.empty:
        return None
    i = len(base) - 1
    ts = base.index[i]
    if ts.date() != now.date():
        return None
    open_dt, close_dt = bounds
    if not (open_dt + pd.Timedelta(minutes=soxl_paper.ENTRY_START_MIN) <= ts <= close_dt - pd.Timedelta(minutes=soxl_paper.ENTRY_END_MIN)):
        return None
    if _last_checked_bar == ts:
        return None
    _last_checked_bar = ts
    direction = soxl_paper.signal_direction(base, i)
    if not direction:
        return None
    row = base.iloc[i]
    trend = "상승 추세 속 눌림 반등" if direction > 0 else "하락 추세 속 반등 실패"
    return {
        "ticker": "SOXL" if direction > 0 else "SOXS",
        "bar": ts,
        "reason": f"SOXX {ts.strftime('%H:%M')} 봉 {trend} (종가 ${row['Close']:.2f}, EMA20 ${row['ema20']:.2f}, VWAP ${row['vwap']:.2f})",
    }


def quote_price(client, ticker: str) -> float:
    """키움 현재가(usa20100 cur_prc). 실패하면 0"""
    try:
        data = client.get_us_stock_quote(ticker, EXCHANGE) or {}
        return abs(float(str(data.get("cur_prc", "0")).replace(",", "").replace("+", "") or 0))
    except (TypeError, ValueError, AttributeError) as e:
        print(f"⚠️ [SOXL 실거래] {ticker} 현재가 확인 실패: {e}")
        return 0.0
