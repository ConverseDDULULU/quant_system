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
    """개별 종목 매매 완료 시 1행씩 누적 기록"""
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

    fieldnames = list(row.keys())

    with open(TRADE_CSV, mode="a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
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
):
    """장 마감 시 계좌 총수익률 및 일일 결산 리포트 생성 & 저장"""
    file_exists = os.path.exists(DAILY_CSV)
    deduplicate_daily_summaries()
    file_exists = os.path.exists(DAILY_CSV)
    date_str = now_ny().date().isoformat()  # 뉴욕 거래일

    net_profit = end_balance - start_balance
    daily_return = (
        (net_profit / start_balance) * 100 if start_balance > 0 else 0.0
    )

    total_trades = len(completed_trades)
    wins = sum(1 for t in completed_trades if t.get("profit_pct", 0) > 0)
    losses = sum(1 for t in completed_trades if t.get("profit_pct", 0) <= 0)
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
        f"   - 총 거래: {total_trades}건 ({wins}승 {losses}패 | 승률: {win_rate:.1f}%)",
    ]

    if completed_trades:
        report_lines.append("\n📋 [종목별 상세 내역]")
        for t in completed_trades:
            p_pct = t.get("profit_pct", 0)
            sign = "+" if p_pct >= 0 else ""
            report_lines.append(
                f"   • {t['ticker']}: {sign}{p_pct:.2f}% (${t.get('profit_usd', 0):+.2f}) | {t.get('exit_reason', '청산')}"
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