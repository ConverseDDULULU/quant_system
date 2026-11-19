"""우량·가치주 보유 전략 (스위치: 서버 config/.env의 VALUE_HOLD=on, 기본 꺼짐)

사용자 결정 (2026-11-19, 2번 세션에서 직접):
  · "단타에서 전략을 바꿔보자. 괜찮은 종목은 홀딩하면서 익절 목표 손절률 없이, 급등주 매매 안 하고"
  · "우량주나 가치주를 사서 홀딩하라고. 단타를 치든 홀딩을 하든 괜찮으니 종목 구애받지 말고"
  · 큰 하락: "알림만, 자동 매도 없음" / 기존 단타: "급등주 단타는 끄고 SOXL은 유지" / 기준: "우량+가치 둘 다"

동작
  · 고르기: 하루 한 번 정해진 기준(아래 상수)으로 미국 큰 회사를 걸러 점수순으로 저장 (특정 종목을 미리 정하지 않음)
      우량: ROE·영업이익률이 높고, 빚(부채비율)이 적고, 잉여현금흐름이 플러스
      가치: 이익에 비해 주가가 비싸지 않음 (PER·선행 PER 상한)
  · 사기: 정규장 10:00~15:00, 하루 1종목까지. 점수 높은 순으로, 최근 20일 평균보다 싸게 내려온(눌린) 날에 산다
      일반 보유 칸(max_holdings)과 한 종목 예산(일반 몫 기준 1/2)은 엔진 것을 그대로 쓴다. 같은 업종은 겹치지 않게
  · 들고 있기: 익절·손절·장마감 청산·오버나잇 판단 없음. 엔진이 자동으로 팔지 않는다 (파는 건 사용자가 직접)
      매입가 대비 -10%·-20%·-30%마다 한 번씩, 기준에서 빠지면 한 번 디스코드 알림
  · 스위치가 켜지면 급등주 단타·하락장 인버스 진입은 하지 않는다. SOXL·SOXS 단타는 따로 그대로
    SOXL·SOXS가 아닌 보유 종목은 모두 이 전략의 보유로 본다 (상태 파일이 없어져도 자동 손절이 다시 붙지 않게)
이 전략의 매수 기준은 과거 재무 자료가 없어 사전 시험을 하지 못했다 (지금 재무로 과거를 고르면 결과가 부풀려짐).
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf
from yfinance import EquityQuery as Q

from market_calendar import now_ny, session_bounds

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "value_hold")
SCREEN_PATH = os.path.join(DATA_DIR, "screen.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
BRANCH = "value"
SOXL_TICKERS = ("SOXL", "SOXS")

# 고르는 기준 (바꿀 땐 사용자에게 먼저 확인)
MIN_MARKET_CAP = 10_000_000_000  # 시가총액 $100억 이상
MIN_PRICE = 5.0
MAX_TRAILING_PE = 25.0
MAX_FORWARD_PE = 20.0
MIN_ROE = 0.15  # 자기자본이익률 15% 이상
MIN_OPERATING_MARGIN = 0.10  # 영업이익률 10% 이상
MAX_DEBT_TO_EQUITY = 150.0  # yfinance 단위(%): 부채가 자기자본의 1.5배 이하
SCREEN_SIZE = 150
MAX_CANDIDATES = 40
MAX_BETA = 1.3  # 시장보다 1.3배 넘게 출렁이는 종목은 뺀다 (우량주는 덜 흔들려야)
# 원자재 가격 따라 이익이 크게 출렁이는 업종은 PER이 낮아 보여도 이익 정점일 수 있어 뺀다 (금광·정유·석유 등)
EXCLUDE_SECTORS = ("Basic Materials", "Energy")
EXCLUDE_NAME_WORDS = (" L.P.", " LP", "Partners")  # 미국 PTP(합자회사)는 외국인 매도 대금 10% 원천징수 문제가 있어 뺀다

# 사는 기준
ENTRY_START = (10, 0)
ENTRY_END = (15, 0)
PULLBACK_SMA_DAYS = 20  # 현재가가 20일 평균 이하일 때만 (오른 날 쫓아 사지 않기)
MAX_BUYS_PER_DAY = 1
BUY_LIMIT_PREMIUM = 1.002
ALERT_LEVELS = (-10.0, -20.0, -30.0)

_screen_lock = threading.Lock()
_screen_thread = None


def is_enabled() -> bool:
    return os.getenv("VALUE_HOLD", "off").strip().lower() == "on"


def manages(ticker: str) -> bool:
    """이 전략이 들고 있는 종목인지: 스위치가 켜져 있으면 SOXL·SOXS가 아닌 보유 종목 전부"""
    return is_enabled() and str(ticker).upper().strip() not in SOXL_TICKERS


# ---------- 상태 파일 ----------

def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_state(path: str | None = None) -> dict:
    state = _read_json(path or STATE_PATH, {})
    state.setdefault("positions", {})
    state.setdefault("buys", {})  # 날짜 → 그날 산 종목 목록
    return state


def save_state(state: dict, path: str | None = None) -> None:
    _write_json(path or STATE_PATH, state)


# ---------- 고르기 ----------

def passes(info: dict) -> tuple[bool, str]:
    """한 종목 재무가 기준을 통과하는지와 이유"""
    def num(key):
        v = info.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    name = str(info.get("longName") or info.get("shortName") or "")
    checks = [
        (info.get("quoteType") == "EQUITY", "주식 아님"),
        (not any(w in name for w in EXCLUDE_NAME_WORDS), "합자회사(PTP)"),
        ((num("marketCap") or 0) >= MIN_MARKET_CAP, "시가총액 작음"),
        (info.get("sector") not in EXCLUDE_SECTORS, "원자재 업종"),
        (num("beta") is not None and num("beta") <= MAX_BETA, "변동성(베타) 큼 또는 자료 없음"),
        (0 < (num("trailingPE") or 0) <= MAX_TRAILING_PE, "PER 높음 또는 적자"),
        (0 < (num("forwardPE") or 0) <= MAX_FORWARD_PE, "선행 PER 높음"),
        ((num("returnOnEquity") or 0) >= MIN_ROE, "ROE 낮음"),
        ((num("operatingMargins") or 0) >= MIN_OPERATING_MARGIN, "영업이익률 낮음"),
        (num("debtToEquity") is not None and num("debtToEquity") <= MAX_DEBT_TO_EQUITY, "빚 많음 또는 자료 없음"),
        ((num("freeCashflow") or 0) > 0, "잉여현금흐름 마이너스"),
    ]
    for ok, why in checks:
        if not ok:
            return False, why
    return True, "통과"


def _screen_symbols() -> list[str]:
    query = Q("and", [
        Q("eq", ["region", "us"]),
        Q("is-in", ["exchange", "NMS", "NYQ"]),
        Q("gt", ["intradaymarketcap", MIN_MARKET_CAP]),
        Q("gt", ["intradayprice", MIN_PRICE]),
        Q("gt", ["returnonequity.lasttwelvemonths", MIN_ROE * 100]),
        Q("btwn", ["peratio.lasttwelvemonths", 0, MAX_TRAILING_PE]),
    ])
    result = yf.screen(query, size=SCREEN_SIZE, sortField="intradaymarketcap", sortAsc=False) or {}
    return [q["symbol"] for q in result.get("quotes", []) if q.get("symbol") and "-" not in q["symbol"] and "." not in q["symbol"]]


def score(rows: list[dict]) -> list[dict]:
    """우량(ROE·영업이익률)과 가치(선행 PER 낮음)를 순위로 더한 점수. 낮을수록 좋음 → 오름차순"""
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df["score"] = (
        df["roe"].rank(ascending=False) + df["operating_margin"].rank(ascending=False) + df["forward_pe"].rank(ascending=True)
    )
    df = df.sort_values("score")
    df["rank"] = range(1, len(df) + 1)
    return df.to_dict("records")


def daily_sma(ticker: str, now) -> float | None:
    """어제까지 완성된 일봉 PULLBACK_SMA_DAYS일 평균"""
    try:
        h = yf.Ticker(ticker).history(period="3mo", interval="1d", auto_adjust=False)
        closes = h["Close"][[d.date() < now.date() for d in h.index]]
        return float(closes.iloc[-PULLBACK_SMA_DAYS:].mean()) if len(closes) >= PULLBACK_SMA_DAYS else None
    except Exception as e:
        print(f"⚠️ [우량·가치] {ticker} 일봉 조회 실패: {type(e).__name__}")
        return None


def build_screen(now=None, symbols_fn=_screen_symbols, info_fn=lambda t: yf.Ticker(t).info, sma_fn=daily_sma) -> dict:
    now = now or now_ny()
    rows, rejected = [], {}
    for sym in symbols_fn():
        try:
            info = info_fn(sym) or {}
        except Exception as e:
            rejected[sym] = f"자료 실패 {type(e).__name__}"
            continue
        ok, why = passes(info)
        if not ok:
            rejected[sym] = why
            continue
        rows.append({
            "ticker": sym,
            "name": str(info.get("longName") or "")[:40],
            "sector": info.get("sector") or "",
            "industry": info.get("industry") or "",
            "price": float(info.get("currentPrice") or info.get("regularMarketPrice") or 0),
            "trailing_pe": round(float(info["trailingPE"]), 1),
            "forward_pe": round(float(info["forwardPE"]), 1),
            "roe": round(float(info["returnOnEquity"]), 3),
            "operating_margin": round(float(info["operatingMargins"]), 3),
            "debt_to_equity": round(float(info["debtToEquity"]), 1),
        })
        time.sleep(0.2)
    ranked = score(rows)[:MAX_CANDIDATES]
    for row in ranked:  # 사기 판단용 20일 평균은 여기서 미리 (엔진 사이클에서 받지 않게)
        row["sma"] = sma_fn(row["ticker"], now)
    return {"date": now.date().isoformat(), "built_at": now.isoformat(), "candidates": ranked, "rejected_count": len(rejected)}


def load_screen(path: str | None = None) -> dict:
    return _read_json(path or SCREEN_PATH, {})


def refresh_screen_async(now=None, path: str | None = None) -> None:
    """오늘 고르기 결과가 없으면 뒤에서(스레드) 만든다. 엔진의 20초 보유 점검을 막지 않게"""
    global _screen_thread
    now = now or now_ny()
    path = path or SCREEN_PATH
    if load_screen(path).get("date") == now.date().isoformat():
        return
    with _screen_lock:
        if _screen_thread and _screen_thread.is_alive():
            return

        def work():
            try:
                screen = build_screen(now)
                if screen["candidates"]:
                    _write_json(path, screen)
                    print(f"🏦 [우량·가치 고르기] {len(screen['candidates'])}종목 통과: "
                          + ", ".join(c["ticker"] for c in screen["candidates"][:10]))
                else:
                    print("⚠️ [우량·가치 고르기] 통과 종목 없음 (자료 조회 실패일 수 있음), 다음 스캔에 다시")
            except Exception as e:
                print(f"⚠️ [우량·가치 고르기] 실패: {type(e).__name__}: {e}")

        _screen_thread = threading.Thread(target=work, daemon=True, name="value-screen")
        _screen_thread.start()


# ---------- 사기 ----------

def in_screen(ticker: str, now=None, path: str | None = None) -> bool | None:
    """오늘 고르기 결과에 있는지. 오늘 결과가 아직 없으면 None (알림 안 보냄)"""
    screen = load_screen(path)
    if screen.get("date") != (now or now_ny()).date().isoformat():
        return None
    return ticker.upper() in {c["ticker"] for c in screen.get("candidates", [])}


def in_entry_window(now) -> bool:
    bounds = session_bounds(now.date())
    if not bounds:
        return False
    start = now.replace(hour=ENTRY_START[0], minute=ENTRY_START[1], second=0, microsecond=0)
    end = now.replace(hour=ENTRY_END[0], minute=ENTRY_END[1], second=0, microsecond=0)
    return max(start, bounds[0]) <= now <= min(end, bounds[1] - timedelta(minutes=30))


def buys_today(state: dict, now=None) -> list[str]:
    return state["buys"].get((now or now_ny()).date().isoformat(), [])


def record_buy(ticker: str, qty: int, price: float, now=None, path: str | None = None) -> None:
    now = now or now_ny()
    state = load_state(path)
    day = now.date().isoformat()
    state["buys"] = {d: v for d, v in state["buys"].items() if d >= (now.date() - timedelta(days=30)).isoformat()}
    state["buys"].setdefault(day, []).append(ticker)
    pos = state["positions"].setdefault(ticker, {"first_bought": day, "alerts": [], "screen_alert": False})
    pos.update(last_bought=day, last_qty=qty, last_price=price)
    save_state(state, path)


# ---------- 들고 있기: 알림 ----------

def holding_alerts(ticker: str, profit_rate: float, in_screen: bool | None, path: str | None = None) -> list[str]:
    """새로 보낼 알림 문구 (같은 단계는 한 번만). 손익이 회복되면 그 단계 알림을 다시 켠다"""
    state = load_state(path)
    pos = state["positions"].setdefault(ticker, {"first_bought": None, "alerts": [], "screen_alert": False})
    before = json.loads(json.dumps(pos))
    messages = []
    for level in ALERT_LEVELS:
        if profit_rate <= level and level not in pos["alerts"]:
            pos["alerts"].append(level)
            messages.append(f"📉 **[우량·가치 보유 알림]** {ticker} 매입가 대비 {profit_rate:+.2f}% ({level:.0f}% 넘게 하락)\n"
                            f"- 자동으로 팔지 않아요. 팔지 들고 갈지는 직접 정해 주세요")
        elif profit_rate > level + 5 and level in pos["alerts"]:
            pos["alerts"].remove(level)
    if in_screen is False and not pos.get("screen_alert"):
        pos["screen_alert"] = True
        messages.append(f"🔎 **[우량·가치 보유 알림]** {ticker}가 오늘 고르기 기준에서 빠졌어요 (수익률 {profit_rate:+.2f}%)\n"
                        f"- 자동으로 팔지 않아요. 재무가 바뀌었는지 확인해 보세요")
    elif in_screen:
        pos["screen_alert"] = False
    if pos != before:
        save_state(state, path)
    return messages
