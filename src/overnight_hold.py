"""장마감 스마트 홀딩 판정

마감 청산 직전, 보유 종목별로 오버나잇 홀딩 여부를 결정한다.
홀딩 조건 (모두 충족):
  1. 수익률이 HOLD_MIN_PROFIT_PCT 이상 (손절선과 충분한 거리 확보)
  2. 당일 뉴스가 악재가 아님 (Gemini 판정)
  3. 상승 모멘텀 지속 또는 뉴스 호재
  4. 연속 홀딩 일수 < MAX_HOLD_DAYS, 홀딩 종목 수 < MAX_OVERNIGHT_HOLDS
판정은 하루 한 번만 내리고 저장해, 마감 구간 동안 매 사이클 같은 결과를 쓴다.
"""

import json
import os
import re
from datetime import timedelta
from typing import Callable

import yfinance as yf

from gemini_client import generate_text
from market_calendar import is_trading_day, now_ny

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HOLD_STATE_PATH = os.path.join(PROJECT_ROOT, "data", "overnight_holds.json")

HOLD_MIN_PROFIT_PCT = 1.0
MAX_OVERNIGHT_HOLDS = 2
MAX_HOLD_DAYS = 3
NEWS_VERDICTS = ("호재", "중립", "악재")


def _load_state() -> dict:
    try:
        with open(HOLD_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("holds", {})
    state.setdefault("decisions", {})
    return state


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(HOLD_STATE_PATH), exist_ok=True)
    # 최근 10거래일 판정만 보관
    for old_date in sorted(state["decisions"])[:-10]:
        del state["decisions"][old_date]
    tmp_path = HOLD_STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, HOLD_STATE_PATH)


def _previous_trading_day(day):
    day -= timedelta(days=1)
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day


def get_news_verdict(ticker: str) -> tuple[str, str]:
    """yfinance 헤드라인 + Gemini로 (호재|중립|악재, 근거) 판정. 실패 시 중립"""
    titles = []
    # yfinance 1.7의 Ticker.news가 빈 목록을 돌려줘서(2026-10-31 확인) Search 결과로 보충한다
    sources = (
        lambda: yf.Ticker(ticker).news,
        lambda: yf.Search(ticker, news_count=8).news,
    )
    for fetch in sources:
        try:
            for n in (fetch() or [])[:6]:
                title = n.get("title")
                if not title and isinstance(n.get("content"), dict):
                    title = n["content"].get("title")
                if title:
                    titles.append(f"- {title}")
        except Exception as e:
            print(f"⚠️ {ticker} 뉴스 수집 실패: {e}")
        if titles:
            break

    if not titles:
        return "중립", "최근 뉴스 없음"

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "중립", "GEMINI_API_KEY 없음"

    prompt = (
        f"미국 주식 {ticker}의 최신 뉴스 헤드라인입니다.\n"
        + "\n".join(titles)
        + "\n\n이 뉴스가 내일 주가에 호재인지 악재인지 판정하세요. "
        '반드시 JSON 한 줄로만 답하세요: {"verdict": "호재|중립|악재", "reason": "한국어 60자 이내 근거"}'
    )
    text = generate_text(prompt, temperature=0.1, timeout=20)
    if text is None:
        return "중립", "뉴스 판정 실패"
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {}
        verdict = parsed.get("verdict", "중립")
        if verdict not in NEWS_VERDICTS:
            verdict = "중립"
        return verdict, str(parsed.get("reason", ""))[:80]
    except Exception as e:
        print(f"⚠️ {ticker} Gemini 뉴스 판정 실패: {e}")
        return "중립", "뉴스 판정 실패"


def decide_hold(
    profit_rate: float,
    momentum_continues: bool,
    news_verdict: str,
    consecutive_days: int,
) -> tuple[bool, str]:
    """순수 판정 로직 (테스트·조건 조정용)"""
    if consecutive_days >= MAX_HOLD_DAYS:
        return False, f"연속 홀딩 {consecutive_days}일 한도 도달"
    if profit_rate < HOLD_MIN_PROFIT_PCT:
        return False, f"수익률 {profit_rate:+.2f}% < 홀딩 기준 +{HOLD_MIN_PROFIT_PCT}%"
    if news_verdict == "악재":
        return False, "뉴스 악재"
    if momentum_continues:
        return True, "상승 모멘텀 지속"
    if news_verdict == "호재":
        return True, "뉴스 호재"
    return False, "모멘텀 둔화, 뉴스 호재 없음"


def select_overnight_holds(
    positions: list[dict],
    momentum_fn: Callable[[str], bool],
) -> dict[str, dict]:
    """positions: [{"ticker", "profit_rate"}]. 홀딩할 종목 {ticker: 판정} 반환

    오늘 이미 판정했으면 저장된 결과를 그대로 쓴다.
    """
    state = _load_state()
    today = now_ny().date()
    today_key = today.isoformat()
    if today_key in state["decisions"]:
        return {
            t: d for t, d in state["decisions"][today_key].items() if d["hold"]
        }

    prev_key = _previous_trading_day(today).isoformat()
    candidates = []
    decisions = {}
    for pos in positions:
        ticker = pos["ticker"]
        prev = state["holds"].get(ticker)
        consecutive = prev["consecutive_days"] if prev and prev["last_hold_date"] == prev_key else 0

        momentum = False
        verdict, news_reason = "중립", ""
        # 수익률 기준 미달이면 외부 호출 없이 바로 청산
        if pos["profit_rate"] >= HOLD_MIN_PROFIT_PCT and consecutive < MAX_HOLD_DAYS:
            try:
                momentum = bool(momentum_fn(ticker))
            except Exception as e:
                print(f"⚠️ {ticker} 모멘텀 분석 실패: {e}")
            verdict, news_reason = get_news_verdict(ticker)

        hold, reason = decide_hold(pos["profit_rate"], momentum, verdict, consecutive)
        decisions[ticker] = {
            "hold": hold,
            "reason": reason,
            "profit_rate": pos["profit_rate"],
            "momentum": momentum,
            "news": verdict,
            "news_reason": news_reason,
            "consecutive_days": consecutive,
        }
        if hold:
            candidates.append(ticker)

    # 홀딩 종목 수 제한: 수익률 높은 순으로 남긴다
    candidates.sort(key=lambda t: decisions[t]["profit_rate"], reverse=True)
    for ticker in candidates[MAX_OVERNIGHT_HOLDS:]:
        decisions[ticker]["hold"] = False
        decisions[ticker]["reason"] = f"홀딩 종목 수 한도({MAX_OVERNIGHT_HOLDS}) 초과"

    for ticker, d in decisions.items():
        if d["hold"]:
            state["holds"][ticker] = {
                "last_hold_date": today_key,
                "consecutive_days": d["consecutive_days"] + 1,
            }
        else:
            state["holds"].pop(ticker, None)

    state["decisions"][today_key] = decisions
    _save_state(state)
    return {t: d for t, d in decisions.items() if d["hold"]}
