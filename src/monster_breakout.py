import math
from typing import Dict, Any, Optional

import numpy as np
import pandas as pd
import yfinance as yf


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]
    return df


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window=period).mean()
    loss = (-delta.clip(upper=0)).rolling(window=period).mean()
    rs = gain / (loss.replace(0, np.nan) + 1e-9)
    return 100 - (100 / (1 + rs))


def _stoch_k(dframe: pd.DataFrame, k_period: int = 14, smooth_k: int = 3) -> pd.Series:
    low_min = dframe["Low"].rolling(window=k_period).min()
    high_max = dframe["High"].rolling(window=k_period).max()
    fast_k = ((dframe["Close"] - low_min) / (high_max - low_min + 1e-9)) * 100
    return fast_k.rolling(window=smooth_k).mean()


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_volume_burst_score(df: pd.DataFrame) -> Dict[str, Any]:
    """거래량 급증 강도 점수 계산. 점수가 높을수록 '몬스터 급등주' 초입에 가까움."""
    if df.empty or len(df) < 30:
        return {
            "score": 0.0,
            "signal": "NO_SIGNAL",
            "burst_ratio": 0.0,
            "burst_vs_median": 0.0,
            "vol_ratio_5m": 0.0,
            "is_accel": False,
        }

    dframe = _flatten_columns(df.copy())
    needed = {"Open", "High", "Low", "Close", "Volume"}
    missing = needed - set(dframe.columns)
    if missing:
        return {"score": 0.0, "signal": "NO_SIGNAL", "burst_ratio": 0.0, "burst_vs_median": 0.0, "vol_ratio_5m": 0.0, "is_accel": False}

    close = pd.to_numeric(dframe["Close"], errors="coerce").astype(float)
    volume = pd.to_numeric(dframe["Volume"], errors="coerce").fillna(0).astype(float)
    dframe["TypicalPrice"] = (dframe["High"] + dframe["Low"] + dframe["Close"]) / 3
    dframe["TypicalPrice"] = pd.to_numeric(dframe["TypicalPrice"], errors="coerce")

    if volume.empty or volume.iloc[-1] <= 0:
        return {"score": 0.0, "signal": "NO_SIGNAL", "burst_ratio": 0.0, "burst_vs_median": 0.0, "vol_ratio_5m": 0.0, "is_accel": False}

    avg_10 = volume.tail(10).mean()
    avg_20 = volume.tail(20).mean()
    avg_50 = volume.tail(50).mean()
    med_50 = volume.tail(50).median()
    recent_vol = float(volume.iloc[-1])
    prev_vol = float(volume.iloc[-2]) if len(volume) >= 2 else recent_vol
    burst_ratio = recent_vol / max(avg_20, 1.0)
    burst_vs_median = recent_vol / max(med_50, 1.0)
    vol_ratio_5m = recent_vol / max(avg_10, 1.0)
    accel = recent_vol / max(prev_vol, 1.0)

    ema20 = _ema(close, 20)
    ema8 = _ema(close, 8)
    vwap = (dframe["TypicalPrice"] * volume).cumsum() / volume.cumsum().replace(0, np.nan)
    vwap_gap = (close.iloc[-1] / (vwap.iloc[-1] + 1e-9) - 1.0) * 100

    price_momentum = (close.iloc[-1] / close.iloc[-2] - 1.0) * 100 if len(close) >= 2 else 0.0
    rsi_val = float(_rsi(close).iloc[-1])
    stoch_k = float(_stoch_k(dframe, 14, 3).iloc[-1])
    is_accel = accel >= 1.25 and recent_vol >= max(avg_20 * 2.5, 1.0)

    score = 0.0
    score += min(max(burst_ratio, 0.0), 20.0) * 1.8
    score += min(max(burst_vs_median, 0.0), 20.0) * 1.3
    score += min(max(vol_ratio_5m, 0.0), 12.0) * 2.2
    score += max(0.0, min(price_momentum * 3.0, 15.0))
    score += max(0.0, min(vwap_gap * 0.5, 15.0))
    score += 8.0 if is_accel else 0.0

    signal = "NO_SIGNAL"
    # 초입 급등주 탐지 : 거래량이 벽을 뚫고, 가격이 20EMA 위로 올라오며, RSI와 스토캐스틱이 중립~강세 구간에 있으면 매수 후보 강하게 부여
    if (
        burst_ratio >= 4.0
        and burst_vs_median >= 2.5
        and is_accel
        and close.iloc[-1] >= ema20.iloc[-1]
        and vwap_gap >= 0.0
        and (36 <= rsi_val <= 68 or rsi_val >= 55)
        and stoch_k >= 35.0
    ):
        signal = "BUY"

    return {
        "score": round(float(score), 2),
        "signal": signal,
        "burst_ratio": round(float(burst_ratio), 2),
        "burst_vs_median": round(float(burst_vs_median), 2),
        "vol_ratio_5m": round(float(vol_ratio_5m), 2),
        "rsi": round(float(rsi_val), 2),
        "stoch_k": round(float(stoch_k), 2),
        "vwap_gap_pct": round(float(vwap_gap), 2),
        "is_accel": bool(is_accel),
        "momentum_pct": round(float(price_momentum), 2),
    }


def get_volume_burst_signal(ticker: str, max_price: float = 50.0) -> Optional[Dict[str, Any]]:
    """바닥권(약 $3~$50) 종목에서 '거래량 폭발' 신호를 포착한다."""
    try:
        stock = yf.Ticker(ticker)
        df_1m = stock.history(period="5d", interval="1m", auto_adjust=True)
        if df_1m.empty or len(df_1m) < 30:
            df_1m = stock.history(period="10d", interval="5m", auto_adjust=True)
        if df_1m.empty or len(df_1m) < 30:
            return None

        df = _flatten_columns(df_1m.copy())
        last_price = _safe_float(df["Close"].iloc[-1], 0.0)
        if last_price <= 0 or last_price > max_price:
            return None

        burst = compute_volume_burst_score(df)
        if burst["signal"] != "BUY":
            return None

        return {
            "ticker": ticker,
            "signal": "BUY",
            "price": last_price,
            "score": burst["score"],
            "burst_ratio": burst["burst_ratio"],
            "burst_vs_median": burst["burst_vs_median"],
            "vol_ratio_5m": burst["vol_ratio_5m"],
            "rsi": burst["rsi"],
            "stoch_k": burst["stoch_k"],
            "vwap_gap_pct": burst["vwap_gap_pct"],
            "momentum_pct": burst["momentum_pct"],
        }
    except Exception:
        return None


def build_partial_exit_plan(
    entry_price: float,
    current_price: float,
    trailing_peak: Optional[float] = None,
    profit_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """분할 익절 / 트레일링 스탑 로직을 생성한다.
    - 1차 익절: +3.5%~+5.0%에서 일부 물량 실현
    - 2차 익절: +8% 이상이면 추세 유지
    - 트레일링 스탑: 최고점의 -2.5% ~ -3.5%로 손절
    """
    if entry_price <= 0:
        return {"action": "HOLD", "exit_pct": 0.0, "trailing_stop": None}

    pr = profit_rate if profit_rate is not None else ((current_price - entry_price) / entry_price) * 100.0
    peak = trailing_peak if trailing_peak is not None else current_price

    if pr >= 8.0:
        return {
            "action": "PARTIAL_EXIT",
            "exit_pct": 0.35,
            "exit_reason": "고점 분할익절",
            "trailing_stop": max(peak * 0.965, entry_price * 1.02),
            "profit_rate": pr,
        }

    if pr >= 3.5:
        return {
            "action": "PARTIAL_EXIT",
            "exit_pct": 0.25,
            "exit_reason": "초기 익절",
            "trailing_stop": max(peak * 0.965, entry_price * 1.025),
            "profit_rate": pr,
        }

    if pr <= -1.8:
        return {
            "action": "FULL_EXIT",
            "exit_pct": 1.0,
            "exit_reason": "칼손절",
            "trailing_stop": None,
            "profit_rate": pr,
        }

    return {
        "action": "HOLD",
        "exit_pct": 0.0,
        "exit_reason": "추세 유지",
        "trailing_stop": max(peak * 0.965, entry_price * 1.02),
        "profit_rate": pr,
    }


def build_trailing_stop_rules(current_price: float, entry_price: float, peak_price: float) -> Dict[str, Any]:
    """이동평균/EMA+trailing stop 기반의 보유 전략 규칙 생성."""
    current_profit = ((current_price - entry_price) / entry_price) * 100.0
    peak_profit = ((peak_price - entry_price) / entry_price) * 100.0

    trailing_stop = peak_price * 0.965
    keep_if = current_price > trailing_stop and current_profit >= 2.0

    return {
        "current_profit_pct": round(current_profit, 2),
        "peak_profit_pct": round(peak_profit, 2),
        "trailing_stop": round(float(trailing_stop), 4),
        "keep_if": bool(keep_if),
    }
