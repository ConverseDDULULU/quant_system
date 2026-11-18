"""SOXL·SOXS 1차 필터: 반도체·시장 뉴스(여론)로 호재/중립/악재를 판단 (사용자 요청 2026-11-15)

SOXL·SOXS는 실적보다 정치(선거·관세·수출 규제)·인물 발언(젠슨 황 등)·연준 같은 여론에 크게 흔들린다.
그래서 기술 신호(SOXX 5분봉) 전에 그날 뉴스 분위기를 먼저 본다.
  · 악재 → SOXL(상승 베팅) 진입 막음, SOXS는 허용
  · 호재 → SOXS(하락 베팅) 진입 막음, SOXL은 허용
  · 중립·판단 없음 → 기술 신호대로 (지금과 같음)
필터는 진입을 막기만 하고 새로 사게 하지 않는다. 손절·익절·서킷브레이커와 무관.

판단은 모의 프로그램(soxl_paper, 별도 서비스)이 장중 30분마다 해서 파일에 쌓는다.
엔진(soxl_live)은 파일만 읽으므로 Gemini 호출(최대 수십 초)이 20초 보유 점검을 늦추지 않는다.
스위치: config/.env의 SOXL_NEWS_FILTER = on(기본, 막음) / log(판단만 기록, 막지 않음) / off
"""

import json
import os
import re
import time
from datetime import datetime, timedelta

import yfinance as yf

from gemini_client import generate_text
from market_calendar import session_bounds

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VERDICTS_PATH = os.path.join(PROJECT_ROOT, "data", "soxl_paper", "news_verdicts.jsonl")

# 반도체 업종 + 시장 전체(정치·연준) 헤드라인
QUERIES = ("semiconductor stocks", "SOXX", "Nvidia", "chip export", "stock market", "Federal Reserve")
PER_QUERY = 8
MAX_HEADLINES = 30
LOOKBACK_HOURS = 24
REFRESH_MINUTES = 30
# 실패 뒤 다시 시도 간격: 연속 실패 횟수만큼 두 배씩 (5→10→20→40→60분). 11/17 Gemini 한도 초과(429) 때
# 5분마다 무한히 다시 불러 하루 76번이 됐다 (2026-11-18 1번 지적)
RETRY_MINUTES = 5
RETRY_MAX_MINUTES = 60
MAX_CALLS_PER_DAY = 16  # 하루(뉴욕 날짜) 판단 시도 상한. 정상이면 하루 약 12번
START_BEFORE_OPEN_MIN = 30  # 개장 30분 전부터
END_BEFORE_CLOSE_MIN = 90  # 마감 90분 전까지만 (그 뒤엔 SOXL 진입이 없어 판단할 필요 없음, soxl_paper.ENTRY_END_MIN과 같음)
MAX_AGE_MINUTES = 45  # 이보다 오래된 판단은 '판단 없음'으로 본다
VERDICTS = ("호재", "중립", "악재")


def mode() -> str:
    m = os.getenv("SOXL_NEWS_FILTER", "on").strip().lower()
    return m if m in ("on", "log", "off") else "on"


def fetch_headlines(now_ts: float | None = None, search=None) -> list[dict]:
    """최근 LOOKBACK_HOURS 안의 헤드라인 (중복 제거, 최신순)"""
    now_ts = now_ts or time.time()
    search = search or (lambda q: yf.Search(q, news_count=PER_QUERY).news)
    seen, out = set(), []
    for q in QUERIES:
        try:
            items = search(q) or []
        except Exception as e:
            print(f"⚠️ [SOXL 뉴스] '{q}' 검색 실패: {type(e).__name__}")
            continue
        for n in items:
            title = (n.get("title") or "").strip()
            published = n.get("providerPublishTime") or 0
            key = title.lower()
            if not title or key in seen or now_ts - published > LOOKBACK_HOURS * 3600:
                continue
            seen.add(key)
            out.append({"title": title, "publisher": n.get("publisher", ""), "published": published})
    out.sort(key=lambda h: h["published"], reverse=True)
    return out[:MAX_HEADLINES]


def judge(headlines: list[dict], generate=generate_text) -> dict:
    """{"verdict": 호재|중립|악재, "reason": str, "count": int}. 실패하면 verdict None (판단 없음)"""
    if not headlines:
        return {"verdict": None, "reason": "최근 24시간 뉴스 없음", "count": 0}
    lines = "\n".join(
        f"- [{datetime.utcfromtimestamp(h['published']).strftime('%m-%d %H:%M')} UTC] {h['title']}" for h in headlines
    )
    prompt = (
        "미국 반도체 업종과 시장 전체에 관한 최근 24시간 뉴스 헤드라인입니다.\n"
        f"{lines}\n\n"
        "이 뉴스들의 여론이 오늘 미국 반도체 주식(SOXX, 3배 레버리지 SOXL)에 호재인지 악재인지 판정하세요.\n"
        "실적 숫자보다 정치(선거·관세·수출 규제), 주요 인물 발언(젠슨 황 등), 연준·금리, 투자 심리를 더 중요하게 보세요.\n"
        "광고성·일반 투자 조언 기사는 무시하세요. 확실하지 않으면 중립입니다.\n"
        '반드시 JSON 한 줄로만 답하세요: {"verdict": "호재|중립|악재", "reason": "한국어 80자 이내 근거"}'
    )
    text = generate(prompt, temperature=0.1, timeout=20)
    if text is None:
        return {"verdict": None, "reason": "뉴스 판정 실패(Gemini)", "count": len(headlines)}
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {}
    except ValueError:
        parsed = {}
    verdict = parsed.get("verdict")
    if verdict not in VERDICTS:
        return {"verdict": None, "reason": "뉴스 판정 형식 오류", "count": len(headlines)}
    return {"verdict": verdict, "reason": str(parsed.get("reason", ""))[:100], "count": len(headlines)}


def read_verdicts(path: str | None = None) -> list[dict]:
    """깨진 줄(쓰다가 잘린 줄 등)은 건너뛴다. 파일 전체를 버리면 필터가 꺼지고 Gemini를 매번 부르게 된다"""
    out = []
    try:
        with open(path or VERDICTS_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    v = json.loads(line) if line.strip() else None
                    if isinstance(v, dict) and v.get("at"):
                        datetime.fromisoformat(v["at"])
                        out.append(v)
                except (ValueError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    return out


def latest_at(when: datetime, path: str | None = None, max_age_minutes: int = MAX_AGE_MINUTES) -> dict | None:
    """when 시점에 알 수 있었던 가장 최근 판단 (max_age 안, verdict가 있는 것만). 없으면 None"""
    best = None
    for v in read_verdicts(path):
        at = datetime.fromisoformat(v["at"])
        if at <= when and v.get("verdict") and (best is None or at > datetime.fromisoformat(best["at"])):
            best = v
    if best and when - datetime.fromisoformat(best["at"]) <= timedelta(minutes=max_age_minutes):
        return best
    return None


def blocks(ticker: str, verdict: dict | None) -> bool:
    """이 판단이 이 종목 진입을 막는가"""
    if not verdict:
        return False
    return (ticker == "SOXL" and verdict["verdict"] == "악재") or (ticker == "SOXS" and verdict["verdict"] == "호재")


def describe(verdict: dict | None) -> str:
    if not verdict:
        return "뉴스 판단 없음"
    return f"뉴스 {verdict['verdict']} ({datetime.fromisoformat(verdict['at']).strftime('%H:%M')}): {verdict['reason']}"


def _due(now: datetime, past: list[dict]) -> tuple[bool, str]:
    """지금 판단할 차례인지와 이유"""
    bounds = session_bounds(now.date())
    if not bounds:
        return False, "휴장일"
    open_dt, close_dt = bounds
    if not (open_dt - timedelta(minutes=START_BEFORE_OPEN_MIN) <= now <= close_dt - timedelta(minutes=END_BEFORE_CLOSE_MIN)):
        return False, "판단 시간 밖"
    today = [v for v in past if datetime.fromisoformat(v["at"]).date() == now.date()]
    if len(today) >= MAX_CALLS_PER_DAY:
        return False, f"하루 상한 {MAX_CALLS_PER_DAY}번"
    if not today:
        return True, "오늘 첫 판단"
    failures = 0
    for v in reversed(today):
        if v.get("verdict"):
            break
        failures += 1
    wait = REFRESH_MINUTES if not failures else min(RETRY_MINUTES * 2 ** (failures - 1), RETRY_MAX_MINUTES)
    if now - datetime.fromisoformat(today[-1]["at"]) < timedelta(minutes=wait):
        return False, f"{wait}분 대기"
    return True, f"연속 실패 {failures}번 뒤 재시도" if failures else "정기 판단"


def refresh_if_due(now: datetime, path: str | None = None, fetch=fetch_headlines, generate=generate_text) -> dict | None:
    """판단할 차례면(정규장 판단 시간 안, 하루 상한 안, 대기 시간 지남) 새로 판단해 파일에 붙인다 (모의 프로그램이 부른다)"""
    path = path or VERDICTS_PATH
    due, why = _due(now, read_verdicts(path))
    if not due:
        return None
    result = {"at": now.isoformat(), **judge(fetch(), generate)}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(f"📰 [SOXL 뉴스] {result['verdict'] or '판단 없음'} | 헤드라인 {result['count']}개 | {result['reason']} ({why})")
    return result
