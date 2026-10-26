import numpy as np
import pandas as pd
from datetime import datetime, timedelta

class QuantBacktester:
    """
    RSI 과매도 반등 + 거래량 돌파 전략 백테스터
    """
    def __init__(self, initial_capital: float = 10000.0):
        self.initial_capital = initial_capital

    def generate_mock_data(self, ticker: str = "NVDA", days: int = 120) -> pd.DataFrame:
        """테스트 및 백테스팅용 가상 나스닥 일봉 데이터 생성"""
        np.random.seed(42)
        dates = [datetime.today() - timedelta(days=i) for i in range(days)]
        dates.reverse()

        base_price = 120.0
        returns = np.random.normal(0.002, 0.03, days)
        price_series = base_price * np.cumprod(1 + returns)

        df = pd.DataFrame({
            "date": dates,
            "close": price_series,
            "volume": np.random.randint(5000000, 25000000, days)
        })

        # RSI(14) 계산
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))

        # 20일 거래량 이동평균
        df["vol_ma20"] = df["volume"].rolling(window=20).mean()
        return df.dropna().reset_index(drop=True)

    def run(self, df: pd.DataFrame) -> dict:
        capital = self.initial_capital
        position = 0
        trades = []
        equity_curve = [capital]
        chart_data = [{
            "date": df.iloc[0]["date"].strftime("%m-%d"),
            "equity": round(capital, 2),
            "drawdown": 0.0
        }]

        for i in range(1, len(df)):
            row = df.iloc[i]

            # 매수 조건: RSI 35 이하 과매도 + 거래량 1.5배 돌파
            if position == 0 and row["rsi"] < 35 and row["volume"] > row["vol_ma20"] * 1.5:
                position = capital / row["close"]
                capital = 0
                entry_price = row["close"]
                entry_date = row["date"]

            # 매도 조건: RSI 70 이상 과매수 진입 시 익절
            elif position > 0 and row["rsi"] > 70:
                capital = position * row["close"]
                ret = (row["close"] - entry_price) / entry_price
                trades.append({
                    "entry_date": entry_date.strftime("%Y-%m-%d"),
                    "exit_date": row["date"].strftime("%Y-%m-%d"),
                    "return": round(ret * 100, 2)
                })
                position = 0

            current_val = capital if position == 0 else position * row["close"]
            equity_curve.append(current_val)

            # 일자별 누적 최고점 및 Drawdown 계산
            peak_so_far = max(equity_curve)
            dd = ((current_val - peak_so_far) / peak_so_far) * 100

            chart_data.append({
                "date": row["date"].strftime("%m-%d"),
                "equity": round(current_val, 2),
                "drawdown": round(dd, 2)
            })

        final_val = equity_curve[-1]
        total_return = ((final_val - self.initial_capital) / self.initial_capital) * 100

        equity_series = pd.Series(equity_curve)
        peak = equity_series.cummax()
        drawdown = (equity_series - peak) / peak
        max_drawdown = drawdown.min() * 100
        win_rate = (len([t for t in trades if t["return"] > 0]) / len(trades) * 100) if trades else 0.0

        return {
            "initial_capital": self.initial_capital,
            "final_value": round(final_val, 2),
            "total_return_pct": round(total_return, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "trade_count": len(trades),
            "win_rate_pct": round(win_rate, 2),
            "trades": trades[-5:],
            "chart_data": chart_data  # 차트용 시계열 데이터 추가
        }

if __name__ == "__main__":
    tester = QuantBacktester(initial_capital=10000)
    data = tester.generate_mock_data("NVDA")
    result = tester.run(data)
    print("=== [BACKTEST RESULT | NVDA] ===")
    for k, v in result.items():
        if k != "trades":
            print(f"{k}: {v}")