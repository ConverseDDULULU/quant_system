import React, { useState, useEffect } from "react";

export default function AccountView({ onSelectTicker }) {
  const [acct, setAcct] = useState(null);
  const [loading, setLoading] = useState(true);

  const loadAccount = async () => {
    try {
      const res = await fetch("http://localhost:8000/api/account/details");
      if (res.ok) {
        const data = await res.json();
        setAcct(data);
      }
    } catch (e) {
      console.error("계좌 조회 실패", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAccount();
    const timer = setInterval(loadAccount, 10000);
    return () => clearInterval(timer);
  }, []);

  if (loading) return <div style={{ padding: "40px", color: "#94a3b8" }}>키움 실서버 원장 동기화 중...</div>;
  if (!acct) return <div style={{ padding: "40px", color: "#ef4444" }}>키움 계좌 정보를 불러올 수 없습니다.</div>;

  // 상단 총 손익 및 수익률 안전 추출
  const totalProfit = Number(acct.total_profit_usd ?? acct.total_pl_usd ?? acct.profit_amt ?? 0);
  const totalRate = Number(acct.total_return_rate ?? acct.total_profit_rate ?? acct.total_pl_rate ?? 0);
  const isProfit = totalProfit >= 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
      {/* 1. 상단 총 자산 요약 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: "16px" }}>
        <div style={cardBox}>
          <span style={cardSub}>키움 실계좌 번호</span>
          <span style={cardMain}>{acct.account_no || "-"}</span>
        </div>
        <div style={cardBox}>
          <span style={cardSub}>원화 예수금 / 외화(USD)</span>
          <span style={cardMain}>
            ₩{(acct.krw_deposit ?? 0).toLocaleString()} / ${(acct.usd_deposit ?? 0).toLocaleString()}
          </span>
        </div>
        <div style={cardBox}>
          <span style={cardSub}>총 평가금액 (보유주식)</span>
          <span style={cardMain}>${(acct.total_eval_usd ?? 0).toLocaleString()}</span>
        </div>
        <div style={cardBox}>
          <span style={cardSub}>총 평가손익 (수익률)</span>
          <span style={{ ...cardMain, color: isProfit ? "#ef4444" : "#3b82f6" }}>
            {isProfit ? "+" : ""}${totalProfit.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ({isProfit ? "+" : ""}{totalRate.toFixed(2)}%)
          </span>
        </div>
      </div>

      {/* 2. 보유 종목 리스트 테이블 */}
      <div style={{ background: "#111827", borderRadius: "12px", border: "1px solid #1f2937", padding: "20px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
          <h3 style={{ margin: 0, fontSize: "16px", color: "#f8fafc" }}>
            📋 보유 종목 상세 내역 ({acct.holdings_count ?? acct.holdings?.length ?? 0}개 종목)
          </h3>
          <button onClick={loadAccount} style={refreshBtn}>새로고침</button>
        </div>

        {(!acct.holdings || acct.holdings.length === 0) ? (
          <div style={{ textAlign: "center", padding: "40px", color: "#64748b" }}>
            현재 키움 계좌에 보유 중인 미국주식이 없습니다.
          </div>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "14px", color: "#e2e8f0" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid #374151", color: "#94a3b8", textAlign: "left" }}>
                <th style={{ padding: "10px" }}>종목코드/종목명</th>
                <th style={{ padding: "10px" }}>보유수량</th>
                <th style={{ padding: "10px" }}>평균단가</th>
                <th style={{ padding: "10px" }}>현재가</th>
                <th style={{ padding: "10px" }}>평가금액</th>
                <th style={{ padding: "10px" }}>평가손익</th>
                <th style={{ padding: "10px" }}>수익률</th>
                <th style={{ padding: "10px", textAlign: "center" }}>차트보기</th>
              </tr>
            </thead>
            <tbody>
              {acct.holdings.map((s) => {
                const pRate = Number(s.profit_rate ?? s.pl_rate ?? 0);
                const sProf = pRate >= 0;
                const purchasePrice = Number(s.purchase_price ?? s.buy_price ?? 0);
                const currentPrice = Number(s.current_price ?? 0);
                const evalAmt = Number(s.eval_amount ?? 0);
                const profitAmt = Number(s.profit_amt ?? s.pl_amount ?? 0);
                const quantity = s.qty ?? s.quantity ?? 0;

                return (
                  <tr key={s.ticker} style={{ borderBottom: "1px solid #1e293b" }}>
                    <td style={{ padding: "12px 10px", fontWeight: "bold" }}>
                      <span style={{ color: "#38bdf8" }}>{s.ticker}</span>
                      <span style={{ fontSize: "12px", color: "#94a3b8", marginLeft: "8px" }}>{s.name}</span>
                    </td>
                    <td style={{ padding: "10px" }}>{quantity}주</td>
                    <td style={{ padding: "10px" }}>${purchasePrice.toFixed(2)}</td>
                    <td style={{ padding: "10px" }}>${currentPrice.toFixed(2)}</td>
                    <td style={{ padding: "10px" }}>${evalAmt.toLocaleString()}</td>
                    <td style={{ padding: "10px", color: sProf ? "#ef4444" : "#3b82f6" }}>
                      {sProf ? "+" : ""}${profitAmt.toFixed(2)}
                    </td>
                    <td style={{ padding: "10px", fontWeight: "bold", color: sProf ? "#ef4444" : "#3b82f6" }}>
                      {sProf ? "+" : ""}{pRate.toFixed(2)}%
                    </td>
                    <td style={{ padding: "10px", textAlign: "center" }}>
                      <button
                        onClick={() => onSelectTicker && onSelectTicker(s.ticker)}
                        style={chartViewBtn}
                      >
                        차트보기
                      </button>
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

const cardBox = {
  background: "#111827",
  padding: "16px 20px",
  borderRadius: "10px",
  border: "1px solid #1f2937",
  display: "flex",
  flexDirection: "column",
  gap: "6px",
};
const cardSub = { fontSize: "12px", color: "#94a3b8" };
const cardMain = { fontSize: "17px", fontWeight: "bold", color: "#f8fafc" };
const refreshBtn = {
  background: "#1e293b",
  border: "1px solid #334155",
  color: "#94a3b8",
  padding: "4px 10px",
  borderRadius: "6px",
  cursor: "pointer",
  fontSize: "12px",
};
const chartViewBtn = {
  background: "#0284c7",
  border: "none",
  color: "#fff",
  padding: "4px 8px",
  borderRadius: "4px",
  cursor: "pointer",
  fontSize: "12px",
};