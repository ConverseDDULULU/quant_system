import csv
from datetime import datetime
import os
from typing import Any, Dict, List

from market_calendar import now_ny

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
os.makedirs(DATA_DIR, exist_ok=True)

TRADE_CSV = os.path.join(DATA_DIR, "trade_history.csv")
DAILY_CSV = os.path.join(DATA_DIR, "daily_summary.csv")

# 키움 미국주식 수수료(한쪽, 매수·매도 각각). 실제 값은 장마감 후 ust21630으로 덮어쓴다.
# 10/30 손절 알림(키움 pl_rt)과 가격 기준 수익률 차이가 약 0.5%p라 한쪽 0.25%로 추정
FEE_RATE_PER_SIDE = float(os.getenv("KIWOOM_US_FEE_RATE", "0.0025"))

# profit_usd·profit_pct는 수수료 전(가격 기준), net_*은 수수료·제세금 차감 후
# fee_source: estimate(요율 추정) | kiwoom(ust21630 실제 값)
TRADE_FIELDS = [
    "timestamp", "date", "ticker", "buy_price", "sell_price", "qty",
    "profit_usd", "profit_pct", "exit_reason",
    "fee_usd", "net_profit_usd", "net_profit_pct", "fee_source",
]


def estimate_fee_usd(buy_price: float, sell_price: float, qty: int) -> float:
    return (float(buy_price) + float(sell_price)) * int(qty) * FEE_RATE_PER_SIDE


def _with_net(row: Dict[str, Any], fee_usd: float, source: str) -> Dict[str, Any]:
    buy_amt = float(row["buy_price"]) * int(float(row["qty"]))
    net = float(row["profit_usd"]) - fee_usd
    row["fee_usd"] = round(fee_usd, 4)
    row["net_profit_usd"] = round(net, 4)
    row["net_profit_pct"] = round(net / buy_amt * 100, 2) if buy_amt > 0 else 0.0
    row["fee_source"] = source
    return row


def _read_trade_rows() -> List[Dict[str, Any]]:
    if not os.path.exists(TRADE_CSV):
        return []
    with open(TRADE_CSV, mode="r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _write_trade_rows(rows: List[Dict[str, Any]]) -> None:
    with open(TRADE_CSV, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _migrate_trade_csv() -> None:
    """수수료 컬럼이 없던 예전 기록에 컬럼을 추가하고 추정 수수료를 채운다 (1회)"""
    if not os.path.exists(TRADE_CSV):
        return
    with open(TRADE_CSV, mode="r", newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), [])
    if header == TRADE_FIELDS:
        return
    rows = _read_trade_rows()
    for row in rows:
        if not row.get("fee_source"):
            try:
                fee = estimate_fee_usd(row["buy_price"], row["sell_price"], int(float(row["qty"])))
                _with_net(row, fee, "estimate")
            except (KeyError, TypeError, ValueError):
                pass
    _write_trade_rows(rows)
    print(f"🧾 [매매 기록] trade_history.csv에 수수료 컬럼을 추가했습니다 ({len(rows)}행)")


def load_trades_for_date(date_str: str) -> List[Dict[str, Any]]:
    """뉴욕 거래일 기준 매매 기록 (재시작으로 메모리 기록이 사라져도 파일 기준으로 센다)"""
    trades = []
    for row in _read_trade_rows():
        if row.get("date") != date_str or str(row.get("ticker", "")).upper().startswith("TEST"):
            continue
        parsed = dict(row)
        for key in ("profit_usd", "profit_pct", "fee_usd", "net_profit_usd", "net_profit_pct"):
            try:
                parsed[key] = float(row.get(key) or 0.0)
            except ValueError:
                parsed[key] = 0.0
        trades.append(parsed)
    return trades


def apply_kiwoom_realized(date_str: str, realized_rows: List[Dict[str, Any]]) -> int:
    """키움 당일 실현손익(ust21630)의 종목별 수수료·제세금으로 추정 수수료를 덮어쓴다.

    ust21630은 종목별 합계라, 같은 종목을 하루에 여러 번 팔았으면 매수금액 비율로 나눈다.
    """
    fees_by_ticker = {}
    for r in realized_rows or []:
        ticker = str(r.get("stk_cd", "")).strip().upper()
        try:
            fee = abs(float(str(r.get("cmsn") or 0).replace(",", ""))) + abs(
                float(str(r.get("altx") or 0).replace(",", ""))
            )
        except ValueError:
            continue
        if ticker:
            fees_by_ticker[ticker] = fees_by_ticker.get(ticker, 0.0) + fee
    if not fees_by_ticker:
        return 0

    _migrate_trade_csv()
    rows = _read_trade_rows()
    updated = 0
    for ticker, total_fee in fees_by_ticker.items():
        targets = [
            r for r in rows
            if r.get("date") == date_str and str(r.get("ticker", "")).upper() == ticker
        ]
        weights = [float(r["buy_price"]) * float(r["qty"]) for r in targets]
        total_weight = sum(weights)
        for row, weight in zip(targets, weights):
            share = weight / total_weight if total_weight > 0 else 1 / len(targets)
            estimated = float(row.get("fee_usd") or 0.0)
            _with_net(row, total_fee * share, "kiwoom")
            print(f"🧾 [수수료 확인] {ticker}: 추정 ${estimated:.4f} → 키움 ${float(row['fee_usd']):.4f}")
            updated += 1
    if updated:
        _write_trade_rows(rows)
    return updated


def deduplicate_daily_summaries() -> int:
    if not os.path.exists(DAILY_CSV):
        return 0

    with open(DAILY_CSV, mode="r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    latest_by_date = {}
    for row in rows:
        date = row.get("date", "")
        if date:
            latest_by_date[date] = row

    deduplicated_rows = list(latest_by_date.values())
    removed_count = len(rows) - len(deduplicated_rows)
    if removed_count <= 0:
        return 0

    with open(DAILY_CSV, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(deduplicated_rows)

    print(f"🧹 [일일 결산] 중복 날짜 행 {removed_count}개를 정리했습니다.")
    return removed_count


def record_trade(
    ticker: str,
    buy_price: float,
    sell_price: float,
    qty: int,
    exit_reason: str,
    extra_info: Dict[str, Any] = None,
):
    """개별 종목 매매 완료 시 1행씩 누적 기록 (수수료는 추정치, 장마감 후 키움 값으로 갱신)"""
    _migrate_trade_csv()
    file_exists = os.path.exists(TRADE_CSV)
    profit_amt = (sell_price - buy_price) * qty
    profit_pct = (
        ((sell_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0.0
    )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    row = {
        "timestamp": now_str,
        "date": now_ny().date().isoformat(),  # 뉴욕 거래일 (서버 시간대와 무관)
        "ticker": ticker,
        "buy_price": round(buy_price, 4),
        "sell_price": round(sell_price, 4),
        "qty": qty,
        "profit_usd": round(profit_amt, 4),
        "profit_pct": round(profit_pct, 2),
        "exit_reason": exit_reason,
    }
    _with_net(row, estimate_fee_usd(buy_price, sell_price, qty), "estimate")

    with open(TRADE_CSV, mode="a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    print(
        f"📁 [데이터 로깅 완료] {ticker} 매매 결과가 '{TRADE_CSV}'에 저장되었습니다."
    )


def finalize_daily_summary(
    start_balance: float,
    end_balance: float,
    completed_trades: List[Dict[str, Any]],
    discord_sender=None,
    kiwoom_client=None,
):
    """장 마감 시 계좌 총수익률 및 일일 결산 리포트 생성 & 저장

    거래 집계는 trade_history.csv(뉴욕 거래일)를 기준으로 한다. completed_trades는 엔진
    메모리 기록이라 장중 재시작 시 앞선 거래가 빠지므로, 파일을 못 읽을 때만 쓴다.
    kiwoom_client를 주면 키움 당일 실현손익(ust21630)으로 수수료를 실제 값으로 갱신한다.
    """
    file_exists = os.path.exists(DAILY_CSV)
    deduplicate_daily_summaries()
    file_exists = os.path.exists(DAILY_CSV)
    date_str = now_ny().date().isoformat()  # 뉴욕 거래일

    if kiwoom_client is not None:
        try:
            apply_kiwoom_realized(date_str, kiwoom_client.get_us_realized_today())
        except Exception as e:
            print(f"⚠️ 키움 실현손익 수수료 반영 실패 (추정 수수료 유지): {e}")
    try:
        file_trades = load_trades_for_date(date_str)
    except Exception as e:
        print(f"⚠️ 매매 기록 파일 읽기 실패, 엔진 메모리 기록으로 집계: {e}")
        file_trades = None
    if file_trades is not None:
        if len(file_trades) != len(completed_trades):
            print(
                f"ℹ️ [일일 결산] 파일 기록 {len(file_trades)}건 / 엔진 메모리 {len(completed_trades)}건 "
                "→ 파일 기준으로 집계 (장중 재시작 등)"
            )
        completed_trades = file_trades

    net_profit = end_balance - start_balance
    daily_return = (
        (net_profit / start_balance) * 100 if start_balance > 0 else 0.0
    )

    total_trades = len(completed_trades)
    # 승패는 수수료 차감 후 기준 (net이 없으면 가격 기준)
    def _pct(t):
        return t.get("net_profit_pct", t.get("profit_pct", 0))

    wins = sum(1 for t in completed_trades if _pct(t) > 0)
    losses = sum(1 for t in completed_trades if _pct(t) <= 0)
    total_fee = sum(float(t.get("fee_usd", 0) or 0) for t in completed_trades)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

    # 1. 일일 요약 CSV 저장
    daily_row = {
        "date": date_str,
        "start_balance_usd": round(start_balance, 2),
        "end_balance_usd": round(end_balance, 2),
        "net_profit_usd": round(net_profit, 2),
        "daily_return_pct": round(daily_return, 2),
        "total_trades": total_trades,
        "win_count": wins,
        "loss_count": losses,
        "win_rate_pct": round(win_rate, 1),
    }

    if file_exists:
        with open(DAILY_CSV, mode="r", newline="", encoding="utf-8-sig") as f:
            existing_rows = list(csv.DictReader(f))

        today_rows = [row for row in existing_rows if row.get("date") == date_str]
        if today_rows:
            deduplicated_rows = [
                row for row in existing_rows if row.get("date") != date_str
            ]
            deduplicated_rows.append(daily_row)
            with open(DAILY_CSV, mode="w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=list(daily_row.keys()))
                writer.writeheader()
                writer.writerows(deduplicated_rows)

            print(f"ℹ️ [일일 결산] {date_str} 결산을 최신 계좌 평가액으로 갱신했습니다.")
            return daily_row

    with open(DAILY_CSV, mode="a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(daily_row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(daily_row)

    # 2. 콘솔 및 디스코드 리포트 문자열 빌드
    report_lines = [
        "==================================================",
        f"📊 [일일 퀀트 트레이딩 결산 리포트] {date_str}",
        "==================================================",
        f"💰 [계좌 자산 변동]",
        f"   - 장 시작 전: ${start_balance:,.2f}",
        f"   - 장 마감 후: ${end_balance:,.2f}",
        f"   - 일일 순손익: {'+' if net_profit >= 0 else ''}${net_profit:,.2f} ({'+' if daily_return >= 0 else ''}{daily_return:.2f}%)",
        f"\n📈 [당일 매매 성과]",
        f"   - 총 거래: {total_trades}건 ({wins}승 {losses}패 | 승률: {win_rate:.1f}%, 수수료 차감 후)",
        f"   - 수수료·제세금: ${total_fee:,.2f}",
    ]

    if completed_trades:
        report_lines.append("\n📋 [종목별 상세 내역]")
        for t in completed_trades:
            p_pct = _pct(t)
            p_usd = t.get("net_profit_usd", t.get("profit_usd", 0))
            sign = "+" if p_pct >= 0 else ""
            fee_note = f", 수수료 ${float(t.get('fee_usd', 0) or 0):.2f}" if "fee_usd" in t else ""
            report_lines.append(
                f"   • {t['ticker']}: {sign}{p_pct:.2f}% (${p_usd:+.2f}{fee_note}) | {t.get('exit_reason', '청산')}"
            )
    report_lines.append("==================================================")

    report_text = "\n".join(report_lines)
    print("\n" + report_text)

    # 3. 디스코드 전송
    if discord_sender:
        try:
            discord_sender(f"```text\n{report_text}\n```")
        except Exception as e:
            print(f"⚠️ 결산 리포트 디스코드 전송 실패: {e}")

    return daily_row