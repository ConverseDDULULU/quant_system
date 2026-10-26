import os
import sys
from datetime import datetime
import pytz
import yfinance as yf
from dotenv import load_dotenv
from google import genai

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../config/.env"))

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
tz_kr = pytz.timezone("Asia/Seoul")
now_kr = datetime.now(tz_kr).strftime("%Y-%m-%d %H:%M KST")

universe = ["NVDA", "AAPL", "GOOGL", "TSLA", "AVGO"]

def get_lean_market_context(tickers: list) -> tuple[str, str]:
    """
    불필요한 메타데이터를 제거하고 핵심 팩트(1줄 뉴스 + 핵심 지표)만 압축 추출
    """
    news_lines = []
    metric_lines = []

    for ticker in tickers:
        stock = yf.Ticker(ticker)

        # 1. 뉴스 정제: 제목 + 핵심 요약(최대 120자 슬라이싱)
        try:
            raw_news = stock.news or []
            # 최신 2개 뉴스만 선별
            for item in raw_news[:2]:
                # yfinance 버전에 따른 호환 처리 (dict vs content wrapper)
                content = item.get("content", item)
                title = content.get("title", "").strip()
                summary = content.get("summary", "").strip()
                provider = item.get("publisher", content.get("provider", {}).get("displayName", "News"))

                if title:
                    # 요약이 너무 길면 120자로 컷하여 토큰 낭비 방지
                    short_summary = f" -> {summary[:120]}..." if summary else ""
                    news_lines.append(f"• [{ticker}] {title} ({provider}){short_summary}")
        except Exception:
            pass

        # 2. 필수 가격 지표 압축 (1줄로 요약)
        try:
            hist = stock.history(period="30d")
            if not hist.empty:
                curr = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2]
                pct = ((curr - prev) / prev) * 100
                ma20 = hist['Close'].rolling(20).mean().iloc[-1]
                rsi = 100 - (100 / (1 + (hist['Close'].diff().where(lambda x: x > 0, 0).rolling(14).mean() / 
                                        (-hist['Close'].diff().where(lambda x: x < 0, 0)).rolling(14).mean()))).iloc[-1]
                
                metric_lines.append(
                    f"• {ticker}: 현재가 ${curr:.2f} ({pct:+.2f}%) | 20일선 ${ma20:.2f} | RSI(14) {rsi:.1f}"
                )
        except Exception:
            pass

    return "\n".join(news_lines), "\n".join(metric_lines)

# 데이터 추출
print("📡 yfinance에서 초경량 뉴스 및 지표 추출 중...")
news_feed, metric_feed = get_lean_market_context(universe)

print("\n--- [압축된 주입 데이터 미리보기] ---")
print("[실제 뉴스 팩트]")
print(news_feed)
print("\n[실시간 핵심 지표]")
print(metric_feed)
print("---------------------------------------\n")

# Gemini 프롬프트 구성 (토큰 절약형)
prompt = f"""
당신은 헤지펀드 시니어 퀀트 애널리스트입니다.
아래에 제공된 **실제 외신 뉴스 팩트**와 **계산된 종목 지표**를 융합하여 '나스닥 퀀트 모닝 브리프'를 작성하세요.
절대 수치를 지어내지 말고, 제공된 뉴스 이슈와 기술적 지표(현재가, 20일선, RSI)만을 인용하세요.

[기준 시각: {now_kr}]

[실제 수집된 외신 뉴스 헤드라인]
{news_feed}

[실제 종목 가격 및 지표]
{metric_feed}

[작성 요구사항]
1. 🌐 **핵심 외신 & 섹터 수급 동향**:
   - 수집된 외신 중 반도체/AI/빅테크 주요 호재 및 악재 팩트 요약
2. 📊 **종목별 퀀트 기술적 상태 진단**:
   - 각 종목(NVDA, AAPL, GOOGL, TSLA, AVGO)의 뉴스 영향력과 이평선/RSI 기반 과매수/과매도 평가
3. 🎯 **단기 매매 전략**:
   - 리스크 관리 기준 (현금 비중 및 진입 유의 구간)

전문적이고 압축감 있는 마크다운 개조식(불릿포인트)으로 작성하세요.
"""

print("🧠 Gemini 3.8 Flash에 초저토큰 팩트 기반 요약 요청 중...")

response = client.models.generate_content(
    model="gemini-3.8-flash",
    contents=prompt,
    config={"temperature": 0.2}
)

print("\n" + "=" * 50)
print(response.text)
print("=" * 50)