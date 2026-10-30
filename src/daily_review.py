"""장 마감 후 일일 분석 → 매매 조건 자동 조정 → 증시 보고서 (디스코드)

흐름 (스케줄러가 장 마감 후 하루 1회 run_if_due 호출, 재시작해도 중복 실행 안 함):
  1. 최근 REVIEW_WINDOW_DAYS 거래일 매매 기록(trade_history.csv) 집계
  2. 손절·익절 종목의 당일 종가를 조회해 "손절 후 반등", "익절 후 추가 상승" 비율 계산
  3. 오버나잇 홀딩 판정(overnight_holds.json)과 다음 날 매도 결과를 맞춰 홀딩 효과 계산
  4. 규칙 기반으로 조건 조정안 계산 → strategy_params가 범위·1회 변경폭 제한 후 반영
  5. 지수·변동성 요약 + Gemini 해설을 붙여 디스코드 전송, data/reports/에 저장

조정 모드 (.env AUTO_TUNE_MODE):
  suggest (기본)  조정안을 보고서에만 싣고 실제 값은 바꾸지 않음 (검증 기간용)
  apply          조정안을 다음 거래일부터 반영
  off            조정안 계산 안 함
조정은 숫자 규칙으로만 결정한다. Gemini는 해설만 쓰고 값은 바꾸지 않는다.

수동 실행:
  python src/daily_review.py                  마지막 장 기준 분석 (디스코드 전송)
  python src/daily_review.py --date 2026-10-28 --no-send
"""

import argparse
import json
import os
import re
from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

import strategy_params
from gemini_client import generate_text
from market_calendar import is_trading_day, now_ny, session_bounds
from trade_logger import DAILY_CSV, TRADE_CSV

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(PROJECT_ROOT, "config", ".env"))

STATE_PATH = os.path.join(PROJECT_ROOT, "data", "daily_review_state.json")
REPORT_DIR = os.path.join(PROJECT_ROOT, "data", "reports")
HOLD_STATE_PATH = os.path.join(PROJECT_ROOT, "data", "overnight_holds.json")

REVIEW_WINDOW_DAYS = 10  # 조정 판단에 쓰는 최근 거래일 수
MIN_TRADES = 10  # 이보다 거래가 적으면 조정하지 않음 (표본 부족)
MIN_EXIT_SAMPLES = 5  # 손절/익절 규칙별 최소 표본
MIN_HOLD_SAMPLES = 3  # 홀딩 규칙 최소 표본
MARKET_SYMBOLS = {"SPY": "S&P500", "QQQ": "나스닥100", "SOXX": "반도체", "^VIX": "VIX", "^TNX": "미 10년물(%)"}
DISCORD_LIMIT = 1900


# ---------------------------------------------------------------- 날짜 / 상태

def last_completed_session(now: datetime | None = None):
    """마감까지 끝난 가장 최근 거래일"""
    now = now or now_ny()
    day = now.date()
    for _ in range(15):
        bounds = session_bounds(day)
        if bounds and now >= bounds[1]:
            return day
        day -= timedelta(days=1)
    return None


def _recent_trading_days(end_day, count: int) -> list:
    days, day = [], end_day
    while len(days) < count:
        if is_trading_day(day):
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 데이터 수집

def load_trades(start_day, end_day) -> pd.DataFrame:
    try:
        df = pd.read_csv(TRADE_CSV, encoding="utf-8-sig")
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()
    df = df[~df["ticker"].astype(str).str.upper().str.startswith("TEST")]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df[(df["date"] >= start_day) & (df["date"] <= end_day)].reset_index(drop=True)


def _daily_close(ticker: str, day) -> float | None:
    try:
        hist = yf.Ticker(ticker).history(start=day, end=day + timedelta(days=1), auto_adjust=False)
        return float(hist["Close"].iloc[-1]) if not hist.empty else None
    except Exception:
        return None


def market_snapshot(day) -> list[dict]:
    rows = []
    for symbol, name in MARKET_SYMBOLS.items():
        try:
            hist = yf.Ticker(symbol).history(start=day - timedelta(days=10), end=day + timedelta(days=1))
            hist = hist[hist.index.date <= day]
            if len(hist) < 2:
                continue
            close, prev = float(hist["Close"].iloc[-1]), float(hist["Close"].iloc[-2])
            rows.append({"symbol": symbol, "name": name, "close": close, "change_pct": (close / prev - 1) * 100})
        except Exception as e:
            print(f"⚠️ {symbol} 지수 조회 실패: {e}")
    return rows


# ---------------------------------------------------------------- 분석

def trade_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"count": 0}
    wins, losses = df[df["profit_pct"] > 0], df[df["profit_pct"] <= 0]
    gross_win, gross_loss = wins["profit_usd"].sum(), -losses["profit_usd"].sum()
    return {
        "count": int(len(df)),
        "win_rate": float(len(wins) / len(df) * 100),
        "avg_win_pct": float(wins["profit_pct"].mean()) if len(wins) else 0.0,
        "avg_loss_pct": float(losses["profit_pct"].mean()) if len(losses) else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else None,
        "total_usd": float(df["profit_usd"].sum()),
        "by_reason": {
            reason: {"count": int(len(g)), "avg_pct": float(g["profit_pct"].mean())}
            for reason, g in df.groupby("exit_reason")
        },
    }


def exit_followthrough(df: pd.DataFrame) -> dict:
    """손절 후 당일 종가가 매입가 위로 회복한 비율, 익절 후 종가가 매도가 +3% 이상 간 비율"""
    result = {}
    for reason, key, test in (
        ("손절", "stop_recovered", lambda r, c: c >= r["buy_price"]),
        ("익절", "tp_ran_further", lambda r, c: c >= r["sell_price"] * 1.03),
    ):
        rows = df[df["exit_reason"] == reason]
        checked = hits = 0
        for _, row in rows.iterrows():
            close = _daily_close(row["ticker"], row["date"])
            if close is None:
                continue
            checked += 1
            hits += bool(test(row, close))
        result[key] = {"checked": checked, "hits": hits, "ratio": hits / checked if checked else None}
    return result


def overnight_hold_outcomes(trades: pd.DataFrame) -> list[dict]:
    """홀딩 판정 시점 수익률 대비 이후 실제 매도 수익률 (양수면 홀딩이 이득)"""
    try:
        with open(HOLD_STATE_PATH, encoding="utf-8") as f:
            decisions = json.load(f).get("decisions", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    outcomes = []
    for day_key, per_ticker in decisions.items():
        hold_day = datetime.fromisoformat(day_key).date()
        for ticker, d in per_ticker.items():
            if not d.get("hold"):
                continue
            later = trades[(trades["ticker"] == ticker) & (trades["date"] > hold_day)] if not trades.empty else trades
            if later.empty:
                continue
            sold = later.sort_values("date").iloc[0]
            outcomes.append({
                "ticker": ticker,
                "hold_date": day_key,
                "hold_profit": float(d["profit_rate"]),
                "exit_profit": float(sold["profit_pct"]),
                "effect": float(sold["profit_pct"]) - float(d["profit_rate"]),
            })
    return outcomes


def propose_changes(stats: dict, follow: dict, holds: list[dict], params: dict) -> tuple[dict, list[str]]:
    """규칙 기반 조정안 {param: 목표값}과 근거 목록"""
    proposed, reasons = {}, []
    if stats["count"] < MIN_TRADES:
        reasons.append(f"최근 {REVIEW_WINDOW_DAYS}거래일 거래 {stats['count']}건 < {MIN_TRADES}건: 표본 부족으로 조정 보류")
        return proposed, reasons

    pf = stats["profit_factor"]
    if stats["win_rate"] < 35 and (pf is None or pf < 1.0):
        proposed["max_buy_qty"] = params["max_buy_qty"] - 1
        proposed["max_holdings"] = params["max_holdings"] - 1
        reasons.append(f"승률 {stats['win_rate']:.0f}%·손익비 {pf or 0:.2f}: 매수 수량·보유 종목 수 축소")
    elif stats["count"] >= 20 and stats["win_rate"] >= 55 and pf is not None and pf >= 1.5:
        proposed["max_buy_qty"] = params["max_buy_qty"] + 1
        reasons.append(f"승률 {stats['win_rate']:.0f}%·손익비 {pf:.2f} (20건 이상): 매수 수량 확대")

    stop = follow["stop_recovered"]
    if stop["checked"] >= MIN_EXIT_SAMPLES and stop["ratio"] >= 0.6:
        proposed["stop_loss_pct"] = params["stop_loss_pct"] - 0.2
        reasons.append(f"손절 {stop['checked']}건 중 {stop['hits']}건이 당일 매입가 회복: 손절폭 확대(노이즈 손절 감소)")
    elif stop["checked"] >= MIN_EXIT_SAMPLES and stop["ratio"] <= 0.2:
        proposed["stop_loss_pct"] = params["stop_loss_pct"] + 0.2
        reasons.append(f"손절 {stop['checked']}건 중 회복 {stop['hits']}건뿐: 손절폭 축소(손실 조기 차단)")

    tp = follow["tp_ran_further"]
    if tp["checked"] >= MIN_EXIT_SAMPLES and tp["ratio"] >= 0.5:
        proposed["take_profit_pct"] = params["take_profit_pct"] + 0.5
        proposed["max_take_profit_pct"] = params["max_take_profit_pct"] + 1.0
        reasons.append(f"익절 {tp['checked']}건 중 {tp['hits']}건이 이후 +3% 이상 추가 상승: 익절선 상향")

    if len(holds) >= MIN_HOLD_SAMPLES:
        avg_effect = sum(h["effect"] for h in holds) / len(holds)
        if avg_effect < -0.5:
            proposed["hold_min_profit_pct"] = params["hold_min_profit_pct"] + 0.5
            reasons.append(f"오버나잇 홀딩 {len(holds)}건 평균 효과 {avg_effect:+.2f}%p: 홀딩 기준 강화")
            if avg_effect < -1.5:
                proposed["max_overnight_holds"] = params["max_overnight_holds"] - 1
        elif avg_effect > 1.0:
            proposed["hold_min_profit_pct"] = params["hold_min_profit_pct"] - 0.5
            reasons.append(f"오버나잇 홀딩 {len(holds)}건 평균 효과 {avg_effect:+.2f}%p: 홀딩 기준 완화")

    if not reasons:
        reasons.append("조정 조건에 해당하는 패턴 없음: 현행 유지")
    return proposed, reasons


# ---------------------------------------------------------------- 보고서

def _gemini_commentary(payload: dict) -> str:
    prompt = (
        "당신은 미국 주식 단타 자동매매 시스템의 일일 리뷰어입니다. 아래 JSON은 오늘 시장 지표, "
        "최근 매매 통계, 자동 조정 결과입니다.\n"
        f"{json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        "한국어로 600자 이내로 작성하세요: (1) 오늘 시장 흐름 한두 문장 (2) 매매 성과에서 눈에 띄는 점 "
        "(3) 내일 주의할 점. 숫자는 주어진 데이터만 쓰고 새로 지어내지 마세요. 특정 종목 매수 권유는 하지 마세요."
    )
    return generate_text(prompt, temperature=0.3, timeout=60) or ""


def _day_summary_row(day) -> dict | None:
    try:
        df = pd.read_csv(DAILY_CSV, encoding="utf-8-sig")
        row = df[df["date"] == day.isoformat()]
        return row.iloc[-1].to_dict() if not row.empty else None
    except Exception:
        return None


def build_report(day, mode, market, today_stats, window_stats, follow, holds, reasons, diff, params, commentary) -> str:
    lines = [f"# 📰 일일 증시·매매 리뷰 ({day.isoformat()}, 뉴욕 기준)", ""]

    lines.append("## 시장")
    for m in market:
        lines.append(f"- {m['name']} ({m['symbol']}): {m['close']:,.2f} ({m['change_pct']:+.2f}%)")
    if not market:
        lines.append("- 지수 데이터 조회 실패")

    summary = _day_summary_row(day)
    lines += ["", "## 오늘 매매"]
    if summary:
        lines.append(
            f"- 계좌: ${summary['start_balance_usd']:,.2f} → ${summary['end_balance_usd']:,.2f} "
            f"({summary['daily_return_pct']:+.2f}%)"
        )
    if today_stats["count"]:
        lines.append(f"- 청산 {today_stats['count']}건, 승률 {today_stats['win_rate']:.0f}%, 손익 ${today_stats['total_usd']:+.2f}")
        for reason, r in today_stats["by_reason"].items():
            lines.append(f"  - {reason}: {r['count']}건, 평균 {r['avg_pct']:+.2f}%")
    else:
        lines.append("- 청산 기록 없음")

    lines += ["", f"## 최근 {REVIEW_WINDOW_DAYS}거래일"]
    if window_stats["count"]:
        pf = window_stats["profit_factor"]
        lines.append(
            f"- {window_stats['count']}건, 승률 {window_stats['win_rate']:.0f}%, "
            f"평균 수익 {window_stats['avg_win_pct']:+.2f}% / 평균 손실 {window_stats['avg_loss_pct']:+.2f}%, "
            f"손익비 {pf:.2f}" if pf is not None else
            f"- {window_stats['count']}건, 승률 {window_stats['win_rate']:.0f}%, 손실 거래 없음"
        )
    for key, label in (("stop_recovered", "손절 후 당일 회복"), ("tp_ran_further", "익절 후 +3% 추가 상승")):
        f = follow.get(key, {})
        if f.get("checked"):
            lines.append(f"- {label}: {f['hits']}/{f['checked']}건")
    if holds:
        avg = sum(h["effect"] for h in holds) / len(holds)
        lines.append(f"- 오버나잇 홀딩 {len(holds)}건, 평균 효과 {avg:+.2f}%p")

    mode_label = {"apply": "자동 반영", "suggest": "제안만 (AUTO_TUNE_MODE=suggest)", "off": "꺼짐"}[mode]
    lines += ["", f"## 조건 조정 ({mode_label})"]
    lines += [f"- {r}" for r in reasons]
    if diff:
        verb = "변경" if mode == "apply" else "제안"
        for name, (old, new) in diff.items():
            lines.append(f"- **{verb}** {strategy_params.PARAM_SPECS[name][4]}: {old} → {new}")
    lines.append(
        f"- 현재 적용값: 손절 {params['stop_loss_pct']}%, 익절 {params['take_profit_pct']}%, "
        f"최대익절 {params['max_take_profit_pct']}%, 수량 {params['max_buy_qty']}주, "
        f"보유 {params['max_holdings']}종목, 홀딩 기준 {params['hold_min_profit_pct']}%"
    )

    if commentary:
        lines += ["", "## AI 해설 (Gemini)", commentary]
    return "\n".join(lines)


def _send_chunks(text: str, sender) -> None:
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > DISCORD_LIMIT:
            sender(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        sender(chunk)


# ---------------------------------------------------------------- 실행

def run_daily_review(day, discord_sender=None) -> str:
    mode = os.getenv("AUTO_TUNE_MODE", "suggest").strip().lower()
    if mode not in ("apply", "suggest", "off"):
        mode = "suggest"

    window_days = _recent_trading_days(day, REVIEW_WINDOW_DAYS)
    trades = load_trades(window_days[0], day)
    today_trades = trades[trades["date"] == day] if not trades.empty else trades
    today_stats = trade_stats(today_trades)
    window_stats = trade_stats(trades)
    follow = exit_followthrough(trades) if not trades.empty else {}
    holds = overnight_hold_outcomes(load_trades(window_days[0] - timedelta(days=7), day))

    params = strategy_params.load_params()
    diff, reasons = {}, ["AUTO_TUNE_MODE=off: 조정 계산 안 함"]
    if mode != "off":
        if window_stats["count"]:
            proposed, reasons = propose_changes(window_stats, follow, holds, params)
        else:
            proposed, reasons = {}, ["최근 매매 기록 없음: 조정 보류"]
        if mode == "apply" and proposed:
            diff = strategy_params.update_params(proposed, reason=" / ".join(reasons))
            params = strategy_params.load_params()
        elif proposed:
            preview = strategy_params.clamp_changes(params, proposed)
            diff = {k: (params[k], v) for k, v in preview.items()}

    market = market_snapshot(day)
    commentary = _gemini_commentary(
        {"date": day, "market": market, "today": today_stats, "recent": window_stats, "tuning": reasons, "changes": diff}
    )
    report = build_report(day, mode, market, today_stats, window_stats, follow, holds, reasons, diff, params, commentary)

    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, f"daily_review_{day.isoformat()}.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    if discord_sender:
        _send_chunks(report, discord_sender)
        try:
            # 함수 안에서 import: matplotlib이 없어도 리뷰와 스케줄러(이 모듈을 import)는 계속 돈다
            import report_charts

            report_charts.send_daily_chart(day, trades, market)
        except Exception as e:
            print(f"⚠️ 일일 리뷰 그래프 생략: {e}")
    return report


def run_if_due(discord_sender=None) -> bool:
    """마지막으로 끝난 거래일의 리뷰를 아직 안 했으면 실행"""
    day = last_completed_session()
    if day is None:
        return False
    state = _load_state()
    if state.get("last_review_date") == day.isoformat():
        return False
    print(f"\n🧾 [일일 리뷰] {day} 장 분석·조건 조정·보고서 생성 시작")
    run_daily_review(day, discord_sender)
    state["last_review_date"] = day.isoformat()
    _save_state(state)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="일일 매매 분석·조건 조정·보고서")
    parser.add_argument("--date", help="분석할 뉴욕 거래일 (YYYY-MM-DD), 기본: 마지막 장")
    parser.add_argument("--no-send", action="store_true", help="디스코드 전송 안 함")
    args = parser.parse_args()

    target = datetime.fromisoformat(args.date).date() if args.date else last_completed_session()
    def send_discord(content: str) -> None:
        url = os.getenv("DISCORD_WEBHOOK_URL", "")
        if url:
            requests.post(url, json={"content": content}, timeout=5)

    run_daily_review(target, None if args.no_send else send_discord)
