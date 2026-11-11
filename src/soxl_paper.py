"""SOXL·SOXS 단타 모의 매매 (실주문 없음)

사용자 결정(2026-11-07): "일단 SOXL은 모의로만". 실계좌 엔진(quant_scheduler)은 그대로 두고,
과거 시험에서 가장 나았던 규칙을 실제 장에서 모의로만 돌려 기록한다.

- 키움 모듈을 import하지 않는다. 가격은 yfinance 5분봉(정규장)만 쓴다. 그래서 주문이 나갈 길이 없다.
- 규칙 (시험 스크립트 lev_replay.py의 "T3 손절4.0 시간- 당일"과 같음, 5분봉 60일 시험: 68건, 이긴 비율 43%, 건당 -0.29%)
  · 판단: 기준 SOXX 5분봉 종가. 개장 15분 뒤 ~ 마감 90분 전
  · 상승 추세(EMA20 위, EMA20이 3봉 전보다 높음, VWAP 위)에서 직전 봉 저가가 EMA20에 닿고 이번 봉 양봉 → SOXL
  · 하락 추세(반대)에서 직전 봉 고가가 EMA20에 닿고 이번 봉 음봉 → SOXS
  · 매수: 다음 봉 시가. 한 번에 1종목, 하루 최대 2번 진입
  · 청산: 가격 +5% 익절, -4% 손절(같은 봉이면 손절 먼저), 마감 10분 전 봉 시가에 정리. 오버나잇 없음
  · 비용: 수수료 왕복 0.5% + 호가 0.05%
- 기록: data/soxl_paper/trades.jsonl (거래), data/soxl_paper/state.json (진행 상태)
- 알림: 진입·청산·장마감 요약을 디스코드에 "[모의]"로 보낸다 (DISCORD_WEBHOOK_URL)

실행:
  python src/soxl_paper.py            장중 1분마다 새 5분봉을 처리하는 반복 실행 (서비스용)
  python src/soxl_paper.py --once     한 번만 처리
  python src/soxl_paper.py --report   지금까지 모의 성적 출력
"""

import argparse
import json
import math
import os
import time as time_mod
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

from market_calendar import NY_TZ, is_trading_day, now_ny, session_bounds

warnings.filterwarnings("ignore", module="yfinance")  # 판다스 사용 중단 예고 경고가 1분마다 찍히는 것 방지

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(PROJECT_ROOT, "config", ".env"))

DATA_DIR = os.path.join(PROJECT_ROOT, "data", "soxl_paper")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
TRADES_PATH = os.path.join(DATA_DIR, "trades.jsonl")

BASE, LONG, INVERSE = "SOXX", "SOXL", "SOXS"
TAKE_PROFIT_PCT = 5.0
STOP_LOSS_PCT = 4.0
COST_PCT = 0.55
MAX_ENTRIES_PER_DAY = 2
ENTRY_START_MIN = 15  # 개장 뒤
ENTRY_END_MIN = 90  # 마감 전
EXIT_BEFORE_CLOSE_MIN = 10
BAR = timedelta(minutes=5)
BAR_SETTLE = timedelta(seconds=30)  # 봉이 끝나고 yfinance에 반영될 때까지 여유
PAPER_CAPITAL_USD = float(os.getenv("SOXL_PAPER_CAPITAL_USD", "200"))  # 수량 환산용 (종목당 1/2)


# ---------- 지표 ----------

def regular_only(df: pd.DataFrame) -> pd.DataFrame:
    """정규장 봉만 (조기 폐장일 포함, market_calendar 기준)"""
    if df.empty:
        return df
    keep = []
    for ts in df.index:
        bounds = session_bounds(ts.date())
        keep.append(bool(bounds) and bounds[0] <= ts < bounds[1])
    return df[np.array(keep, dtype=bool)]


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["Close"]
    out["ema20"] = close.ewm(span=20, adjust=False).mean()
    out["ema_up"] = out["ema20"] > out["ema20"].shift(3)
    day = out.index.date
    tpv = (out["High"] + out["Low"] + close) / 3 * out["Volume"]
    out["vwap"] = tpv.groupby(day).cumsum() / out["Volume"].groupby(day).cumsum().replace(0, np.nan)
    return out


def signal_direction(base: pd.DataFrame, i: int) -> int:
    """+1: SOXL, -1: SOXS, 0: 없음 (추세 속 눌림)"""
    row, prev = base.iloc[i], base.iloc[i - 1]
    up = row["Close"] > row["ema20"] and bool(row["ema_up"]) and row["Close"] > row["vwap"]
    down = row["Close"] < row["ema20"] and not bool(row["ema_up"]) and row["Close"] < row["vwap"]
    if up and prev["Low"] <= prev["ema20"] and row["Close"] > row["Open"]:
        return 1
    if down and prev["High"] >= prev["ema20"] and row["Close"] < row["Open"]:
        return -1
    return 0


# ---------- 상태 ----------

def new_state() -> dict:
    return {"last_bar": None, "day": None, "entries_today": 0, "position": None, "pending": None, "summary_sent": None}


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return {**new_state(), **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return new_state()


def save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def append_trade(trade: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TRADES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(trade, ensure_ascii=False) + "\n")


def read_trades() -> list[dict]:
    try:
        with open(TRADES_PATH, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


# ---------- 한 봉씩 처리 (과거 시험과 같은 순서) ----------

def process_bars(state: dict, base: pd.DataFrame, etfs: dict[str, pd.DataFrame], start: int, on_event) -> None:
    """base·etfs는 같은 시각 인덱스. start부터 끝까지 완성된 봉을 순서대로 처리한다"""
    for i in range(max(start, 1), len(base)):
        ts = base.index[i]
        day = ts.date()
        bounds = session_bounds(day)
        if not bounds:
            continue
        open_dt, close_dt = bounds
        if state["day"] != day.isoformat():
            if state["position"]:  # 마감 정리 봉이 비었던 날: 전날 마지막 봉 종가로 정리
                pos_t = state["position"]["ticker"]
                prev_ts = base.index[i - 1]
                _close_position(state, float(etfs[pos_t].iloc[i - 1]["Close"]), "마감(봉 누락)", prev_ts, on_event)
            state.update(day=day.isoformat(), entries_today=0, pending=None)
        state["last_bar"] = ts.isoformat()

        if state["pending"]:
            ticker = state["pending"]
            state["pending"] = None
            entry = float(etfs[ticker].iloc[i]["Open"])
            state["position"] = {"ticker": ticker, "entry": entry, "entry_at": ts.isoformat()}
            on_event("entry", {"ticker": ticker, "entry": entry, "at": ts.isoformat()})

        pos = state["position"]
        if pos:
            row = etfs[pos["ticker"]].iloc[i]
            entry = pos["entry"]
            stop_px = entry * (1 - STOP_LOSS_PCT / 100)
            tp_px = entry * (1 + TAKE_PROFIT_PCT / 100)
            if ts >= close_dt - timedelta(minutes=EXIT_BEFORE_CLOSE_MIN):
                _close_position(state, float(row["Open"]), "마감 정리", ts, on_event)
            elif row["Low"] <= stop_px:
                _close_position(state, stop_px, "손절", ts, on_event)
            elif row["High"] >= tp_px:
                _close_position(state, tp_px, "익절", ts, on_event)
            continue

        entry_start = open_dt + timedelta(minutes=ENTRY_START_MIN)
        entry_end = close_dt - timedelta(minutes=ENTRY_END_MIN)
        if entry_start <= ts <= entry_end and state["entries_today"] < MAX_ENTRIES_PER_DAY:
            direction = signal_direction(base, i)
            if direction:
                state["pending"] = LONG if direction > 0 else INVERSE
                state["entries_today"] += 1


def _close_position(state: dict, exit_px: float, reason: str, ts, on_event) -> None:
    pos = state["position"]
    net = (exit_px / pos["entry"] - 1) * 100 - COST_PCT
    qty = math.floor(PAPER_CAPITAL_USD / 2 / pos["entry"])
    trade = {
        "day": state["day"],
        "ticker": pos["ticker"],
        "entry_at": pos["entry_at"],
        "entry": round(pos["entry"], 4),
        "exit_at": ts.isoformat(),
        "exit": round(exit_px, 4),
        "reason": reason,
        "net_pct": round(net, 3),
        "paper_qty": qty,
        "paper_usd": round(qty * pos["entry"] * net / 100, 2),
    }
    state["position"] = None
    on_event("exit", trade)


# ---------- 실행 ----------

FETCH_RETRY_WAITS = (5, 10)  # yfinance가 잠깐 빈 결과를 줄 때 다시 시도하기 전 기다리는 초 (실패할 때만)


def _history(ticker: str, sleep=time_mod.sleep) -> pd.DataFrame | None:
    """5분봉 조회. 예외·빈 결과면 FETCH_RETRY_WAITS 간격으로 다시 시도 (11/10 SOXX 'possibly delisted' 3회)"""
    last_error = "빈 결과"
    for attempt, wait in enumerate((0,) + FETCH_RETRY_WAITS):
        if wait:
            sleep(wait)
        try:
            df = yf.Ticker(ticker).history(period="5d", interval="5m", prepost=False, auto_adjust=False)
        except Exception as e:
            df, last_error = None, f"{type(e).__name__}: {e}"
        if df is not None and not df.empty:
            if attempt:
                print(f"ℹ️ [SOXL 모의] {ticker} 분봉 {attempt}번 재시도 끝에 받음")
            return df
    print(f"⚠️ [SOXL 모의] {ticker} 분봉 없음 ({len(FETCH_RETRY_WAITS)}번 재시도 실패, {last_error}): 이번 처리 건너뜀, 다음 실행 때 밀린 봉부터 처리")
    return None


def fetch_frames(now: datetime) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]] | None:
    frames = {}
    for ticker in (BASE, LONG, INVERSE):
        df = _history(ticker)
        if df is None:
            return None
        df = df.tz_convert(NY_TZ)
        df = df[df.index + BAR + BAR_SETTLE <= now]  # 아직 안 끝난 봉 제외
        frames[ticker] = regular_only(df)
    idx = frames[BASE].index
    for ticker in (LONG, INVERSE):
        idx = idx.intersection(frames[ticker].index)
    base = indicators(frames[BASE].loc[idx])
    return base, {t: frames[t].loc[idx] for t in (LONG, INVERSE)}


def send_discord(content: str) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not url:
        return
    try:
        requests.post(url, json={"content": content}, timeout=5)
    except Exception as e:
        print(f"⚠️ [SOXL 모의] 디스코드 전송 실패: {type(e).__name__}")


def make_event_handler(notify, catch_up: bool):
    tag = " (재시작 뒤 따라잡기)" if catch_up else ""

    def on_event(kind: str, info: dict) -> None:
        if kind == "entry":
            msg = f"🧪 [모의] {info['ticker']} 매수 @ ${info['entry']:.2f} ({info['at'][11:16]} 뉴욕){tag}"
        else:
            append_trade(info)
            msg = (
                f"🧪 [모의] {info['ticker']} {info['reason']} @ ${info['exit']:.2f} | 순손익 {info['net_pct']:+.2f}% "
                f"({info['paper_qty']}주면 ${info['paper_usd']:+.2f}){tag}"
            )
        print(msg)
        if notify:
            notify(msg)

    return on_event


def run_once(now: datetime | None = None, notify=send_discord) -> None:
    now = now or now_ny()
    state = load_state()
    fetched = fetch_frames(now)
    if fetched is None:
        return
    base, etfs = fetched
    if base.empty:
        return
    today = now.date().isoformat()
    last = pd.Timestamp(state["last_bar"]) if state["last_bar"] else None
    if last is not None and last >= base.index[0]:
        # 받은 5일 봉 안에 마지막 처리 봉이 있으면 날짜와 상관없이 그다음 봉부터 이어서 처리
        # (전날 마감 전에 멈췄어도 전날 남은 봉에서 '마감 정리'로 닫혀 기록이 남는다)
        start = int(base.index.searchsorted(last, side="right"))
    else:
        # 처음 실행했거나 기록이 받은 봉보다 오래됐으면 오늘 첫 봉부터 처리
        if state["position"]:
            print("⚠️ [SOXL 모의] 마지막 처리 봉이 받은 데이터보다 오래돼 남은 포지션을 기록 없이 비웁니다")
            state["position"] = None
        state["pending"] = None
        todays = np.flatnonzero(np.array([ts.date().isoformat() == today for ts in base.index]))
        if not len(todays):
            return
        start = int(todays[0])
    catch_up = start < len(base) - 1
    process_bars(state, base, etfs, start, make_event_handler(notify, catch_up))
    save_state(state)


def daily_summary(day: str) -> str:
    trades = read_trades()
    today = [t for t in trades if t["day"] == day]
    lines = [f"🧪 **[SOXL 모의 매매] {day} 마감 요약** (실주문 없음)"]
    if today:
        for t in today:
            lines.append(f"• {t['ticker']} {t['reason']} {t['net_pct']:+.2f}% (${t['entry']:.2f}→${t['exit']:.2f})")
    else:
        lines.append("• 오늘 신호 없음")
    if trades:
        nets = [t["net_pct"] for t in trades]
        wins = sum(1 for n in nets if n > 0)
        lines.append(
            f"누적 {len(trades)}건 | 이긴 비율 {wins / len(trades) * 100:.0f}% | 건당 평균 {np.mean(nets):+.2f}% | "
            f"합계 {sum(nets):+.2f}% | 종목당 ${PAPER_CAPITAL_USD / 2:.0f} 기준 ${sum(t['paper_usd'] for t in trades):+.2f}"
        )
    return "\n".join(lines)


def loop() -> None:
    print("🧪 [SOXL 모의] 시작: 실주문 없음, yfinance 5분봉만 사용")
    while True:
        now = now_ny()
        bounds = session_bounds(now.date()) if is_trading_day(now.date()) else None
        if bounds and bounds[0] + BAR <= now <= bounds[1] + timedelta(minutes=10):
            try:
                run_once(now)
            except Exception as e:
                print(f"⚠️ [SOXL 모의] 처리 실패: {type(e).__name__}: {e}")
            time_mod.sleep(60)
            continue
        if bounds and now > bounds[1] + timedelta(minutes=10):
            state = load_state()
            today = now.date().isoformat()
            if state.get("summary_sent") != today and state.get("day") == today:
                send_discord(daily_summary(today))
                state["summary_sent"] = today
                save_state(state)
        time_mod.sleep(300)


def main() -> None:
    parser = argparse.ArgumentParser(description="SOXL·SOXS 단타 모의 매매 (실주문 없음)")
    parser.add_argument("--once", action="store_true", help="한 번만 처리")
    parser.add_argument("--report", action="store_true", help="모의 성적 출력")
    args = parser.parse_args()
    if args.report:
        print(daily_summary(now_ny().date().isoformat()))
    elif args.once:
        run_once(notify=None)
    else:
        loop()


if __name__ == "__main__":
    main()
