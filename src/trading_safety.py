import csv
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import pytz


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SAFETY_DB_PATH = os.path.join(PROJECT_ROOT, "data", "trading_safety.sqlite3")
RISK_SUMMARY_CSV_PATH = os.path.join(PROJECT_ROOT, "data", "trading_risk_summary.csv")
RISK_EVENTS_CSV_PATH = os.path.join(PROJECT_ROOT, "data", "trading_risk_events.csv")
MAX_DAILY_STOP_COUNT = 3
MAX_DAILY_LOSS_PCT = 0.015
MARKET_CACHE_SECONDS = 300
STOP_LOSS_PCT = -1.8
TAKE_PROFIT_PCT = 3.5
MAX_TAKE_PROFIT_PCT = 7.0
MAX_POSITION_FRACTION = 0.5  # 한 종목 매수 금액 상한: 계좌 평가액의 1/2
KNOWN_EXCHANGE_CODES = {"GRAB": "ND", "RIOT": "ND", "SOXL": "NA"}

_market_cache_lock = threading.Lock()
_market_cache_time = 0.0
_market_cache_result = (False, "아직 장세 조회 전")


def _trading_date() -> str:
    return datetime.now(pytz.timezone("America/New_York")).date().isoformat()


def get_known_exchange_code(ticker: str) -> str | None:
    return KNOWN_EXCHANGE_CODES.get(ticker.upper().strip())


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(SAFETY_DB_PATH), exist_ok=True)
    connection = sqlite3.connect(SAFETY_DB_PATH, timeout=10)
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_risk (
            trading_date TEXT PRIMARY KEY,
            starting_balance REAL NOT NULL DEFAULT 0,
            stop_count INTEGER NOT NULL DEFAULT 0,
            estimated_loss REAL NOT NULL DEFAULT 0,
            alert_sent INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS stop_events (
            trading_date TEXT NOT NULL,
            position_key TEXT NOT NULL,
            estimated_loss REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (trading_date, position_key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS position_exit_state (
            position_key TEXT PRIMARY KEY,
            take_profit_armed INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS account_baseline (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            baseline_equity_usd REAL NOT NULL,
            baseline_date TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_ticker_activity (
            trading_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            action TEXT NOT NULL,
            PRIMARY KEY (trading_date, ticker)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_rotation (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cursor INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return connection


@contextmanager
def _connection():
    connection = _connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_daily_row(connection: sqlite3.Connection, starting_balance: float) -> None:
    today = _trading_date()
    connection.execute(
        "INSERT OR IGNORE INTO daily_risk (trading_date, starting_balance) VALUES (?, ?)",
        (today, max(0.0, float(starting_balance))),
    )
    row = connection.execute(
        "SELECT starting_balance FROM daily_risk WHERE trading_date = ?", (today,)
    ).fetchone()
    if row and row[0] <= 0 and starting_balance > 0:
        connection.execute(
            "UPDATE daily_risk SET starting_balance = ? WHERE trading_date = ?",
            (float(starting_balance), today),
        )


def portfolio_equity_usd(cash_usd: float, holdings: list) -> float:
    total = max(0.0, float(cash_usd or 0.0))
    for holding in holdings or []:
        if not isinstance(holding, dict):
            continue
        try:
            quantity = float(
                str(
                    holding.get("poss_qty")
                    or holding.get("rmnd_qty")
                    or holding.get("qty")
                    or 0
                ).replace(",", "")
            )
            price = float(
                str(
                    holding.get("now_pric")
                    or holding.get("cur_prc")
                    or holding.get("now_price")
                    or holding.get("cur_price")
                    or 0
                ).replace(",", "")
            )
        except (TypeError, ValueError):
            continue
        if quantity > 0 and price > 0:
            total += quantity * price
    return round(total, 2)


def account_equity_usd(client, fallback_cash_usd: float, holdings: list) -> float:
    """계좌 평가액 = 외화예수금(ust21120) + 보유 평가액. 조회 실패 시 주문가능금액으로 대체"""
    cash = client.get_us_cash_usd() if hasattr(client, "get_us_cash_usd") else None
    return portfolio_equity_usd(fallback_cash_usd if cash is None else cash, holdings)


def kiwoom_minute_rows_to_frame(rows: list) -> pd.DataFrame:
    columns = {
        "open_pric": "Open",
        "high_pric": "High",
        "low_pric": "Low",
        "cur_prc": "Close",
        "trde_qty": "Volume",
    }
    frame = pd.DataFrame(rows or [])
    if frame.empty or not set(columns).issubset(frame.columns):
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    frame = frame.rename(columns=columns)
    for column in ("Open", "High", "Low", "Close", "Volume"):
        values = frame[column].astype(str).str.replace(",", "", regex=False)
        values = values.str.replace("+", "", regex=False)
        frame[column] = pd.to_numeric(values, errors="coerce")

    if {"bus_dt", "cntr_tm"}.issubset(frame.columns):
        frame["_sort_time"] = frame["bus_dt"].astype(str) + frame["cntr_tm"].astype(str)
        frame = frame.sort_values("_sort_time").drop(columns="_sort_time")
    return frame.dropna(
        subset=["Open", "High", "Low", "Close", "Volume"]
    ).reset_index(drop=True)


def get_rotating_us_candidates(
    client, max_price: float = 50.0, limit: int = 12
) -> list[dict]:
    # 후보 원천: 전체 거래소 거래량 상위(usa20530, 1페이지) + 상승률 상위(usa20910, 거래대금 100만$ 이상, 3페이지)
    # 상승률 1페이지는 +10% 넘는 폭등주라 0.5~15% 필터를 거의 못 통과해 3페이지(약 +4%대)까지 본다
    ranked_rows = []
    sources = (
        ("거래량 상위", lambda: client.get_us_today_volume_top(exchange="0", max_pages=1)),
        ("상승률 상위", lambda: client.get_us_top_gainers(exchange="0", max_pages=3)),
    )
    for label, fetch in sources:
        try:
            ranked_rows.extend(fetch())
        except Exception as exc:
            print(f"⚠️ 키움 {label} 조회 실패: {exc}")

    def _num(value) -> float:
        return float(str(value if value is not None else "0").replace(",", "").replace("+", ""))

    unique = {}
    for row in ranked_rows:
        ticker = str(row.get("stk_cd", "")).strip().upper()
        exchange = str(row.get("stex_tp", "")).strip().upper()
        # 매수 허용 거래소: NASDAQ·NYSE, AMEX는 SOXL만 (kiwoom_client.order_us_stock과 같은 기준)
        if not ticker or (exchange not in ("ND", "NY") and not (ticker == "SOXL" and exchange == "NA")):
            continue
        try:
            # cur_prc·거래량은 등락 부호가 붙어 올 수 있어 절댓값, 등락률은 부호 유지
            price = abs(_num(row.get("cur_prc")))
            volume = int(abs(_num(row.get("acc_trde_qty") or row.get("trde_qty"))))
            change_pct = _num(row.get("flu_rt"))
        except (TypeError, ValueError):
            continue
        if not (1.0 <= price <= max_price and volume > 0 and 0.5 <= change_pct <= 15.0):
            continue
        if ticker in unique and unique[ticker]["volume"] >= volume:
            continue
        unique[ticker] = {
            "ticker": ticker,
            "exchange": exchange,
            "price": price,
            "volume": volume,
            "change_pct": change_pct,
        }

    candidates = sorted(
        unique.values(), key=lambda item: (item["volume"], item["change_pct"]), reverse=True
    )
    if not candidates or limit <= 0:
        return []

    with _connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO candidate_rotation (id, cursor) VALUES (1, 0)"
        )
        cursor = connection.execute(
            "SELECT cursor FROM candidate_rotation WHERE id = 1"
        ).fetchone()[0]
        start = int(cursor) % len(candidates)
        rotated = candidates[start:] + candidates[:start]
        selected = rotated[: min(limit, len(rotated))]
        connection.execute(
            "UPDATE candidate_rotation SET cursor = ? WHERE id = 1",
            ((start + len(selected)) % len(candidates),),
        )

    return selected


def get_or_create_account_baseline(current_equity_usd: float) -> dict:
    with _connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO account_baseline "
            "(id, baseline_equity_usd, baseline_date) VALUES (1, ?, ?)",
            (max(0.0, float(current_equity_usd)), _trading_date()),
        )
        row = connection.execute(
            "SELECT baseline_equity_usd, baseline_date "
            "FROM account_baseline WHERE id = 1"
        ).fetchone()

    return {"equity_usd": row[0], "date": row[1]}


def has_ticker_activity_today(ticker: str) -> bool:
    with _connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM daily_ticker_activity "
            "WHERE trading_date = ? AND ticker = ?",
            (_trading_date(), ticker.upper().strip()),
        ).fetchone()
    return row is not None


def reserve_ticker_buy(ticker: str) -> bool:
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "INSERT OR IGNORE INTO daily_ticker_activity "
            "(trading_date, ticker, action) VALUES (?, ?, 'BUY_PENDING')",
            (_trading_date(), ticker.upper().strip()),
        )
    return cursor.rowcount == 1


def finish_ticker_buy(ticker: str, accepted: bool) -> None:
    with _connection() as connection:
        if accepted:
            connection.execute(
                "UPDATE daily_ticker_activity SET action = 'BUY' "
                "WHERE trading_date = ? AND ticker = ? AND action = 'BUY_PENDING'",
                (_trading_date(), ticker.upper().strip()),
            )
        else:
            connection.execute(
                "DELETE FROM daily_ticker_activity "
                "WHERE trading_date = ? AND ticker = ? AND action = 'BUY_PENDING'",
                (_trading_date(), ticker.upper().strip()),
            )


def record_ticker_sell(ticker: str) -> None:
    with _connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO daily_ticker_activity "
            "(trading_date, ticker, action) VALUES (?, ?, 'SELL')",
            (_trading_date(), ticker.upper().strip()),
        )


def is_momentum_continuing(
    price: float,
    vwap: float,
    short_ma: float,
    long_ma: float,
    obv: float,
    obv_ma: float,
    candle_green: bool,
) -> bool:
    return bool(
        candle_green
        and price >= vwap
        and short_ma > long_ma
        and obv > obv_ma
    )


def arm_take_profit(position_key: str) -> None:
    with _connection() as connection:
        connection.execute(
            "INSERT INTO position_exit_state (position_key, take_profit_armed) "
            "VALUES (?, 1) ON CONFLICT(position_key) DO UPDATE SET take_profit_armed = 1",
            (position_key,),
        )


def is_take_profit_armed(position_key: str) -> bool:
    with _connection() as connection:
        row = connection.execute(
            "SELECT take_profit_armed FROM position_exit_state WHERE position_key = ?",
            (position_key,),
        ).fetchone()
    return bool(row and row[0])


def clear_position_exit_state(position_key: str) -> None:
    with _connection() as connection:
        connection.execute(
            "DELETE FROM position_exit_state WHERE position_key = ?",
            (position_key,),
        )


def position_budget_usd(account_equity: float, cash_available: float) -> float:
    """한 종목에 쓸 수 있는 최대 금액: 현금과 계좌 평가액 x MAX_POSITION_FRACTION 중 작은 값"""
    cash = max(0.0, float(cash_available or 0.0))
    equity = float(account_equity or 0.0)
    if equity <= 0:
        return cash
    return min(cash, equity * MAX_POSITION_FRACTION)


def max_position_qty(
    price: float, account_equity: float, cash_available: float, max_qty: int
) -> int:
    """한 종목 매수 수량: 최대 수량과 한 종목 예산(position_budget_usd)으로 살 수 있는 수량 중 작은 값"""
    if price <= 0:
        return 0
    budget = position_budget_usd(account_equity, cash_available)
    return max(0, min(int(max_qty), int(budget / price)))


def get_exit_rule(
    profit_pct: float,
    momentum_continues: bool = False,
    take_profit_armed: bool = False,
) -> str | None:
    if profit_pct <= STOP_LOSS_PCT:
        return "STOP_LOSS"
    if profit_pct >= MAX_TAKE_PROFIT_PCT:
        return "MAX_TAKE_PROFIT"
    if (profit_pct >= TAKE_PROFIT_PCT or take_profit_armed) and not momentum_continues:
        return "TAKE_PROFIT"
    return None


def export_safety_csvs() -> tuple[str, str]:
    os.makedirs(os.path.dirname(SAFETY_DB_PATH), exist_ok=True)
    with _connection() as connection:
        daily_rows = connection.execute(
            "SELECT trading_date, starting_balance, stop_count, estimated_loss, alert_sent "
            "FROM daily_risk ORDER BY trading_date"
        ).fetchall()
        event_rows = connection.execute(
            "SELECT trading_date, position_key, estimated_loss "
            "FROM stop_events ORDER BY trading_date, position_key"
        ).fetchall()

    daily_fields = [
        "trading_date",
        "starting_balance_usd",
        "stop_count",
        "estimated_loss_usd",
        "alert_sent",
    ]
    event_fields = ["trading_date", "position_key", "estimated_loss_usd"]
    for path, fields, rows in (
        (RISK_SUMMARY_CSV_PATH, daily_fields, daily_rows),
        (RISK_EVENTS_CSV_PATH, event_fields, event_rows),
    ):
        with open(path, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(fields)
            writer.writerows(rows)

    return RISK_SUMMARY_CSV_PATH, RISK_EVENTS_CSV_PATH


def get_circuit_breaker_status(starting_balance: float) -> dict:
    with _connection() as connection:
        today = _trading_date()
        existed = connection.execute(
            "SELECT 1 FROM daily_risk WHERE trading_date = ?", (today,)
        ).fetchone() is not None
        previous_balance = connection.execute(
            "SELECT starting_balance FROM daily_risk WHERE trading_date = ?",
            (today,),
        ).fetchone()
        _ensure_daily_row(connection, starting_balance)
        current_balance = connection.execute(
            "SELECT starting_balance FROM daily_risk WHERE trading_date = ?",
            (today,),
        ).fetchone()
        baseline_updated = (
            previous_balance is not None
            and current_balance is not None
            and previous_balance[0] != current_balance[0]
        )
        row = connection.execute(
            "SELECT starting_balance, stop_count, estimated_loss, alert_sent "
            "FROM daily_risk WHERE trading_date = ?",
            (today,),
        ).fetchone()

        balance, stop_count, estimated_loss, alert_sent = row
        loss_limit = balance * MAX_DAILY_LOSS_PCT
        reasons = []
        if stop_count >= MAX_DAILY_STOP_COUNT:
            reasons.append(f"손절 신호 {stop_count}회")
        if loss_limit > 0 and estimated_loss >= loss_limit:
            reasons.append(
                f"추정 손실 ${estimated_loss:.2f} / 한도 ${loss_limit:.2f}"
            )

        blocked = bool(reasons)
        newly_blocked = blocked and not bool(alert_sent)
        if newly_blocked:
            connection.execute(
                "UPDATE daily_risk SET alert_sent = 1 WHERE trading_date = ?",
                (today,),
            )

        status = {
            "allowed": not blocked,
            "blocked": blocked,
            "newly_blocked": newly_blocked,
            "reason": ", ".join(reasons),
            "stop_count": stop_count,
            "estimated_loss": estimated_loss,
            "loss_limit": loss_limit,
            "starting_balance": balance,
        }

    if (
        not existed
        or baseline_updated
        or newly_blocked
        or not os.path.exists(RISK_SUMMARY_CSV_PATH)
        or not os.path.exists(RISK_EVENTS_CSV_PATH)
    ):
        export_safety_csvs()
    return status


def record_stop_loss(
    position_key: str, estimated_loss: float, starting_balance: float
) -> dict:
    today = _trading_date()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_daily_row(connection, starting_balance)
        cursor = connection.execute(
            "INSERT OR IGNORE INTO stop_events "
            "(trading_date, position_key, estimated_loss) VALUES (?, ?, ?)",
            (today, position_key, max(0.0, float(estimated_loss))),
        )
        if cursor.rowcount:
            connection.execute(
                "UPDATE daily_risk SET stop_count = stop_count + 1, "
                "estimated_loss = estimated_loss + ? WHERE trading_date = ?",
                (max(0.0, float(estimated_loss)), today),
            )

    export_safety_csvs()
    return get_circuit_breaker_status(starting_balance)


def is_aggressive_entry_mode() -> bool:
    """.env ENTRY_MODE=aggressive 이면 진입 조건 완화 (기본 normal)"""
    return os.getenv("ENTRY_MODE", "normal").strip().lower() == "aggressive"


def market_trend_allows_entries(client) -> tuple[bool, str]:
    global _market_cache_time, _market_cache_result

    now = time.monotonic()
    with _market_cache_lock:
        if now - _market_cache_time < MARKET_CACHE_SECONDS:
            return _market_cache_result

        try:
            rows = client.get_us_stock_minute_chart(
                "QQQ", exchange="ND", interval="5", max_pages=1
            )
            candles = kiwoom_minute_rows_to_frame(rows)
            close = candles["Close"].dropna()
            completed = close.iloc[:-1]
            if len(completed) < 24:
                result = (False, "QQQ 5분봉 데이터 부족")
            else:
                ema20 = completed.ewm(span=20, adjust=False).mean()
                above = completed.iloc[-1] > ema20.iloc[-1]
                rising = ema20.iloc[-1] > ema20.iloc[-4]
                if is_aggressive_entry_mode():
                    # 공격 모드: 지수가 EMA20 2% 이상 아래로 무너진 급락장만 차단
                    trend_ok = completed.iloc[-1] > ema20.iloc[-1] * 0.98
                    reason = "[공격 모드] QQQ 급락장 아님" if trend_ok else "[공격 모드] QQQ EMA20 -2% 이탈 급락장"
                else:
                    trend_ok = above and rising
                    reason = "QQQ EMA20 위·상승" if trend_ok else "QQQ EMA20 아래 또는 하락"
                result = (bool(trend_ok), reason)
        except Exception as exc:
            result = (False, f"QQQ 장세 조회 실패: {exc}")

        _market_cache_time = now
        _market_cache_result = result
        return result