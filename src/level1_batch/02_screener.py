"""
Module: 02_screener.py
Purpose: Parquet 시계열 데이터 기반 Wilder's Smoothed RSI(14) 연산 및 감시 타깃 추출
Architecture: Level 1 Batch Engine (Screening Layer)
"""

import os
import json
import pandas as pd
import numpy as np


def load_parquet_data(file_path: str = "data/us_sp500_daily.parquet") -> pd.DataFrame:
    """Parquet 원장 로드 및 기본 유효성 검증"""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"[ERROR] 원장 파일이 없습니다: {file_path}")
    
    df = pd.read_parquet(file_path)
    required = {"ticker", "date", "open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        raise ValueError(f"[ERROR] 필수 컬럼 누락: {required - set(df.columns)}")
    
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(by=["ticker", "date"]).reset_index(drop=True)
    return df


def calculate_wilders_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's Smoothing 방식을 적용한 표준 14일 RSI 연산
    (TradingView 및 HTS 기술지표 표준 공식)
    """
    delta = series.diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    gain_series = pd.Series(gain, index=series.index)
    loss_series = pd.Series(loss, index=series.index)

    # 초기 period 기간의 단순 평균
    avg_gain = gain_series.rolling(window=period, min_periods=period).mean()
    avg_loss = loss_series.rolling(window=period, min_periods=period).mean()

    # Wilder 지수평활 (alpha = 1 / period)
    for i in range(period, len(series)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss[i]) / period

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def run_screening(df: pd.DataFrame, rsi_threshold: float = 40.0) -> pd.DataFrame:
    """
    전 종목 RSI 및 일일 거래대금(Dollar Volume) 연산
    감정적 물타기 방지(동결 종목 배제) 가드 적용
    """
    results = []

    # 종목별 그룹화 후 개별 시계열 연산
    for ticker, group in df.groupby("ticker"):
        group = group.sort_values(by="date").copy()
        if len(group) < 15:
            continue

        group["rsi_14"] = calculate_wilders_rsi(group["close"], period=14)
        group["dollar_volume"] = group["close"] * group["volume"]
        
        # 가장 최근 거래일의 스냅샷 추출
        latest_row = group.iloc[-1]
        results.append({
            "ticker": ticker,
            "date": latest_row["date"].strftime("%Y-%m-%d"),
            "close": float(latest_row["close"]),
            "rsi_14": round(float(latest_row["rsi_14"]), 2),
            "dollar_volume": float(latest_row["dollar_volume"])
        })

    screened_df = pd.DataFrame(results)

    # [리스크 관리 가드] 동결 자산 (물타기 금지 대상) 원천 필터링
    FROZEN_TICKERS = {"SOXL", "TQQQ", "OKLO", "NLR"}
    screened_df = screened_df[~screened_df["ticker"].isin(FROZEN_TICKERS)]

    # RSI 오름차순 정렬 (과매도 강도가 높은 순)
    screened_df = screened_df.sort_values(by="rsi_14", ascending=True).reset_index(drop=True)
    return screened_df


def main():
    print("[PROCESS] Parquet 데이터 로드 및 시계열 분석 시작...")
    df = load_parquet_data()
    
    screened = run_screening(df)
    
    print("\n" + "=" * 65)
    print(f" [Level 1 Batch Screener] 전 종목 최신 RSI 현황 (총 {len(screened)}종목)")
    print("=" * 65)
    # 거래대금 단위 가독성 포맷 (백만 달러 단위)
    display_df = screened.copy()
    display_df["dollar_vol(M$)"] = (display_df["dollar_volume"] / 1_000_000).round(1)
    print(display_df[["ticker", "date", "close", "rsi_14", "dollar_vol(M$)"]].to_string(index=False))

    # Level 2 초저지연 트리거 엔진이 즉시 읽을 수 있는 워치리스트 JSON 저장
    output_target_json = "data/watchlist_targets.json"
    targets = screened.to_dict(orient="records")
    
    with open(output_target_json, "w", encoding="utf-8") as f:
        json.dump(targets, f, indent=2, ensure_ascii=False)
        
    print(f"\n[OUTPUT] Level 2 트리거 엔진용 감시 타깃 저장 완료: {output_target_json}")


if __name__ == "__main__":
    main()