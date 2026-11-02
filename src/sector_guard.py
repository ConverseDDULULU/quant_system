"""같은 업종 동시 보유 제한

이미 보유 중이거나 오늘 산 종목과 업종(yfinance industry, 예: Gold, Airlines, Semiconductors)이
같은 종목은 신규 매수하지 않는다. 11/02 개장 직후 금광주 3종목(GFI·HMY·HYMC)을 한꺼번에 사서
금값 한 요인에 계좌 전체가 걸렸던 일을 막기 위함.

- 업종은 거의 바뀌지 않으므로 data/industry_cache.json에 영구 캐시 (매수 신호가 난 종목만 조회)
- 조회에 실패하면 막지 않고 로그만 남긴다 (일시적 조회 실패로 매매가 멈추지 않게, 다음 신호 때 다시 조회)
- ETF처럼 업종이 없는 종목은 비교하지 않는다
"""

import json
import os
import threading

import yfinance as yf

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CACHE_PATH = os.path.join(PROJECT_ROOT, "data", "industry_cache.json")

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp_path = CACHE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CACHE_PATH)


def get_industry(ticker: str) -> str | None:
    """업종 이름. 업종이 없는 종목(ETF 등)은 "", 조회 실패는 None (캐시하지 않음)"""
    ticker = ticker.upper().strip()
    with _lock:
        cache = _load()
        if ticker in cache:
            return cache[ticker]
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        print(f"⚠️ [업종 조회 실패] {ticker}: {type(e).__name__}")
        return None
    if not info.get("quoteType"):
        print(f"⚠️ [업종 조회 실패] {ticker}: 응답 없음")
        return None
    industry = str(info.get("industry") or "")
    with _lock:
        cache = _load()
        cache[ticker] = industry
        _save(cache)
    return industry


def find_same_industry(ticker: str, others) -> tuple[str, str] | None:
    """others(보유·오늘 매수 종목) 중 ticker와 업종이 같은 종목이 있으면 (그 종목, 업종)"""
    industry = get_industry(ticker)
    if not industry:
        if industry is None:
            print(f"   ⚠️ [업종 확인 불가] {ticker}: 업종 중복 검사 없이 진행")
        return None
    for other in others:
        other = other.upper().strip()
        if other == ticker.upper().strip():
            continue
        if get_industry(other) == industry:
            return other, industry
    return None
