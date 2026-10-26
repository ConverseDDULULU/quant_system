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
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
warnings.simplefilter(action="ignore", category=Warning)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../config/.env"), override=True)

from kiwoom_client import KiwoomClient

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
IS_DRY_RUN = True  # 실서버 주문 집행 시 False로 전환

# 1. 급등주 및 유망 모멘텀 종목 풀 ($2 ~ $30 범위 유동성 대장주)
MOMENTUM_POOL = [
    "SOUN", "PLUG", "LCID", "OPEN", "GRAB", "CLOV",
    "WULF", "CIFR", "RUM", "JOBY", "SOXL", "PLTR"
]

# 2. 보유 포지션 관리 메모리 (익절/손절/트레일링 추적용)
# 형태: { ticker: { "buy_price": 5.20, "qty": 3, "highest_price": 5.40 } }
active_positions = {}

kiwoom = None
try:
    kiwoom = KiwoomClient()
    print("🔑 [KIWOOM] 클라이언트 인스턴스 생성 성공:", kiwoom)
    print("🔑 [KIWOOM] 토큰 상태:", getattr(kiwoom, "access_token", None))
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

def load_kiwoom_stock_master():
    """키움 API usa10099 종목 마스터 로드 및 병합"""
    global US_STOCK_MASTER
    time.sleep(3)
    if not kiwoom or not kiwoom.access_token:
        return

    url = f"{kiwoom.base_url}/api/us/stkinfo"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {kiwoom.access_token}",
        "api-id": "usa10099",
    }

    combined = []
    for exch in ["NAS", "NYS", "AMS"]:
        try:
            res = requests.post(url, headers=headers, json={"exch_cd": exch}, timeout=10)
            if res.status_code == 200:
                data = res.json()
                for item in data.get("stk_list", []):
                    ticker = item.get("stk_cd", "").strip()
                    kr_name = item.get("stk_nm_korean", "").strip()
                    en_name = item.get("stk_nm_eng", "").strip()
                    if ticker:
                        combined.append({
                            "ticker": ticker,
                            "name_kr": kr_name if kr_name else en_name,
                            "name_en": en_name,
                            "market": exch
                        })
        except Exception:
            pass

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
        closes = df["Close"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
        highs = df["High"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["High"]
        lows = df["Low"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Low"]
        volumes = df["Volume"].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df["Volume"]

        closes = pd.Series(closes.values.flatten(), index=df.index, dtype=float)
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
        avg_vol = volumes.rolling(20).mean()

        return {
            "cur_price": float(closes.iloc[-1]),
            "cur_vol": float(volumes.iloc[-1]),
            "avg_vol": float(avg_vol.iloc[-1]),
            "vwap": float(vwap.iloc[-1]),
            "obv": float(obv.iloc[-1]),
            "obv_ma": float(obv_ma.iloc[-1]),
            "adx": float(adx.iloc[-1]),
            "plus_di": float(plus_di.iloc[-1]),
            "minus_di": float(minus_di.iloc[-1]),
            "ma5": float(ma5.iloc[-1]),
            "ma20": float(ma20.iloc[-1]),
        }
    except Exception:
        return None

def real_trading_engine():
    """예수금 실시간 검증 + 1분봉 5중 복합 퀀트 검증 + 트레일링/상한 익절 + 칼손절 엔진"""
    global active_positions
    time.sleep(5)
    print("⚡ [엔진] 1분봉 5대 핵심 지표(VWAP/OBV/ADX/이평/거래량) 실전 감시 가동 시작")

    while True:
        # 가용 예수금 실시간 확인
        available_usd = 0.0
        if kiwoom:
            try:
                deposit_info = kiwoom.get_deposit()
                usd_direct = float(deposit_info.get("usd_deposit", 0.0))
                krw_direct = float(deposit_info.get("krw_deposit", 0.0))
                available_usd = usd_direct + (krw_direct / 1380.0)
            except Exception:
                pass

        for ticker in MOMENTUM_POOL:
            try:
                # 1분봉(1m, 당일 데이터) 수집
                data = yf.download(ticker, period="1d", interval="1m", progress=False, auto_adjust=True)
                if data.empty or len(data) < 30:
                    continue

                ind = calculate_technical_indicators(data)
                if not ind:
                    continue

                cur_price = ind["cur_price"]

                # -------------------------------------------------------------
                # CASE 1: 보유 중인 포지션 관리 -> 손절(-2.5%) / 맥시멈 상한(+30%) / 트레일링 스탑
                # -------------------------------------------------------------
                if ticker in active_positions:
                    pos = active_positions[ticker]
                    buy_price = pos["buy_price"]

                    # 최고가 실시간 갱신
                    if cur_price > pos["highest_price"]:
                        pos["highest_price"] = cur_price

                    highest_price = pos["highest_price"]
                    profit_rate = ((cur_price - buy_price) / buy_price) * 100
                    pullback_rate = ((cur_price - highest_price) / highest_price) * 100
                    highest_profit = ((highest_price - buy_price) / buy_price) * 100

                    # 1) 원금 보호 기계적 손절 (-2.5% 이하)
                    if profit_rate <= -2.5:
                        print(f"🛑 [원금보호 손절] {ticker} | 실현수익률: {profit_rate:+.2f}%")
                        order_status = "가상(Dry-Run)"
                        if not IS_DRY_RUN and kiwoom:
                            order_res = kiwoom.order_us_stock(ticker, "SELL", pos["qty"])
                            order_status = order_res.get("return_msg", "접수완료")

                        ai_report = fetch_real_news_and_ai_report(ticker, "SELL", cur_price)
                        send_discord(
                            f"🛑 **[원금 보호 손절 체결]**\n"
                            f"• 종목: **{ticker}** | 청산단가: ${cur_price:.2f} ({pos['qty']}주)\n"
                            f"• 실현손익: **{profit_rate:+.2f}%** | 주문상태: `{order_status}`\n"
                            f"• 사유: 손절선(-2.5%) 터치에 따른 손실 고정 방어\n"
                            f"────────────────────────\n"
                            f"📰 **외신 AI 리포트**:\n{ai_report}"
                        )
                        del active_positions[ticker]
                        continue

                    # 2) 맥시멈 목표 상한선 도달 전량 익절 (+30.0% 이상)
                    if profit_rate >= 30.0:
                        print(f"🎉 [맥시멈 목표 달성] {ticker} | 실현수익률: {profit_rate:+.2f}%")
                        order_status = "가상(Dry-Run)"
                        if not IS_DRY_RUN and kiwoom:
                            order_res = kiwoom.order_us_stock(ticker, "SELL", pos["qty"])
                            order_status = order_res.get("return_msg", "접수완료")

                        ai_report = fetch_real_news_and_ai_report(ticker, "SELL", cur_price)
                        send_discord(
                            f"🎉 **[최대 목표 달성 전량 익절]**\n"
                            f"• 종목: **{ticker}** | 청산단가: ${cur_price:.2f} ({pos['qty']}주)\n"
                            f"• **실현수익률: {profit_rate:+.2f}%** | 주문상태: `{order_status}`\n"
                            f"• 사유: 설정된 최대 목표 수익률(+30%) 완벽 달성\n"
                            f"────────────────────────\n"
                            f"📰 **외신 AI 리포트**:\n{ai_report}"
                        )
                        del active_positions[ticker]
                        continue

                    # 3) 트레일링 스탑 익절 (+4.0% 이상 도달 후 최고점 대비 -2.5% 되돌림 시)
                    if highest_profit >= 4.0 and pullback_rate <= -2.5:
                        print(f"🎯 [트레일링 익절] {ticker} | 최고점: +{highest_profit:.2f}% -> 확정: {profit_rate:+.2f}%")
                        order_status = "가상(Dry-Run)"
                        if not IS_DRY_RUN and kiwoom:
                            order_res = kiwoom.order_us_stock(ticker, "SELL", pos["qty"])
                            order_status = order_res.get("return_msg", "접수완료")

                        ai_report = fetch_real_news_and_ai_report(ticker, "SELL", cur_price)
                        send_discord(
                            f"🔔 **[트레일링 스탑 수익 보존 청산]**\n"
                            f"• 종목: **{ticker}** | 청산단가: ${cur_price:.2f} ({pos['qty']}주)\n"
                            f"• 최고점: +{highest_profit:.2f}% | **최종 실현수익률: {profit_rate:+.2f}%**\n"
                            f"• 사유: 최고점 대비 -2.5% 꺾임 발생 (수익 확정)\n"
                            f"────────────────────────\n"
                            f"📰 **외신 AI 리포트**:\n{ai_report}"
                        )
                        del active_positions[ticker]
                        continue

                    continue

                # -------------------------------------------------------------
                # CASE 2: 미보유 상태 -> 1분봉 5중 복합 퀀트 지표 동시 검증
                # -------------------------------------------------------------
                if cur_price > available_usd or cur_price < 1.0:
                    continue

                # 5대 지표 개별 플래그 판정
                cond_vwap = cur_price > ind["vwap"]                          # 1. 당일 VWAP 상회
                cond_obv = ind["obv"] > ind["obv_ma"]                        # 2. OBV 추세선 상회 (자금 유입)
                cond_adx = (ind["adx"] >= 25.0) and (ind["plus_di"] > ind["minus_di"])  # 3. ADX 강추세 + 매수우위
                cond_ma = ind["ma5"] > ind["ma20"]                           # 4. 단기 이평선 정배열
                cond_vol = (ind["avg_vol"] > 0) and (ind["cur_vol"] >= ind["avg_vol"] * 2.5) # 5. 1분 거래량 2.5배 폭증

                # 5개 조건이 완벽히 동시 충족될 때만 진입
                if cond_vwap and cond_obv and cond_adx and cond_ma and cond_vol:
                    vol_ratio = ind["cur_vol"] / ind["avg_vol"]
                    
                    # 1회 매수 수량 계산 (1회당 최대 약 $35 한도 내에서 주수 결정, 최소 1주)
                    target_budget = min(available_usd * 0.3, 35.0)
                    buy_qty = max(1, int(target_budget // cur_price))

                    if (cur_price * buy_qty) > available_usd:
                        buy_qty = 1

                    print(f"🚀 [5중 검증 급등 포착] {ticker} | 단가: ${cur_price:.2f} ({buy_qty}주) | ADX: {ind['adx']:.1f} | 거래량: {vol_ratio:.1f}배")

                    order_status = "가상(Dry-Run)"
                    if not IS_DRY_RUN and kiwoom:
                        order_res = kiwoom.order_us_stock(ticker, "BUY", buy_qty)
                        order_status = order_res.get("return_msg", "접수완료")

                    active_positions[ticker] = {
                        "buy_price": cur_price,
                        "qty": buy_qty,
                        "highest_price": cur_price
                    }

                    ai_report = fetch_real_news_and_ai_report(ticker, "BUY", cur_price)

                    msg = (
                        f"⚡ **[1분봉 5중 복합 퀀트 매수 체결]** {ticker}\n"
                        f"• 체결단가: **${cur_price:.2f}** ({buy_qty}주) | 주문상태: `{order_status}`\n"
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

      # 총 투자 원금 대비 수익률 계산
        total_cost = total_eval_usd - total_profit_usd
        total_profit_rate = round((total_profit_usd / total_cost * 100), 2) if total_cost > 0 else 0.0

        return {
            "account_no": kiwoom.full_account_no,
            "krw_deposit": dep.get("krw_deposit", 0.0),
            "usd_deposit": dep.get("usd_deposit", 0.0),
            
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
    tf_map = {
        "1m": ("1d", "1m"),
        "10m": ("5d", "5m"),
        "30m": ("1mo", "30m"),
        "1h": ("1mo", "1h"),
        "3h": ("3mo", "1h"),
        "1D": ("6mo", "1d"),
        "1W": ("1y", "1wk"),
        "1M": ("2y", "1mo")
    }
    period, interval = tf_map.get(timeframe, ("6mo", "1d"))

    try:
        data = yf.download(ticker.upper(), period=period, interval=interval, progress=False, auto_adjust=True)
        if data.empty:
            raise HTTPException(status_code=404, detail="시세 데이터가 없습니다.")

        close = data["Close"].iloc[:, 0] if isinstance(data.columns, pd.MultiIndex) else data["Close"]
        open_p = data["Open"].iloc[:, 0] if isinstance(data.columns, pd.MultiIndex) else data["Open"]
        high = data["High"].iloc[:, 0] if isinstance(data.columns, pd.MultiIndex) else data["High"]
        low = data["Low"].iloc[:, 0] if isinstance(data.columns, pd.MultiIndex) else data["Low"]
        volume = data["Volume"].iloc[:, 0] if isinstance(data.columns, pd.MultiIndex) else data["Volume"]

        candles = []
        for i in range(len(data)):
            idx = data.index[i]
            t_val = int(idx.timestamp()) if interval in ["1m", "5m", "15m", "30m", "1h"] else idx.strftime("%Y-%m-%d")
            candles.append({
                "time": t_val,
                "open": round(float(open_p.iloc[i]), 2),
                "high": round(float(high.iloc[i]), 2),
                "low": round(float(low.iloc[i]), 2),
                "close": round(float(close.iloc[i]), 2),
                "volume": int(volume.iloc[i])
            })
        return {"ticker": ticker.upper(), "candles": candles}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))