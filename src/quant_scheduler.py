import os
import sys
import json
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
import warnings
import pytz
from market_calendar import market_phase, next_market_open, now_ny, seconds_until_next_open
from overnight_hold import select_overnight_holds
from trade_logger import finalize_daily_summary, record_trade
from trading_safety import (
    MAX_TAKE_PROFIT_PCT,
    TAKE_PROFIT_PCT,
    arm_take_profit,
    clear_position_exit_state,
    get_exit_rule,
    get_circuit_breaker_status,
    get_known_exchange_code,
    get_rotating_us_candidates,
    finish_ticker_buy,
    has_ticker_activity_today,
    is_momentum_continuing,
    is_take_profit_armed,
    market_trend_allows_entries,
    kiwoom_minute_rows_to_frame,
    portfolio_equity_usd,
    record_ticker_sell,
    record_stop_loss,
    reserve_ticker_buy,
)

# 당일 세션 데이터 추적용 전역 변수
INITIAL_BALANCE = None  # 장 시작 시 최초 1회 기록
DAILY_COMPLETED_TRADES = []  # 오늘 청산된 종목 누적 리스트
CURRENT_TRADING_DATE = None  # 뉴욕 기준 거래일 (바뀌면 당일 상태 초기화)
SUMMARY_SENT_DATE = None  # 일일 결산을 이미 보낸 거래일
HOLD_NOTICE_DATE = None  # 오버나잇 홀딩 판정 알림을 보낸 거래일

warnings.filterwarnings("ignore")
warnings.simplefilter(action="ignore", category=FutureWarning)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

load_dotenv(os.path.join(PROJECT_ROOT, "config", ".env"), override=True)

from src.kiwoom_client import KiwoomClient

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

MAX_BUY_QTY = 5


def send_discord_message(content: str):
    """디스코드 실시간 체결 및 알림 전송"""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        payload = {"content": content}
        headers = {"Content-Type": "application/json"}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, headers=headers, timeout=5)
    except Exception as e:
        print(f"⚠️ 디스코드 알림 전송 실패: {e}")


def format_us_price(price: float) -> float:
    """미국 주식 호가 단위(Tick Size) 보정: $1 이상 소수점 2자리, $1 미만 4자리"""
    if price >= 1.0:
        return float(f"{price:.2f}")
    if price > 0:
        return float(f"{price:.4f}")
    return 0.0


def get_pre_breakout_stocks(
    client: KiwoomClient, max_price: float = 100.0, limit: int = 15
):
    """키움 미국 거래량 상위 TR에서 가격 조건을 만족하는 후보를 순환 반환한다."""
    return get_rotating_us_candidates(client, max_price=max_price, limit=limit)


def get_bottom_reversal_signal(client: KiwoomClient, ticker: str, exchange: str):
    """
    반등 직전 저점 타점 포착 엔진:
    음봉 투매 후 첫 양봉 + RSI 36~54 바닥 탈출 + 20이평선 밀착 + OBV 유입 + 스토캐스틱 골든크로스
    """
    try:
        rows = client.get_us_stock_minute_chart(
            ticker, exchange=exchange, interval="1", max_pages=2
        )
        df = kiwoom_minute_rows_to_frame(rows)
        if df.empty or len(df) < 25:
            rows = client.get_us_stock_minute_chart(
                ticker, exchange=exchange, interval="5", max_pages=1
            )
            df = kiwoom_minute_rows_to_frame(rows)
            if df.empty or len(df) < 25:
                return None

        df = df.iloc[:-1]
        if len(df) < 25:
            return None

        closes = df["Close"]
        opens = df["Open"]
        volumes = df["Volume"]
        highs = df["High"]
        lows = df["Low"]

        cur_price = float(closes.iloc[-1])
        cur_open = float(opens.iloc[-1])
        prev_close = float(closes.iloc[-2])

        # 1. 이동평균선 & 이격도 (20EMA 대비 98.5% ~ 101.5% 밀착 구간)
        ma5 = float(closes.rolling(window=5).mean().iloc[-1])
        ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
        disparity_20 = (cur_price / (ema20 + 1e-9)) * 100
        is_tight_ma = 98.5 <= disparity_20 <= 101.5

        # 2. 캔들 반등 패턴 (직전봉 음봉/조정 -> 현재봉 양봉으로 돌려세움)
        is_reversal_candle = (cur_price > cur_open) and (cur_price >= prev_close)

        # 3. RSI 침체 탈출 구간 (36.0 ~ 54.0)
        delta = closes.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        rsi = float(100 - (100 / (1 + rs)).iloc[-1])
        is_rsi_bottom = 36.0 <= rsi <= 54.0

        # 4. 완료된 반등 봉 거래량이 직전 20봉 평균의 3~5배인지 확인
        cur_vol = float(volumes.iloc[-1])
        avg_vol = float(volumes.iloc[-21:-1].mean())
        vol_surge = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1.0
        is_volume_burst = 3.0 <= vol_surge <= 5.0

        # 5. VWAP 지지 여부
        typical_price = (highs + lows + closes) / 3
        vwap = float((typical_price * volumes).sum() / (volumes.sum() + 1e-9))
        vwap_supported = cur_price >= (vwap * 0.995)

        # 6. 볼린저 밴드 하단 지지 여부
        std20 = closes.rolling(20).std().iloc[-1]
        lower_band = float(ema20 - (std20 * 2))
        near_band_support = (cur_price <= (lower_band * 1.02)) or (
            cur_price >= lower_band
        )

        # 7. OBV 자금 유입 확인
        obv = (np.sign(delta) * volumes).fillna(0).cumsum()
        obv_ma = float(obv.rolling(window=5).mean().iloc[-1])
        is_obv_up = bool(obv.iloc[-1] > obv_ma)
        is_cur_green = cur_price >= cur_open
        momentum_continues = is_momentum_continuing(
            price=cur_price,
            vwap=vwap,
            short_ma=ma5,
            long_ma=ema20,
            obv=float(obv.iloc[-1]),
            obv_ma=obv_ma,
            candle_green=is_cur_green,
        )

        # 8. 스토캐스틱 슬로우 (%K, %D 계산)
        lowest_low = lows.rolling(window=14).min()
        highest_high = highs.rolling(window=14).max()
        fast_k = ((closes - lowest_low) / (highest_high - lowest_low + 1e-9)) * 100
        slow_k = fast_k.rolling(window=3).mean()
        slow_d = slow_k.rolling(window=3).mean()

        cur_slow_k = float(slow_k.iloc[-1])
        cur_slow_d = float(slow_d.iloc[-1])
        is_stoch_golden = (cur_slow_k > cur_slow_d) and (20.0 <= cur_slow_k <= 65.0)

        # 시그널 판정
        signal = "HOLD"
        if (
            is_reversal_candle
            and is_rsi_bottom
            and is_tight_ma
            and is_volume_burst
            and vwap_supported
            and near_band_support
            and is_obv_up
            and is_stoch_golden
        ):
            signal = "BUY"
        elif cur_price < (ema20 * 0.98):
            signal = "SELL"

        return {
            "signal": signal,
            "cur_price": cur_price,
            "rsi": rsi,
            "vol_surge": vol_surge,
            "disparity": disparity_20,
            "is_candle_up": is_reversal_candle,
            "is_cur_green": is_cur_green,
            "momentum_continues": momentum_continues,
            "slow_k": cur_slow_k,
            "slow_d": cur_slow_d,
            "obv_bullish": is_obv_up,
        }
    except Exception:
        return None
    
def get_exchange_code(client: KiwoomClient, ticker: str) -> str:
    """티커 심볼에 따라 키움 거래소 구분 코드(ND/NY/NA) 반환"""
    known_exchange = get_known_exchange_code(ticker)
    if known_exchange:
        return known_exchange

    try:
        exchange = client.get_us_exchange_code(ticker)
        if exchange in {"ND", "NY", "NA"}:
            return exchange
    except Exception:
        pass
    return "UNKNOWN"


def run_trading_cycle(client: KiwoomClient, dry_run: bool = False):
    """자동매매 1회 점검 사이클 (+3.5% 익절 / +7.0% 강제 익절 / -1.8% 손절)"""
    global INITIAL_BALANCE, DAILY_COMPLETED_TRADES, CURRENT_TRADING_DATE, SUMMARY_SENT_DATE
    trading_date = now_ny().date().isoformat()
    if CURRENT_TRADING_DATE != trading_date:
        # 24시간 상시 가동 시 전날 시작 잔고·청산 내역이 이월되지 않도록 초기화
        CURRENT_TRADING_DATE = trading_date
        INITIAL_BALANCE = None
        DAILY_COMPLETED_TRADES = []

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 65)
    print(
        f"⏰ [바닥 반등 선취매 가동] {now_str} (모드: {'가상' if dry_run else '실거래'})"
    )
    print("=" * 65)

    # 0단계: 장마감 10분 전 감지 시 일괄 청산 후 신규 진입 차단
    balance_res = client.get_balance()
    holdings = balance_res.get("result_list", [])

    if is_market_closing_time():
        if holdings:
            liquidate_all_positions(client, holdings)
            print(
                "💤 장마감 청산 주문 처리를 마쳤습니다. 실제 체결은 계좌 잔고에서 확인하세요."
            )
        else:
            print("💤 장마감 직전입니다. 포지션이 없으므로 대기합니다.")

        # 마감 구간 동안 매 사이클 청산은 재시도하되, 결산은 하루 한 번만 보낸다
        if SUMMARY_SENT_DATE == trading_date:
            return
        try:
            # 청산 후 USD 현금과 잔여 보유주식 평가액을 합산
            final_deposit = client.get_deposit()
            final_holdings = client.get_balance().get("result_list", [])
            final_balance = portfolio_equity_usd(
                final_deposit.get("usd_deposit", 0.0), final_holdings
            )
            if INITIAL_BALANCE is None:
                risk_state = get_circuit_breaker_status(final_balance)
                INITIAL_BALANCE = risk_state["starting_balance"] or final_balance

            # 일일 결산 리포트 생성, CSV 저장 및 디스코드 전송
            finalize_daily_summary(
                start_balance=INITIAL_BALANCE or final_balance,
                end_balance=final_balance,
                completed_trades=DAILY_COMPLETED_TRADES,
                discord_sender=send_discord_message,  # 스케줄러의 디스코드 전송 함수명
            )
            SUMMARY_SENT_DATE = trading_date
        except Exception as e:
            print(f"⚠️ 일일 결산 생성 중 오류: {e}")
        return


    deposit_info = client.get_deposit()
    usd_available = deposit_info.get("usd_deposit", 0.0)
    print(f"💰 현재 키움 계좌 주문가능 외화: ${usd_available:,.2f}")
    
   
    if INITIAL_BALANCE is None:
        current_equity = portfolio_equity_usd(usd_available, holdings)
        risk_state = get_circuit_breaker_status(current_equity)
        INITIAL_BALANCE = risk_state["starting_balance"] or current_equity
    # 1단계: 보유 종목 동적 익절/손절 감시
    held_tickers = []

    if holdings:
        print(f"📋 [보유 계좌 잔고 감시 ({len(holdings)}개 종목 추적 중)]")
        for item in holdings:
            ticker = item.get("stk_cd", "")
            qty = int(float(item.get("poss_qty", 0)))
            if qty <= 0:
                continue

            held_tickers.append(ticker)
            buy_price = float(item.get("frgn_stk_book_uv", 0.0))
            now_price = float(item.get("now_pric", 0.0))
            profit_rate = float(item.get("pl_rt", 0.0))

            print(
                f"   📊 [{ticker:4s}] 보유: {qty}주 | 매입단가: ${buy_price:.2f} | 현재가: ${now_price:.2f} | 수익률: {profit_rate:+.2f}%"
            )

            is_sell_triggered = False
            sell_reason = ""

            position_key = f"scheduler:{ticker}:{buy_price:.4f}:{qty}"
            take_profit_armed = (
                not dry_run and is_take_profit_armed(position_key)
            )
            if profit_rate >= TAKE_PROFIT_PCT:
                take_profit_armed = True
                if not dry_run:
                    arm_take_profit(position_key)

            momentum_continues = False
            if take_profit_armed and profit_rate < MAX_TAKE_PROFIT_PCT:
                exchange_code = get_exchange_code(client, ticker)
                analysis = get_bottom_reversal_signal(client, ticker, exchange_code)
                momentum_continues = bool(
                    analysis and analysis.get("momentum_continues", False)
                )

            exit_rule = get_exit_rule(
                profit_rate,
                momentum_continues=momentum_continues,
                take_profit_armed=take_profit_armed,
            )
            if exit_rule == "MAX_TAKE_PROFIT":
                is_sell_triggered = True
                sell_reason = f"🚀 **[최대 상한 익절]** {ticker} {qty}주 (+{profit_rate:.2f}%, 단가: ${now_price:.2f})"
            elif exit_rule == "TAKE_PROFIT":
                is_sell_triggered = True
                sell_reason = f"🎉 **[익절]** {ticker} {qty}주 (+{profit_rate:.2f}%, 단가: ${now_price:.2f})"
            elif exit_rule == "STOP_LOSS":
                is_sell_triggered = True
                sell_reason = f"🚨 **[손절]** {ticker} {qty}주 ({profit_rate:.2f}%, 단가: ${now_price:.2f})"

            if is_sell_triggered:
                print(f"   ➔ {sell_reason}")

                if not dry_run:
                    sell_target_price = format_us_price(now_price * 0.99)
                    res = client.order_us_stock(
                        ticker=ticker,
                        order_type="SELL",
                        qty=qty,
                        price=sell_target_price,
                        exchange=exchange_code,
                    )
                    return_code = res.get("return_code", -1)
                    return_msg = res.get("return_msg", "응답 없음")
                    print(
                        f"   ➔ 키움 실계좌 매도 접수 응답 ({exchange_code}): [{return_code}] {return_msg}"
                    )

                    if str(return_code) == "0":
                        clear_position_exit_state(position_key)
                        record_ticker_sell(ticker)
                        if exit_rule == "STOP_LOSS":
                            breaker = record_stop_loss(
                                position_key=f"scheduler:{ticker}:{buy_price:.4f}:{qty}",
                                estimated_loss=max(0.0, (buy_price - now_price) * qty),
                                starting_balance=INITIAL_BALANCE or usd_available,
                            )
                            if breaker["newly_blocked"]:
                                send_discord_message(
                                    f"🛑 [일일 서킷브레이커 발동] 신규 매수 중단: {breaker['reason']}"
                                )
                        send_discord_message(
                            f"✅ **[매도 주문 접수 성공]** {sell_reason}"
                        )
                    time.sleep(2)
    else:
        print("ℹ️ 현재 키움 계좌에 보유 중인 주식이 없습니다.")
        
        

    # 2단계: 급등 전 바닥 선취매 스캔
    breaker = get_circuit_breaker_status(usd_available)
    if breaker["newly_blocked"]:
        send_discord_message(
            f"🛑 [일일 서킷브레이커 발동] 신규 매수 중단: {breaker['reason']}"
        )
    if breaker["blocked"]:
        print(f"🛑 [일일 서킷브레이커] 신규 매수 차단: {breaker['reason']}")
        return

    market_ok, market_reason = market_trend_allows_entries(client)
    if not market_ok:
        print(f"📉 [시장 장세 필터] 신규 매수 차단: {market_reason}")
        return

    MAX_HOLDINGS = 3
    if len(held_tickers) >= MAX_HOLDINGS:
        print(
            f"\n🔒 [보유 슬롯 만석] 현재 {len(held_tickers)}/{MAX_HOLDINGS} 종목 보유 중으로 신규 매수를 대기합니다."
        )
        return

    # [안전장치 2] 1종목 몰빵 방지: 예수금이 많아도 1회 진입 단가는 최대 $50 이하로 제한
    max_entry_price = min(usd_available, 50.0)

    target_stocks = get_pre_breakout_stocks(
        client, max_price=max_entry_price, limit=12
    )
    print(
        f"\n🎯 [키움 수급 후보군 ({len(target_stocks)}개, 상한선: ${max_entry_price:.2f})]: "
        f"{', '.join(item['ticker'] for item in target_stocks)}"
    )

    signals_summary = []

    for candidate in target_stocks:
        ticker = candidate["ticker"]
        exchange_code = candidate["exchange"]
        if ticker in held_tickers:
            continue
        if has_ticker_activity_today(ticker):
            print(f"⏸️ [당일 재진입 차단] {ticker}: 오늘 이미 주문했습니다.")
            continue

        analysis = get_bottom_reversal_signal(client, ticker, exchange_code)
        if not analysis:
            continue

        sig = analysis["signal"]
        price = analysis["cur_price"]
        rsi_val = analysis["rsi"]
        vol_surge = analysis["vol_surge"]
        disparity = analysis["disparity"]
        k_val = analysis.get("slow_k", 0.0)
        d_val = analysis.get("slow_d", 0.0)
        obv_txt = "유입" if analysis.get("obv_bullish") else "이탈"

        print(
            f"📊 [{ticker:4s}] 현재가: ${price:6.2f} | 거래량: {vol_surge:4.1f}배 | 이격도: {disparity:5.1f}% | RSI: {rsi_val:4.1f} | Stoch: {k_val:.1f}/{d_val:.1f} | OBV: {obv_txt} | 판정: {sig}"
        )

        if sig == "BUY":
            is_soxl_amex = ticker == "SOXL" and exchange_code == "NA"
            if exchange_code != "ND" and not is_soxl_amex:
                print(
                    f"   ⏭️ [NASDAQ 전용] {ticker} 매수 차단 (거래소: {exchange_code})"
                )
                continue

            if not dry_run:
                buy_target_price = format_us_price(price)
                try:
                    numeric_price = float(buy_target_price)
                except (TypeError, ValueError):
                    numeric_price = 0.0

                if numeric_price <= 0:
                    print(
                        f"❌ [주문 차단] {ticker} 매수 단가 오류! (값: {buy_target_price}). 0원 주문을 방지하기 위해 주문을 스킵합니다."
                    )
                    continue

                buy_qty = min(MAX_BUY_QTY, int(usd_available / numeric_price))
                if buy_qty <= 0:
                    print(
                        f"   ⚠️ [주문 스킵] {ticker} 단가(${numeric_price:.2f})가 잔여 예수금(${usd_available:.2f})보다 큽니다."
                    )
                    continue

                if not reserve_ticker_buy(ticker):
                    print(f"   ⏸️ [당일 재진입 차단] {ticker}: 다른 엔진에서 이미 주문했습니다.")
                    continue

                res = client.order_us_stock(
                    ticker=ticker,
                    order_type="BUY",
                    qty=buy_qty,
                    price=numeric_price,
                    exchange=exchange_code,
                )
                return_code = res.get("return_code", -1)
                return_msg = res.get("return_msg", "응답 없음")
                print(
                    f"  ➔ 키움 실계좌 매수 접수 응답 ({exchange_code}): [{return_code}] {return_msg}"
                )
                if str(return_code) == "0":
                    finish_ticker_buy(ticker, accepted=True)
                    usd_available -= numeric_price * buy_qty
                    send_discord_message(
                        f"✅ **[매수 주문 접수 성공]** {ticker} {buy_qty}주\n"
                        f"- 주문단가: ${numeric_price:.2f}\n"
                        f"- 진입 지표: RSI {rsi_val:.1f} | 거래량 {vol_surge:.1f}배 | 20이평 밀착 ({disparity:.1f}%) | Stoch({k_val:.1f}>{d_val:.1f}) | OBV {obv_txt}"
                    )
                else:
                    finish_ticker_buy(ticker, accepted=False)
                time.sleep(2)


def is_market_closing_time() -> bool:
    """미국 정규장 마감 10분 전인지 확인 (평일 15:50~16:00, 조기폐장일 12:50~13:00)"""
    try:
        return market_phase() == "closing"
    except Exception as e:
        print(f"⚠️ 시간 계산 오류: {e}")
        return False


def _momentum_continues(client, ticker: str) -> bool:
    exchange_code = get_exchange_code(client, ticker)
    analysis = get_bottom_reversal_signal(client, ticker, exchange_code)
    return bool(analysis and analysis.get("momentum_continues", False))


def liquidate_all_positions(client, held_positions: list):
    """장마감 전 보유 종목 일괄 청산 (스마트 홀딩 조건 충족 종목은 오버나잇 유지)"""
    global HOLD_NOTICE_DATE
    if not held_positions:
        return

    print("\n⏰ ========================================================")
    print("🚨 [장마감 10분 전] 스마트 홀딩 판정 후 나머지 종목을 일괄 청산합니다.")
    print("============================================================")

    positions = []
    for item in held_positions:
        ticker = item.get("stk_cd") or item.get("symbol")
        try:
            profit_rate = float(str(item.get("pl_rt", "0")).replace(",", "").strip() or "0")
        except (TypeError, ValueError):
            profit_rate = 0.0
        if ticker:
            positions.append({"ticker": ticker, "profit_rate": profit_rate})

    try:
        hold_decisions = select_overnight_holds(
            positions, momentum_fn=lambda t: _momentum_continues(client, t)
        )
    except Exception as e:
        # 판정 오류 시 기존처럼 전량 청산
        print(f"⚠️ 스마트 홀딩 판정 오류, 전량 청산합니다: {e}")
        hold_decisions = {}

    trading_date = now_ny().date().isoformat()
    if hold_decisions and HOLD_NOTICE_DATE != trading_date:
        lines = [
            f"• **{t}** {d['profit_rate']:+.2f}% | {d['reason']} | 뉴스: {d['news']}"
            + (f" ({d['news_reason']})" if d["news_reason"] else "")
            + f" | 연속 {d['consecutive_days'] + 1}일째"
            for t, d in hold_decisions.items()
        ]
        send_discord_message("🌙 **[오버나잇 홀딩]** 마감 청산 제외 종목\n" + "\n".join(lines))
        HOLD_NOTICE_DATE = trading_date

    accepted_orders = 0
    skipped_orders = 0

    for item in held_positions:
        ticker = item.get("stk_cd") or item.get("symbol")
        if ticker in hold_decisions:
            print(f"  🌙 [오버나잇 홀딩] {ticker}: {hold_decisions[ticker]['reason']}")
            continue
        qty = 0
        for key in ("poss_qty", "rmnd_qty", "qty"):
            try:
                qty = int(float(str(item.get(key, "0")).replace(",", "").strip() or "0"))
            except (TypeError, ValueError):
                continue
            if qty > 0:
                break

        curr_price = 0.0
        for key in ("now_pric", "cur_prc", "now_price", "cur_price"):
            try:
                curr_price = float(str(item.get(key, "0")).replace(",", "").strip() or "0")
            except (TypeError, ValueError):
                continue
            if curr_price > 0:
                break

        if qty <= 0:
            skipped_orders += 1
            print(f"⚠️ [마감 청산 건너뜀] {ticker}: 보유 수량 필드를 확인할 수 없습니다.")
            continue
        if curr_price <= 0:
            skipped_orders += 1
            print(f"⚠️ [마감 청산 건너뜀] {ticker}: 현재가 필드를 확인할 수 없습니다.")
            continue

        exchange_code = get_exchange_code(client, ticker)
        # 즉시 체결을 위해 현재가보다 살짝 아래(-1%)로 던져 잔량 없이 털어냄
        sell_target_price = format_us_price(curr_price * 0.99)

        print(f"  ➔ [마감 일괄 청산] {ticker} {qty}주 매도 발송 중...")
        res = client.order_us_stock(
            ticker=ticker,
            order_type="SELL",
            qty=qty,
            price=sell_target_price,
            exchange=exchange_code,
        )

        code = res.get("return_code", -1)
        msg = res.get("return_msg", "응답 없음")
        print(f"  ➔ {ticker} 마감 청산 응답: [{code}] {msg}")
        if str(code) == "0":
            accepted_orders += 1
            record_ticker_sell(ticker)
        else:
            skipped_orders += 1
        time.sleep(1)

    print(
        f"📋 [마감 청산 결과] 주문 접수 {accepted_orders}건, 실패/건너뜀 {skipped_orders}건 "
        "(실제 체결은 계좌 잔고에서 확인 필요)"
    )

def main():
    print("🚀 [Quant Sentinel] 바닥 반등 선취매 자동매매 상시 스케줄러 가동")
    client = KiwoomClient()
    dry_run = False  # 실거래 집행 모드

    was_open = None

    while True:
        is_open = market_phase() != "closed"

        if not is_open:
            next_open = next_market_open()
            if was_open is not False:
                kst_open = next_open.astimezone(pytz.timezone("Asia/Seoul"))
                message = (
                    f"💤 [장 종료/휴장] 다음 정규장 개장: 뉴욕 {next_open:%m-%d %H:%M} "
                    f"(한국 {kst_open:%m-%d %H:%M}). 그때까지 매매를 쉽니다."
                )
                print(message)
                if was_open:
                    send_discord_message(message)
            was_open = False
            # 개장 직후에 깨어나도록 최대 5분 단위로 나눠 대기
            time.sleep(min(300, max(1, seconds_until_next_open())))
            continue

        if not was_open:
            send_discord_message(
                f"🔔 [정규장 개장] 자동매매 가동 시작 (모드: {'가상' if dry_run else '실거래'})"
            )
        was_open = True

        try:
            run_trading_cycle(client, dry_run=dry_run)
        except Exception as e:
            print(f"❌ 사이클 실행 중 에러 발생: {e}")

        print("⏳ 다음 점검 사이클까지 60초 대기 중... (종료: Ctrl + C)")
        time.sleep(60)


if __name__ == "__main__":
    main()