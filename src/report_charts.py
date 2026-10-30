"""장 마감 리포트용 그래프 이미지 (막대·원형) 생성 및 디스코드 첨부 전송

한 장의 PNG에 6개 패널을 그린다:
  [오늘 종목별 수익률 막대] [최근 10거래일 일별 손익 막대] [시장 지표 등락률 막대]
  [청산 이유 비율 원형]     [승/패 비율 원형]             [요약 숫자]

daily_review.run_daily_review()에서 send_daily_chart(day, trades, market)를 호출한다.
그래프 실패가 리포트 전송을 막지 않도록 send_daily_chart는 예외를 밖으로 던지지 않는다.

수동 실행 (예시 데이터로 샘플 이미지 생성):
  python src/report_charts.py --sample
"""

import argparse
import os

import matplotlib

matplotlib.use("Agg")  # 서버(화면 없음)에서도 동작
import matplotlib.pyplot as plt
import pandas as pd
import requests
from matplotlib import font_manager

from trade_logger import DAILY_CSV

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CHART_DIR = os.path.join(PROJECT_ROOT, "data", "reports")
RECENT_DAYS = 10

UP_COLOR = "#d64545"  # 한국식: 상승 빨강
DOWN_COLOR = "#2f6fd6"  # 하락 파랑
PIE_COLORS = ["#f2a541", "#3fa57a", "#8e6cc9", "#7f8c8d", "#c9a227", "#5b9bd5"]
EXIT_REASON_COLORS = {"익절": "#d64545", "최대익절": "#a51d1d", "손절": "#2f6fd6"}
KOREAN_FONTS = ["NanumGothic", "Malgun Gothic", "AppleGothic", "Noto Sans CJK KR"]


def _set_korean_font() -> None:
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in KOREAN_FONTS:
        if name in installed:
            plt.rcParams["font.family"] = name
            break
    else:
        print("⚠️ 한글 글꼴이 없어 그래프 글자가 깨질 수 있습니다 (서버: sudo apt install fonts-nanum)")
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["text.parse_math"] = False  # "$" 금액 표기가 수식으로 해석되지 않게


def _usd(v: float) -> str:
    """부호 있는 달러 표기: +$1.94 / -$0.43"""
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}"


def _bar_colors(values) -> list:
    return [UP_COLOR if v > 0 else DOWN_COLOR for v in values]


def _empty(ax, title: str, message: str = "데이터 없음") -> None:
    ax.set_title(title)
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=12, color="gray")
    ax.axis("off")


def _label_bars(ax, bars, values, fmt: str) -> None:
    for bar, v in zip(bars, values):
        ax.annotate(
            fmt.format(v),
            (bar.get_x() + bar.get_width() / 2, v),
            xytext=(0, 3 if v >= 0 else -11),
            textcoords="offset points",
            ha="center",
            fontsize=8,
        )


def _load_recent_daily(day, days: int = RECENT_DAYS) -> pd.DataFrame:
    try:
        df = pd.read_csv(DAILY_CSV, encoding="utf-8-sig")
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df[df["date"] <= day].drop_duplicates("date", keep="last")
    return df.sort_values("date").tail(days)


def _plot_today_trades(ax, today: pd.DataFrame) -> None:
    title = "오늘 종목별 수익률 (%)"
    if today.empty:
        _empty(ax, title, "오늘 청산 없음")
        return
    labels = [f"{t}\n{r}" for t, r in zip(today["ticker"], today["exit_reason"])]
    values = today["profit_pct"].astype(float).tolist()
    bars = ax.bar(labels, values, color=_bar_colors(values))
    _label_bars(ax, bars, values, "{:+.2f}%")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title(title)
    ax.tick_params(axis="x", labelsize=8)


def _plot_daily_pnl(ax, daily: pd.DataFrame) -> None:
    title = f"최근 {RECENT_DAYS}거래일 일별 손익 ($)"
    if daily.empty:
        _empty(ax, title)
        return
    labels = [d.strftime("%m/%d") for d in daily["date"]]
    values = daily["net_profit_usd"].astype(float).tolist()
    bars = ax.bar(labels, values, color=_bar_colors(values))
    _label_bars(ax, bars, values, "{:+.2f}")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title(title)
    ax.tick_params(axis="x", labelsize=8, rotation=45)


def _plot_market(ax, market: list) -> None:
    title = "시장 지표 등락률 (%)"
    if not market:
        _empty(ax, title)
        return
    labels = [m["name"] for m in market]
    values = [float(m["change_pct"]) for m in market]
    bars = ax.barh(labels, values, color=_bar_colors(values))
    for bar, v in zip(bars, values):
        ax.annotate(
            f"{v:+.2f}%",
            (v, bar.get_y() + bar.get_height() / 2),
            xytext=(3 if v >= 0 else -3, 0),
            textcoords="offset points",
            ha="left" if v >= 0 else "right",
            va="center",
            fontsize=8,
        )
    ax.axvline(0, color="black", linewidth=0.8)
    ax.invert_yaxis()
    ax.set_title(title)
    ax.margins(x=0.25)


def _reason_colors(reasons) -> list:
    """익절 계열은 빨강, 손절은 파랑, 나머지(장마감청산 등)는 중립색"""
    others = iter(PIE_COLORS)
    return [EXIT_REASON_COLORS.get(r) or next(others, "#7f8c8d") for r in reasons]


def _plot_pie(ax, counts: pd.Series, title: str, colors=None) -> None:
    counts = counts[counts > 0]
    if counts.empty:
        _empty(ax, title)
        return
    ax.pie(
        counts.values,
        labels=[f"{k}\n{v}건" for k, v in counts.items()],
        colors=colors or _reason_colors(counts.index),
        autopct="%1.0f%%",
        startangle=90,
        counterclock=False,
        wedgeprops={"edgecolor": "white"},
        textprops={"fontsize": 9},
    )
    ax.set_title(title)


def _plot_summary(ax, today: pd.DataFrame, window: pd.DataFrame, daily: pd.DataFrame, day) -> None:
    ax.axis("off")
    lines = [f"{day.isoformat()} (뉴욕 기준)", ""]
    if not daily.empty and daily["date"].iloc[-1] == day:
        row = daily.iloc[-1]
        lines.append(
            f"계좌 ${row['start_balance_usd']:,.2f} -> ${row['end_balance_usd']:,.2f} ({row['daily_return_pct']:+.2f}%)"
        )
    if today.empty:
        lines.append("오늘 청산 0건")
    else:
        lines.append(f"오늘 청산 {len(today)}건, 손익 {_usd(today['profit_usd'].sum())}")
    if not window.empty:
        wins = int((window["profit_pct"] > 0).sum())
        lines.append(f"최근 {RECENT_DAYS}거래일 {len(window)}건, 승률 {wins / len(window) * 100:.0f}%")
        lines.append(f"누적 손익 {_usd(window['profit_usd'].sum())}")
    ax.text(0.02, 0.95, "\n".join(lines), va="top", fontsize=11, linespacing=1.8)
    ax.set_title("요약")


def build_daily_chart(
    day, trades: pd.DataFrame, market: list, out_path: str | None = None, daily: pd.DataFrame | None = None
) -> str:
    """day: 뉴욕 거래일(date), trades: 최근 거래일 매매 기록(load_trades 결과), market: market_snapshot 결과
    daily를 생략하면 daily_summary.csv에서 최근 거래일 결산을 읽는다."""
    _set_korean_font()
    trades = trades if trades is not None else pd.DataFrame()
    today = trades[trades["date"] == day] if not trades.empty else trades
    daily = _load_recent_daily(day) if daily is None else daily

    fig, axes = plt.subplots(2, 3, figsize=(16, 9))
    fig.suptitle(f"일일 증시·매매 리뷰 ({day.isoformat()})", fontsize=16)
    _plot_today_trades(axes[0][0], today)
    _plot_daily_pnl(axes[0][1], daily)
    _plot_market(axes[0][2], market)
    if trades.empty:
        _empty(axes[1][0], f"청산 이유 비율 (최근 {RECENT_DAYS}거래일)")
        _empty(axes[1][1], f"승/패 비율 (최근 {RECENT_DAYS}거래일)")
    else:
        _plot_pie(axes[1][0], trades["exit_reason"].value_counts(), f"청산 이유 비율 (최근 {RECENT_DAYS}거래일)")
        win_loss = pd.Series(
            {"수익": int((trades["profit_pct"] > 0).sum()), "손실": int((trades["profit_pct"] <= 0).sum())}
        )
        _plot_pie(axes[1][1], win_loss, f"승/패 비율 (최근 {RECENT_DAYS}거래일)", colors=[UP_COLOR, DOWN_COLOR])
    _plot_summary(axes[1][2], today, trades, daily, day)

    fig.tight_layout(rect=(0, 0, 1, 0.95))
    if out_path is None:
        os.makedirs(CHART_DIR, exist_ok=True)
        out_path = os.path.join(CHART_DIR, f"daily_chart_{day.isoformat()}.png")
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


def send_discord_image(path: str, content: str = "", webhook_url: str | None = None) -> bool:
    url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL", "")
    if not url:
        return False
    with open(path, "rb") as f:
        res = requests.post(
            url,
            data={"content": content},
            files={"file": (os.path.basename(path), f, "image/png")},
            timeout=15,
        )
    if res.status_code >= 300:
        print(f"⚠️ 디스코드 그래프 전송 실패: HTTP {res.status_code}")
        return False
    return True


def send_daily_chart(day, trades: pd.DataFrame, market: list, webhook_url: str | None = None) -> str | None:
    """그래프를 만들어 디스코드에 첨부한다. 실패해도 예외를 던지지 않는다."""
    try:
        path = build_daily_chart(day, trades, market)
        send_discord_image(path, f"📊 일일 리뷰 그래프 ({day.isoformat()})", webhook_url)
        return path
    except Exception as e:
        print(f"⚠️ 일일 리뷰 그래프 생성/전송 실패: {e}")
        return None


def _sample_data():
    from datetime import date

    day = date(2026, 10, 29)
    rows = [
        (day, "PLUG", -1.41, -0.10, "손절"),
        (day, "SOUN", 3.62, 0.79, "익절"),
        (day, "RIOT", 1.88, 1.25, "장마감청산"),
        (date(2026, 10, 28), "CLOV", -1.80, -0.43, "손절"),
        (date(2026, 10, 28), "LCID", 4.10, 0.69, "탄력둔화익절"),
        (date(2026, 10, 27), "GRAB", 2.30, 0.58, "익절"),
    ]
    trades = pd.DataFrame(rows, columns=["date", "ticker", "profit_pct", "profit_usd", "exit_reason"])
    market = [
        {"name": "S&P500", "change_pct": 0.34},
        {"name": "나스닥100", "change_pct": 0.60},
        {"name": "반도체", "change_pct": 2.13},
        {"name": "VIX", "change_pct": -3.48},
        {"name": "미 10년물(%)", "change_pct": 1.04},
    ]
    pnl = [0.85, -1.20, 2.10, 0.00, -0.43, 1.55, 0.92, -0.60, 0.26, 1.94]
    dates = pd.bdate_range(end=day, periods=len(pnl)).date
    daily = pd.DataFrame(
        {
            "date": dates,
            "net_profit_usd": pnl,
            "start_balance_usd": [195.39] * len(pnl),
            "end_balance_usd": [195.39 + v for v in pnl],
            "daily_return_pct": [v / 195.39 * 100 for v in pnl],
        }
    )
    return day, trades, market, daily


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true", help="예시 데이터로 샘플 이미지 생성")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    if args.sample:
        day, trades, market, daily = _sample_data()
        print(build_daily_chart(day, trades, market, out_path=args.out, daily=daily))
