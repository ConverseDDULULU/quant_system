import React, { useEffect, useState } from "react";

export default function PortfolioSummary() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchPortfolio = async () => {
    try {
      const res = await fetch("http://localhost:8000/api/portfolio");
      if (res.ok) {
        const json = await res.json();
        setData(json);
      }
    } catch (err) {
      console.error("포트폴리오 조회 오류:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchPortfolio();
    const interval = setInterval(fetchPortfolio, 10000); // 10초마다 갱신
    return () => clearInterval(interval);
  }, []);

  if (loading) return <div style={{ color: "#94a3b8", padding: "10px" }}>계좌 잔고 확인 중...</div>;
  if (!data) return null;

  const isProfit = data.total_profit_amt >= 0;

  return (
    <div style={{ marginBottom: "20px" }}>
      {/* 1. 상단 잔고/수익률 요약 카드 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "12px", marginBottom: "15px" }}>
        <div style={cardStyle}>
          <div style={cardLabel}>계좌 번호</div>
          <div style={cardValue}>{data.account_no || "미연결"}</div>
        </div>
        <div style={cardStyle}>
          <div style={cardLabel}>원화 / 외화 예수금</div>
          <div style={cardValue}>
            ₩{data.krw_deposit.toLocaleString()} / ${data.usd_deposit.toLocaleString()}
          </div>
        </div>
        <div style={cardStyle}>
          <div style={cardLabel}>총 평가손익</div>
          <div style={{ ...cardValue, color: isProfit ? "#ef4444" : "#3b82f6" }}>
            {isProfit ? "+" : ""}${data.total_profit_amt.toFixed(2)}
          </div>
        </div>
        <div style={cardStyle}>
          <div style={cardLabel}>총 수익률</div>
          <div style={{ ...cardValue, color: data.account_profit_rate >= 0 ? "#ef4444" : "#3b82f6" }}>
            {data.account_profit_rate >= 0 ? "+" : ""}{data.account_profit_rate}%
          </div>
        </div>
      </div>

      {/* 2. 보유 종목 리스트 테이블 */}
      <div style={{ background: "#111827", borderRadius: "10px", padding: "15px", border: "1px solid #1f2937" }}>
        <h4 style={{ margin: "0 0 10px 0", fontSize: "14px", color: "#e2e8f0" }}>📦 보유 종목 현황</h4>
        {data.holdings.length === 0 ? (
          <div style={{ color: "#6b7280", fontSize: "13px", padding: "10px 0" }}>현재 보유 중인 종목이 없습니다.</div>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px", color: "#f8fafc" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid #374151", color: "#9ca3af", textAlign: "left" }}>
                <th style={{ padding: "8px" }}>종목</th>
                <th style={{ padding: "8px" }}>수량</th>
                <th style={{ padding: "8px" }}>매입단가</th>
                <th style={{ padding: "8px" }}>현재가</th>
                <th style={{ padding: "8px" }}>평가손익</th>
                <th style={{ padding: "8px" }}>수익률</th>
              </tr>
            </thead>
            <tbody>
              {data.holdings.map((stock) => {
                const stockProfit = stock.profit_rate >= 0;
                return (
                  <tr key={stock.ticker} style={{ borderBottom: "1px solid #1f2937" }}>
                    <td style={{ padding: "8px", fontWeight: "bold" }}>
                      <span style={{ color: "#38bdf8" }}>{stock.ticker}</span>
                      <span style={{ fontSize: "11px", color: "#9ca3af", marginLeft: "6px" }}>{stock.name}</span>
                    </td>
                    <td style={{ padding: "8px" }}>{stock.qty}주</td>
                    <td style={{ padding: "8px" }}>${stock.purchase_price.toFixed(2)}</td>
                    <td style={{ padding: "8px" }}>${stock.current_price.toFixed(2)}</td>
                    <td style={{ padding: "8px", color: stockProfit ? "#ef4444" : "#3b82f6" }}>
                      {stockProfit ? "+" : ""}${stock.profit_amt.toFixed(2)}
                    </td>
                    <td style={{ padding: "8px", color: stockProfit ? "#ef4444" : "#3b82f6", fontWeight: "bold" }}>
                      {stockProfit ? "+" : ""}{stock.profit_rate.toFixed(2)}%
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

const cardStyle = {
  background: "#111827",
  padding: "12px 16px",
  borderRadius: "8px",
  border: "1px solid #1f2937",
};

const cardLabel = {
  fontSize: "11px",
  color: "#9ca3af",
  marginBottom: "4px",
};

const cardValue = {
  fontSize: "15px",
  fontWeight: "bold",
  color: "#f8fafc",
};