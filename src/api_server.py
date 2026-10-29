import os
import sys
import time
import json
import threading
import warnings
import requests
import pandas as pd
import yfinance as yf
import numpy as np
import pytz
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
warnings.simplefilter(action="ignore", category=Warning)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../config/.env"), override=True)

from kiwoom_client import KiwoomClient
from market_calendar import market_phase, seconds_until_next_open
import strategy_params
import trading_safety
from trade_logger import record_trade
from trading_safety import (
    arm_take_profit,
    clear_position_exit_state,
    get_exit_rule,
    get_circuit_breaker_status,
    get_or_create_account_baseline,
    get_known_exchange_code,
    finish_ticker_buy,
    has_ticker_activity_today,
    is_momentum_continuing,
    is_take_profit_armed,
    market_trend_allows_entries,
    portfolio_equity_usd,
    record_ticker_sell,
    record_stop_loss,
    reserve_ticker_buy,
    kiwoom_minute_rows_to_frame,
)

app = FastAPI(title="Kiwoom Quant Engine with Real-News AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
IS_DRY_RUN = False  # 실서버 주문 집행 시 False로 전환

# 2. API 엔진이 진입한 보유 포지션 관리 메모리
# 형태: { ticker: { "buy_price": 5.20, "qty": 3, "highest_price": 5.40 } }
active_positions = {}
DAILY_STARTING_EQUITY = None
DAILY_START_DATE = None

kiwoom = None
try:
    kiwoom = KiwoomClient()
    print("🔑 [KIWOOM] 클라이언트 인스턴스 생성 성공:", kiwoom)
    print("🔑 [KIWOOM] 토큰 상태:", "발급됨" if getattr(kiwoom, "access_token", None) else "없음")
except Exception as e:
    print(f"❌ [KIWOOM] 클라이언트 초기화 실패 원인: {e}")

# 기본 대표 종목 데이터셋 (검색창 즉각 동작 보장)
DEFAULT_US_STOCKS = [
    {"ticker": "NVDA", "name_kr": "엔비디아", "name_en": "NVIDIA Corp", "market": "NAS"},
    {"ticker": "TSLA", "name_kr": "테슬라", "name_en": "Tesla Inc", "market": "NAS"},
    {"ticker": "AAPL", "name_kr": "애플", "name_en": "Apple Inc", "market": "NAS"},
    {"ticker": "MSFT", "name_kr": "마이크로소프트", "name_en": "Microsoft Corp", "market": "NAS"},
    {"ticker": "AMZN", "name_kr": "아마존", "name_en": "Amazon.com Inc", "market": "NAS"},
    {"ticker": "GOOGL", "name_kr": "알파벳 A (구글)", "name_en": "Alphabet Inc Class A", "market": "NAS"},
    {"ticker": "META", "name_kr": "메타 (페이스북)", "name_en": "Meta Platforms Inc", "market": "NAS"},
    {"ticker": "PLTR", "name_kr": "팔란티어 테크놀로지스", "name_en": "Palantir Tech", "market": "NYS"},
    {"ticker": "SOXL", "name_kr": "디렉시온 반도체 3배 레버리지", "name_en": "Direxion Semi Bull 3X", "market": "AMS"},
    {"ticker": "SOUN", "name_kr": "사운드하운드 AI", "name_en": "SoundHound AI", "market": "NAS"},
    {"ticker": "PLUG", "name_kr": "플러그파워", "name_en": "Plug Power", "market": "NAS"},
    {"ticker": "LCID", "name_kr": "루시드 그룹", "name_en": "Lucid Group", "market": "NAS"},
    {"ticker": "WULF", "name_kr": "테라울프", "name_en": "TeraWulf Inc", "market": "NAS"},
    {"ticker": "JOBY", "name_kr": "조비 에비에이션", "name_en": "Joby Aviation", "market": "NYS"},
]

US_STOCK_MASTER = list(DEFAULT_US_STOCKS)

def get_kiwoom_exchange_code(ticker: str) -> str:
    market_codes = {
        "NAS": "ND", "NYS": "NY", "AMS": "NA", "NA": "NA",
    }
    normalized_ticker = ticker.upper().strip()
    known_exchange = get_known_exchange_code(normalized_ticker)
    if known_exchange:
        return known_exchange

    for stock in US_STOCK_MASTER:
        if stock.get("ticker", "").upper() == normalized_ticker:
            market = stock.get("market", "").upper()
            if market in market_codes:
                return market_codes[market]

    try:
        exchange = kiwoom.get_us_exchange_code(normalized_ticker) if kiwoom else ""
        if exchange in {"ND", "NY", "NA"}:
            return exchange
    except Exception:
        pass

    return "UNKNOWN"

def load_kiwoom_stock_master():
    """키움 API usa10099 종목 마스터 로드 및 병합"""
    global US_STOCK_MASTER
    time.sleep(3)
    if not kiwoom or not kiwoom.access_token:
        return

    combined = []
    market_names = {"ND": "NAS", "NY": "NYS", "NA": "AMS"}
    for exchange in ("ND", "NY", "NA"):
        try:
            for item in kiwoom.get_us_stock_master(exchange=exchange):
                ticker = str(item.get("stk_cd", "")).strip().upper()
                english_name = str(item.get("stk_enm", "")).strip()
                name = str(item.get("stk_nm", "")).strip() or english_name or ticker
                if ticker:
                    combined.append({
                        "ticker": ticker,
                        "name_kr": name,
                        "name_en": english_name or name,
                        "market": market_names[exchange],
                    })
        except Exception as exc:
            print(f"⚠️ 키움 {exchange} 종목 마스터 조회 실패: {exc}")

    if combined:
        seen = set()
        merged = []
        for s in combined + DEFAULT_US_STOCKS:
            if s["ticker"] not in seen:
                seen.add(s["ticker"])
                merged.append(s)
        US_STOCK_MASTER = merged
        print(f"✅ 미국주식 종목 마스터 로드 완료: 총 {len(US_STOCK_MASTER)}개")

threading.Thread(target=load_kiwoom_stock_master, daemon=True).start()

def send_discord(content: str):
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=5)
    except Exception as e:
        print(f"⚠️ 디스코드 오류: {e}")

def submit_us_stock_order(
    ticker: str, order_type: str, qty: int, price: float, exchange: str | None = None
):
    if IS_DRY_RUN:
        return True, "가상(Dry-Run)"
    if not kiwoom:
        return False, "키움 클라이언트가 없습니다."

    normalized_type = order_type.upper()
    if normalized_type == "BUY" and not reserve_ticker_buy(ticker):
        return False, "오늘 이미 주문한 종목이라 재진입을 차단했습니다."

    try:
        result = kiwoom.order_us_stock(
            ticker,
            normalized_type,
            qty,
            price=price,
            exchange=exchange or get_kiwoom_exchange_code(ticker),
        )
    except Exception as exc:
        if normalized_type == "BUY":
            finish_ticker_buy(ticker, accepted=False)
        return False, str(exc)

    accepted = str(result.get("return_code", -1)) == "0"
    if normalized_type == "BUY":
        finish_ticker_buy(ticker, accepted=accepted)
    elif normalized_type == "SELL" and accepted:
        record_ticker_sell(ticker)

    return accepted, result.get("return_msg", "응답 없음")

def is_new_entry_cutoff_time() -> bool:
    """장 마감 10분 전 또는 장외 시간이면 신규 진입 차단 (조기폐장일 반영)"""
    return market_phase() != "open"

def fetch_real_news_and_ai_report(ticker: str, action: str, price: float) -> str:
    """yfinance 실시간 뉴스 수집 + Gemini 2.5 Flash 200자 브리핑"""
    news_titles = []
    try:
        yt = yf.Ticker(ticker)
        raw_news = yt.news or []
        for n in raw_news[:4]:
            title = n.get("title")
            if not title and isinstance(n.get("content"), dict):
                title = n["content"].get("title")
            if title:
                news_titles.append(f"- {title}")
    except Exception as e:
        print(f"⚠️ {ticker} 뉴스 수집 실패: {e}")

    news_context = "\n".join(news_titles) if news_titles else "최근 주요 외신 헤드라인 없음"

    prompt = (
        f"당신은 월가 퀀트 헤지펀드 트레이더입니다.\n"
        f"종목: {ticker} | 매매포지션: {action} (체결단가: ${price:.2f})\n"
        f"[야후 파이낸스 실시간 최신 뉴스 헤드라인]\n{news_context}\n\n"
        f"지침:\n"
        f"1. 위 실시간 외신 뉴스와 시장 상황을 바탕으로 이것이 호재인지 악재인지 명확히 판정하세요.\n"
        f"2. 반드시 한국어로 공백 포함 180자~220자 내외로 핵심만 요약 브리핑하세요.\n"
        f"3. 템플릿 같은 형식적인 문구는 쓰지 말고 뉴스 내용을 직접 인용하여 분석하세요."
    )

    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.4}
            }
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                result = res.json()
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            print(f"⚠️ Gemini 호출 에러: {e}")

    return f"[{'호재' if action=='BUY' else '악재'}] {ticker} 실시간 이슈: {news_titles[0] if news_titles else '거래량 변동성 확대'}. 현재가 ${price:.2f} 기준으로 단기 수급 변화가 관측되어 {action} 포지션에 진입합니다."

def calculate_technical_indicators(df: pd.DataFrame):
    """1분봉 데이터 기반 VWAP, OBV, ADX(14), MA5, MA20 벡터화 계산"""
    try:
        if df.empty or len(df) < 31:
            return None
        df = df.iloc[:-1]

        closes = df["Close"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
        highs = df["High"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["High"]
        lows = df["Low"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Low"]
        volumes = df["Volume"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Volume"]

        closes = pd.Series(closes.values.flatten(), index=df.index, dtype=float)
        opens = df["Open"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Open"]
        opens = pd.Series(opens.values.flatten(), index=df.index, dtype=float)
        highs = pd.Series(highs.values.flatten(), index=df.index, dtype=float)
        lows = pd.Series(lows.values.flatten(), index=df.index, dtype=float)
        volumes = pd.Series(volumes.values.flatten(), index=df.index, dtype=float)

        # 1. 당일 VWAP (거래량 가중 평균가)
        typical_price = (highs + lows + closes) / 3.0
        cum_vol = volumes.cumsum().replace(0, 1.0)
        vwap = (typical_price * volumes).cumsum() / cum_vol

        # 2. OBV & OBV 10이평선 (실제 자금 유입 추세)
        direction = np.where(closes > closes.shift(1), 1, np.where(closes < closes.shift(1), -1, 0))
        obv = (volumes * direction).cumsum()
        obv_ma = obv.rolling(10).mean()
        momentum_continues = is_momentum_continuing(
            price=float(closes.iloc[-1]),
            vwap=float(vwap.iloc[-1]),
            short_ma=float(closes.rolling(5).mean().iloc[-1]),
            long_ma=float(closes.rolling(20).mean().iloc[-1]),
            obv=float(obv.iloc[-1]),
            obv_ma=float(obv_ma.iloc[-1]),
            candle_green=bool(closes.iloc[-1] >= opens.iloc[-1]),
        )

        # 3. ADX & DMI (+DI, -DI) (추세 강도 분석)
        up_move = highs - highs.shift(1)
        down_move = lows.shift(1) - lows
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        tr1 = highs - lows
        tr2 = (highs - closes.shift(1)).abs()
        tr3 = (lows - closes.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        tr_smooth = pd.Series(tr).rolling(14).mean().replace(0, 0.0001)
        plus_di = (pd.Series(plus_dm, index=df.index).rolling(14).mean() / tr_smooth) * 100
        minus_di = (pd.Series(minus_dm, index=df.index).rolling(14).mean() / tr_smooth) * 100

        dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 0.0001)) * 100
        adx = dx.rolling(14).mean()

        # 4. 단기 이동평균선 & 평균 거래량
        ma5 = closes.rolling(5).mean()
        ma20 = closes.rolling(20).mean()
        avg_vol = volumes.iloc[-21:-1].mean()

        return {
            "cur_price": float(closes.iloc[-1]),
            "cur_vol": float(volumes.iloc[-1]),
            "avg_vol": float(avg_vol),
            "vwap": float(vwap.iloc[-1]),
            "obv": float(obv.iloc[-1]),
            "obv_ma": float(obv_ma.iloc[-1]),
            "adx": float(adx.iloc[-1]),
            "plus_di": float(plus_di.iloc[-1]),
            "minus_di": float(minus_di.iloc[-1]),
            "ma5": float(ma5.iloc[-1]),
            "ma20": float(ma20.iloc[-1]),
            "momentum_continues": momentum_continues,
        }
    except Exception:
        return None

def real_trading_engine():
    """5중 진입 검증과 공통 모멘텀 익절·손절을 수행하는 엔진"""
    global active_positions, DAILY_STARTING_EQUITY, DAILY_START_DATE
    time.sleep(5)
    print("⚡ [엔진] 1분봉 5대 핵심 지표(VWAP/OBV/ADX/이평/거래량) 실전 감시 가동 시작")

    while True:
        # 정규장(휴장일·조기폐장 반영) 외 시간에는 시세 조회·주문 없이 대기
        if market_phase() == "closed":
            time.sleep(min(300, max(1, seconds_until_next_open())))
            continue

        # 가용 예수금 실시간 확인
        available_usd = 0.0
        usd_direct = 0.0
        krw_direct = 0.0
        daily_starting_balance = 0.0
        if kiwoom:
            try:
                deposit_info = kiwoom.get_deposit()
                usd_direct = float(deposit_info.get("usd_deposit", 0.0))
                krw_direct = float(deposit_info.get("krw_deposit", 0.0))
                available_usd = usd_direct + (krw_direct / 1380.0)
            except Exception:
                pass

        today_ny = datetime.now(pytz.timezone("America/New_York")).date().isoformat()
        if DAILY_START_DATE != today_ny:
            try:
                opening_holdings = kiwoom.get_us_holdings() if kiwoom else []
                DAILY_STARTING_EQUITY = portfolio_equity_usd(
                    usd_direct, opening_holdings
                )
            except Exception:
                DAILY_STARTING_EQUITY = usd_direct
            DAILY_START_DATE = today_ny
            # 전날 장 마감 후 자동 조정된 익절·손절 기준 반영
            strategy_params.apply_to_engine()

        daily_starting_balance = DAILY_STARTING_EQUITY or usd_direct

        if not IS_DRY_RUN:
            breaker = get_circuit_breaker_status(daily_starting_balance)
            if breaker["newly_blocked"]:
                send_discord(
                    f"🛑 [일일 서킷브레이커 발동] 신규 매수 중단: {breaker['reason']}"
                )

        candidates = (
            get_rotating_us_candidates(
                kiwoom,
                max_price=min(available_usd, 50.0),
                limit=12,
            )
            if kiwoom and available_usd > 0
            else []
        )
        candidate_by_ticker = {item["ticker"]: item for item in candidates}
        for ticker, position in active_positions.items():
            candidate_by_ticker.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "exchange": position.get("exchange") or get_kiwoom_exchange_code(ticker),
                },
            )

        for candidate in candidate_by_ticker.values():
            ticker = candidate["ticker"]
            try:
                exchange_code = candidate.get("exchange") or get_kiwoom_exchange_code(ticker)
                is_soxl_amex = ticker == "SOXL" and exchange_code == "NA"
                if exchange_code != "ND" and not is_soxl_amex and ticker not in active_positions:
                    print(f"⏭️ [NASDAQ 전용] {ticker} 매수 스캔 제외 (거래소: {exchange_code})")
                    continue
                if ticker not in active_positions and has_ticker_activity_today(ticker):
                    print(f"⏸️ [당일 재진입 차단] {ticker}: 오늘 이미 주문했습니다.")
                    continue

                # 키움 미국주식 1분봉(usa06011)
                candle_rows = kiwoom.get_us_stock_minute_chart(
                    ticker, exchange=exchange_code, interval="1", max_pages=2
                )
                data = kiwoom_minute_rows_to_frame(candle_rows)
                if data.empty or len(data) < 30:
                    continue

                ind = calculate_technical_indicators(data)
                if not ind:
                    continue

                cur_price = ind["cur_price"]

                # -------------------------------------------------------------
                # CASE 1: 보유 포지션을 공통 익절·손절 기준으로 관리
                # -------------------------------------------------------------
                if ticker in active_positions:
                    pos = active_positions[ticker]
                    buy_price = pos["buy_price"]
                    profit_rate = ((cur_price - buy_price) / buy_price) * 100
                    position_key = f"api:{ticker}:{buy_price:.4f}:{pos['qty']}"
                    take_profit_armed = is_take_profit_armed(position_key)
                    if profit_rate >= trading_safety.TAKE_PROFIT_PCT:
                        take_profit_armed = True
                        if not IS_DRY_RUN:
                            arm_take_profit(position_key)

                    momentum_continues = bool(
                        ind.get("momentum_continues", False)
                    ) if take_profit_armed and profit_rate < trading_safety.MAX_TAKE_PROFIT_PCT else False
                    exit_rule = get_exit_rule(
                        profit_rate,
                        momentum_continues=momentum_continues,
                        take_profit_armed=take_profit_armed,
                    )

                    if exit_rule:
                        exit_labels = {
                            "STOP_LOSS": ("🛑", "손절", f"손절 기준 {trading_safety.STOP_LOSS_PCT}% 도달"),
                            "TAKE_PROFIT": ("🎉", "익절", f"익절 기준 +{trading_safety.TAKE_PROFIT_PCT}% 도달"),
                            "MAX_TAKE_PROFIT": (
                                "🚀",
                                "최대 상한 익절",
                                f"최대 익절 기준 +{trading_safety.MAX_TAKE_PROFIT_PCT}% 도달",
                            ),
                        }
                        emoji, exit_name, exit_reason = exit_labels[exit_rule]
                        print(
                            f"{emoji} [{exit_name}] {ticker} | 수익률: {profit_rate:+.2f}%"
                        )
                        order_accepted, order_status = submit_us_stock_order(
                            ticker, "SELL", pos["qty"], cur_price, exchange_code
                        )
                        if not order_accepted:
                            print(f"❌ [{exit_name} 매도 주문 실패] {ticker}: {order_status}")
                            continue
                        clear_position_exit_state(position_key)
                        if not IS_DRY_RUN:
                            try:
                                # 체결가가 아니라 주문 시점 현재가 기준 추정치
                                record_trade(
                                    ticker,
                                    buy_price,
                                    cur_price,
                                    pos["qty"],
                                    {"STOP_LOSS": "손절", "TAKE_PROFIT": "익절", "MAX_TAKE_PROFIT": "최대익절"}[exit_rule],
                                )
                            except Exception as e:
                                print(f"⚠️ 매매 기록 저장 실패: {e}")

                        if exit_rule == "STOP_LOSS" and not IS_DRY_RUN:
                            breaker = record_stop_loss(
                                position_key=f"api:{ticker}:{buy_price:.4f}:{pos['qty']}",
                                estimated_loss=max(
                                    0.0, (buy_price - cur_price) * pos["qty"]
                                ),
                                starting_balance=daily_starting_balance,
                            )
                            if breaker["newly_blocked"]:
                                send_discord(
                                    f"🛑 [일일 서킷브레이커 발동] 신규 매수 중단: {breaker['reason']}"
                                )

                        ai_report = fetch_real_news_and_ai_report(
                            ticker, "SELL", cur_price
                        )
                        send_discord(
                            f"{emoji} **[{exit_name} 주문 접수]**\n"
                            f"• 종목: **{ticker}** | 주문기준가: ${cur_price:.2f} ({pos['qty']}주)\n"
                            f"• 수익률: **{profit_rate:+.2f}%** | 주문상태: `{order_status}`\n"
                            f"• 사유: {exit_reason}\n"
                            f"────────────────────────\n"
                            f"📰 **외신 AI 리포트**:\n{ai_report}"
                        )
                        del active_positions[ticker]
                        continue

                    continue

                # -------------------------------------------------------------
                # CASE 2: 미보유 상태 -> 1분봉 5중 복합 퀀트 지표 동시 검증
                # -------------------------------------------------------------
                if is_new_entry_cutoff_time():
                    continue

                breaker = get_circuit_breaker_status(daily_starting_balance)
                if breaker["newly_blocked"]:
                    send_discord(
                        f"🛑 [일일 서킷브레이커 발동] 신규 매수 중단: {breaker['reason']}"
                    )
                if breaker["blocked"]:
                    print(f"🛑 [일일 서킷브레이커] 신규 매수 차단: {breaker['reason']}")
                    continue

                market_ok, market_reason = market_trend_allows_entries(kiwoom)
                if not market_ok:
                    print(f"📉 [시장 장세 필터] 신규 매수 차단: {market_reason}")
                    continue

                if cur_price > available_usd or cur_price < 1.0:
                    continue

                # 5대 지표 개별 플래그 판정
                cond_vwap = cur_price > ind["vwap"]                          # 1. 당일 VWAP 상회
                cond_obv = ind["obv"] > ind["obv_ma"]                        # 2. OBV 추세선 상회 (자금 유입)
                cond_adx = (ind["adx"] >= 25.0) and (ind["plus_di"] > ind["minus_di"])  # 3. ADX 강추세 + 매수우위
                cond_ma = ind["ma5"] > ind["ma20"]                           # 4. 단기 이평선 정배열
                volume_ratio = ind["cur_vol"] / ind["avg_vol"] if ind["avg_vol"] > 0 else 0.0
                cond_vol = 3.0 <= volume_ratio <= 5.0 # 5. 완료봉 평균 대비 3~5배 거래량

                # 5개 조건이 완벽히 동시 충족될 때만 진입
                if cond_vwap and cond_obv and cond_adx and cond_ma and cond_vol:
                    vol_ratio = volume_ratio
                    
                    # 1회 매수 수량 계산 (1회당 최대 약 $35 한도 내에서 주수 결정, 최소 1주)
                    target_budget = min(available_usd * 0.3, 35.0)
                    buy_qty = min(5, max(1, int(target_budget // cur_price)))

                    if (cur_price * buy_qty) > available_usd:
                        buy_qty = 1

                    print(f"🚀 [5중 검증 급등 포착] {ticker} | 단가: ${cur_price:.2f} ({buy_qty}주) | ADX: {ind['adx']:.1f} | 거래량: {vol_ratio:.1f}배")

                    order_accepted, order_status = submit_us_stock_order(
                        ticker, "BUY", buy_qty, cur_price, exchange_code
                    )
                    if not order_accepted:
                        print(f"❌ [매수 주문 실패] {ticker}: {order_status}")
                        continue

                    active_positions[ticker] = {
                        "buy_price": cur_price,
                        "qty": buy_qty,
                        "highest_price": cur_price,
                        "exchange": exchange_code,
                    }

                    ai_report = fetch_real_news_and_ai_report(ticker, "BUY", cur_price)

                    msg = (
                        f"⚡ **[1분봉 5중 복합 퀀트 매수 주문 접수]** {ticker}\n"
                        f"• 주문기준가: **${cur_price:.2f}** ({buy_qty}주) | 주문상태: `{order_status}`\n"
                        f"• **지표 검증 통과**:\n"
                        f"  - VWAP 돌파: ${cur_price:.2f} > ${ind['vwap']:.2f}\n"
                        f"  - ADX 추세강도: {ind['adx']:.1f} (상승 탄력 돌입)\n"
                        f"  - OBV 자금흐름: 매수 누적선 상향 정배열\n"
                        f"  - 거래량 폭증: 평균 대비 **{vol_ratio:.1f}배** 유입\n"
                        f"────────────────────────\n"
                        f"📰 **외신 AI 리포트**:\n{ai_report}"
                    )
                    send_discord(msg)

            except Exception:
                pass

        time.sleep(10)  

threading.Thread(target=real_trading_engine, daemon=True).start()

# --- 프론트엔드 연동 엔드포인트 ---

def safe_float(v, default=0.0):
    """안전한 float 변환 헬퍼 함수"""
    try:
        if v is None:
            return default
        s = str(v).replace(",", "").strip()
        return float(s) if s else default
    except Exception:
        return default

@app.get("/api/account/details")
def get_account_details():
    try:
        bal = kiwoom.get_us_balance() or {}
        dep = kiwoom.get_deposit() or {}

        # 1. ust21070 응답 원장 목록 추출
        raw_holdings = bal.get("holdings_list") or bal.get("output1") or bal.get("result_list") or []
        if isinstance(raw_holdings, dict):
            raw_holdings = [raw_holdings]

        holdings = []
        total_eval_usd = 0.0
        total_profit_usd = 0.0

        for item in raw_holdings:
            # 주식 종목 식별자 추출 (stk_cd가 우선)
            ticker = item.get("stk_cd") or item.get("ovrs_pdno") or item.get("symbol")
            if not ticker or str(ticker).strip() == "":
                continue

            name = item.get("frgn_stk_nm") or item.get("ovrs_item_name") or ticker
            
            # 수량 추출 (poss_qty, qty, sell_alowq 중 존재하는 값)
            qty_raw = item.get("poss_qty") or item.get("qty") or item.get("sell_alowq") or 0
            qty = int(float(str(qty_raw).replace(",", "").strip()))
            if qty <= 0:
                continue

            # 금액 및 손익 안전 파싱
            def parse_num(key_list, default=0.0):
                for k in key_list:
                    v = item.get(k)
                    if v is not None and str(v).strip() != "":
                        try:
                            return float(str(v).replace(",", "").strip())
                        except ValueError:
                            pass
                return default

            buy_price = parse_num(["frgn_stk_book_uv", "pchs_avg_pric", "book_uv"])
            current_price = parse_num(["now_pric", "now_pric2", "cprc"])
            eval_amt = parse_num(["evlt_amt", "ovrs_stck_evlu_amt"])
            profit_amt = parse_num(["pl_amt", "frgn_evlu_pfls_amt"])
            profit_rate = parse_num(["pl_rt", "evlu_pfls_rt"])

            # eval_amt가 0이면 수량 * 현재가로 보정
            if eval_amt == 0.0 and current_price > 0:
                eval_amt = current_price * qty

            total_eval_usd += eval_amt
            total_profit_usd += profit_amt

            holdings.append({
                "ticker": str(ticker).strip().upper(),
                "name": str(name).strip(),
                "qty": qty,
                "purchase_price": round(buy_price, 2),
                "current_price": round(current_price, 2),
                "eval_amount": round(eval_amt, 2),
                "profit_amt": round(profit_amt, 2),
                "profit_rate": round(profit_rate, 2),
            })

        # 보유주 미실현손익과 계좌 전체 추적 손익을 분리
        total_cost = total_eval_usd - total_profit_usd
        total_profit_rate = round((total_profit_usd / total_cost * 100), 2) if total_cost > 0 else 0.0
        usd_deposit = safe_float(dep.get("usd_deposit", 0.0))
        account_equity_usd = round(usd_deposit + total_eval_usd, 2)
        account_baseline = get_or_create_account_baseline(account_equity_usd)
        account_profit_usd = round(
            account_equity_usd - account_baseline["equity_usd"], 2
        )
        account_profit_rate = (
            round(account_profit_usd / account_baseline["equity_usd"] * 100, 2)
            if account_baseline["equity_usd"] > 0
            else 0.0
        )

        return {
            "account_no": kiwoom.full_account_no,
            "krw_deposit": dep.get("krw_deposit", 0.0),
            "usd_deposit": usd_deposit,
            "account_equity_usd": account_equity_usd,
            "account_baseline_usd": account_baseline["equity_usd"],
            "account_baseline_date": account_baseline["date"],
            "account_profit_usd": account_profit_usd,
            "account_profit_rate": account_profit_rate,
            
            # 총 평가금액 필드 매핑
            "total_eval_usd": round(total_eval_usd, 2),
            "total_eval_amt": round(total_eval_usd, 2),
            "total_eval": round(total_eval_usd, 2),
            
            # 총 평가손익(달러) 필드 매핑
            "total_profit_usd": round(total_profit_usd, 2),
            "total_pl_usd": round(total_profit_usd, 2),
            "profit_amt": round(total_profit_usd, 2),
            "total_profit": round(total_profit_usd, 2),
            "total_return_rate": total_profit_rate,
            "total_profit_rate": total_profit_rate,
            
            # 총 수익률(%) 필드 매핑 (프론트가 참조 가능한 키 모두 제공)
            "total_profit_rate": total_profit_rate,
            "total_pl_rate": total_profit_rate,
            "profit_rate": total_profit_rate,
            "total_rate": total_profit_rate,
            "pl_rate": total_profit_rate,
            "rate": total_profit_rate,
            
            "holdings": holdings,
            "holdings_count": len(holdings)
        }
    except Exception as e:
        print(f"⚠️ get_account_details 처리 오류: {e}")
        return {
            "account_no": getattr(kiwoom, "full_account_no", ""),
            "krw_deposit": 0.0,
            "usd_deposit": 0.0,
            "account_equity_usd": 0.0,
            "account_baseline_usd": 0.0,
            "account_baseline_date": "",
            "account_profit_usd": 0.0,
            "account_profit_rate": 0.0,
            "total_eval_usd": 0.0,
            "total_profit_usd": 0.0,
            "total_profit_rate": 0.0,
            "holdings": [],
            "holdings_count": 0
        }

@app.get("/api/search")
def search_all_stocks(q: str = Query("", min_length=1)):
    """전종목 실시간 필터링 (티커, 한글명, 영문명)"""
    query = q.strip().upper()
    results = []
    for item in US_STOCK_MASTER:
        if (query in item["ticker"].upper()) or (query in item["name_kr"].upper()) or (query in item["name_en"].upper()):
            results.append(item)
            if len(results) >= 20:
                break
    return results

@app.get("/api/candles/{ticker}")
def get_candles(ticker: str, timeframe: str = Query("1D")):
    try:
        normalized_ticker = ticker.upper().strip()
        exchange = get_kiwoom_exchange_code(normalized_ticker)
        if exchange not in {"ND", "NY", "NA"}:
            raise HTTPException(status_code=404, detail="키움 거래소 정보를 찾을 수 없습니다.")

        today_ny = datetime.now(pytz.timezone("America/New_York")).date()
        chart_configs = {
            "1m": ("minute", "1", 1),
            "10m": ("minute", "10", 5),
            "30m": ("minute", "30", 30),
            "1h": ("minute", "60", 90),
            "3h": ("minute", "60", 90),
            "1D": ("D", "", 180),
            "1W": ("W", "", 730),
            "1M": ("M", "", 1825),
        }
        chart_type, interval, lookback_days = chart_configs.get(
            timeframe, chart_configs["1D"]
        )
        start_date = (today_ny - pd.Timedelta(days=lookback_days)).strftime("%Y%m%d")

        if chart_type == "minute":
            rows = kiwoom.get_us_stock_minute_chart(
                normalized_ticker,
                exchange=exchange,
                interval=interval,
                start_date=start_date,
                max_pages=5,
            )
        else:
            rows = kiwoom.get_us_stock_period_chart(
                normalized_ticker,
                exchange=exchange,
                period=chart_type,
                start_date=start_date,
                max_pages=5,
            )

        if not rows:
            raise HTTPException(status_code=404, detail="시세 데이터가 없습니다.")

        candles = []
        for row in rows:
            date_text = str(row.get("dt") or row.get("bus_dt") or row.get("cntr_tm") or "")
            if not date_text:
                continue

            try:
                if chart_type == "minute":
                    timestamp_text = str(row.get("cntr_tm") or date_text)
                    timestamp_dt = datetime.strptime(timestamp_text[:14], "%Y%m%d%H%M%S")
                    timestamp_dt = pytz.timezone("America/New_York").localize(timestamp_dt)
                    t_val = int(timestamp_dt.timestamp())
                else:
                    day_text = str(row.get("dt") or row.get("bus_dt") or date_text)[:8]
                    t_val = f"{day_text[:4]}-{day_text[4:6]}-{day_text[6:8]}"
                open_price = safe_float(row.get("open_pric"))
                high_price = safe_float(row.get("high_pric"))
                low_price = safe_float(row.get("low_pric"))
                close_price = safe_float(row.get("cur_prc"))
                volume = int(safe_float(row.get("trde_qty") or row.get("acc_trde_qty")))
            except (TypeError, ValueError):
                continue
            if min(open_price, high_price, low_price, close_price) <= 0:
                continue
            candles.append({
                "time": t_val,
                "open": round(open_price, 2),
                "high": round(high_price, 2),
                "low": round(low_price, 2),
                "close": round(close_price, 2),
                "volume": max(0, volume),
            })

        if timeframe == "3h":
            grouped = []
            for index in range(0, len(candles), 3):
                chunk = candles[index : index + 3]
                if len(chunk) < 3:
                    continue
                grouped.append({
                    "time": chunk[0]["time"],
                    "open": chunk[0]["open"],
                    "high": max(candle["high"] for candle in chunk),
                    "low": min(candle["low"] for candle in chunk),
                    "close": chunk[-1]["close"],
                    "volume": sum(candle["volume"] for candle in chunk),
                })
            candles = grouped

        candles.sort(key=lambda candle: candle["time"])
        return {"ticker": normalized_ticker, "candles": candles}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(e))