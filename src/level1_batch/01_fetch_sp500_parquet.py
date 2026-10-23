"""
Module: 01_fetch_sp500_parquet.py
Purpose: 일봉 시계열 데이터 수집 및 Parquet 포맷 저장 (노트북 경량 테스트 겸용)
Architecture: Level 1 Spark Batch Engine (Data Ingestion)
"""

import os
import sys
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta


def get_target_tickers(fast_mode: bool = True) -> list[str]:
    """
    수집 대상 티커 목록 반환
    fast_mode=True: 노트북/핫스팟 환경용 핵심 우량주 15종목
    fast_mode=False: S&P 500 전 종목 (위키피디아 크롤링)
    """
    if fast_mode:
        print("[MODE] 노트북 경량 모드: 핵심 감시 종목 15개 수집")
        return [
            "NVDA", "AVGO", "AAPL", "MSFT", "AMZN", 
            "GOOGL", "META", "TSLA", "CSCO", "AMD",
            "INTC", "QCOM", "TXN", "IBM", "MU"
        ]

    print("[MODE] S&P 500 전 종목 로드 중...")
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url)
        df_sp500 = tables[0]
        tickers = df_sp500["Symbol"].tolist()
        tickers = [t.replace(".", "-") for t in tickers]
        print(f"[SUCCESS] 총 {len(tickers)}개 티커 수집 완료.")
        return sorted(list(set(tickers)))
    except Exception as e:
        print(f"[WARN] 위키피디아 파싱 실패 ({e}). 기본 티커 적용.")
        return ["NVDA", "AVGO", "AAPL", "MSFT", "CSCO"]


def fetch_and_save_daily_data(
    tickers: list[str],
    output_path: str = "data/us_sp500_daily.parquet",
    period_days: int = 365
) -> None:
    """일봉 데이터 일괄 다운로드 후 Parquet 압축 저장"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=period_days)
    
    print(f"[FETCH] 수집 기간: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
    print(f"[FETCH] yfinance 일괄 다운로드 시작 (총 {len(tickers)}개 종목)...")

    raw_data = yf.download(
        tickers=tickers,
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        group_by="ticker",
        auto_adjust=False,
        threads=True,
        progress=True
    )

    if raw_data.empty:
        raise ValueError("[CRITICAL] 다운로드된 시계열 데이터가 비어 있습니다.")

    print("\n[TRANSFORM] 데이터 스키마 표준화 및 정규화 진행 중...")
    records = []

    for ticker in tickers:
        try:
            if ticker in raw_data.columns.levels[0]:
                df_t = raw_data[ticker].dropna(how="all").reset_index()
                if df_t.empty:
                    continue
                df_t["ticker"] = ticker
                df_t = df_t.rename(columns={
                    "Date": "date",
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume"
                })
                records.append(df_t[["ticker", "date", "open", "high", "low", "close", "volume"]])
        except KeyError:
            continue

    if not records:
        raise ValueError("[CRITICAL] 정규화할 유효 시계열 레코드가 없습니다.")

    final_df = pd.concat(records, ignore_index=True)
    final_df["date"] = pd.to_datetime(final_df["date"]).dt.strftime("%Y-%m-%d")
    final_df["open"] = final_df["open"].astype("float32")
    final_df["high"] = final_df["high"].astype("float32")
    final_df["low"] = final_df["low"].astype("float32")
    final_df["close"] = final_df["close"].astype("float32")
    final_df["volume"] = final_df["volume"].fillna(0).astype("int64")
    
    # 이상치 정제 (0원 이하 데이터 필터링)
    final_df = final_df[final_df["close"] > 0]

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    print(f"[SAVE] Parquet 저장 중: {output_path} (Snappy 압축)")
    final_df.to_parquet(output_path, engine="pyarrow", compression="snappy", index=False)
    
    print("=" * 60)
    print(f"[COMPLETE] 원장 저장 완료!")
    print(f"- 저장 경로: {output_path}")
    print(f"- 총 레코드 행 수: {len(final_df):,}건")
    print(f"- 수집 종목 수: {final_df['ticker'].nunique()}개")
    print("=" * 60)


if __name__ == "__main__":
    # fast_mode=True로 노트북에서 10초 만에 테스트 완료
    target_tickers = get_target_tickers(fast_mode=True)
    fetch_and_save_daily_data(target_tickers)