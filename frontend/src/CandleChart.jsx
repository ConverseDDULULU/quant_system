import React, { useEffect, useRef, useState } from "react";
import { createChart, CandlestickSeries } from "lightweight-charts";

export default function CandleChart({ ticker = "NVDA", timeframe = "1D" }) {
  const chartContainerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const [hoverData, setHoverData] = useState(null);

  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      width: chartContainerRef.current.clientWidth,
      height: 480,
      layout: { background: { color: "#131722" }, textColor: "#d1d4dc" },
      grid: { vertLines: { color: "#1f2434" }, horzLines: { color: "#1f2434" } },
      crosshair: { mode: 1 },
      timeScale: { borderColor: "#2B2B43", timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: "#2B2B43" },
    });

    const seriesOptions = {
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: false,
      wickUpColor: "#26a69a",
      wickDownColor: "#ef5350",
    };

    let candlestickSeries;
    if (typeof chart.addCandlestickSeries === "function") {
      candlestickSeries = chart.addCandlestickSeries(seriesOptions);
    } else if (typeof chart.addSeries === "function" && CandlestickSeries) {
      candlestickSeries = chart.addSeries(CandlestickSeries, seriesOptions);
    }

    chartRef.current = chart;
    seriesRef.current = candlestickSeries;

    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.point || param.point.x < 0 || param.point.x > chartContainerRef.current.clientWidth) {
        setHoverData(null);
        return;
      }

      const candle = candlestickSeries ? param.seriesData.get(candlestickSeries) : null;
      if (candle) {
        let formattedDate = "";
        if (typeof param.time === "number") {
          const d = new Date(param.time * 1000);
          formattedDate = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
        } else if (typeof param.time === "object") {
          formattedDate = `${param.time.year}-${String(param.time.month).padStart(2, "0")}-${String(param.time.day).padStart(2, "0")}`;
        } else {
          formattedDate = String(param.time);
        }

        const open = candle.open;
        const close = candle.close;
        const changeRate = (((close - open) / open) * 100).toFixed(2);

        setHoverData({
          time: formattedDate,
          open: candle.open.toFixed(2),
          high: candle.high.toFixed(2),
          low: candle.low.toFixed(2),
          close: candle.close.toFixed(2),
          changeRate: changeRate > 0 ? `+${changeRate}%` : `${changeRate}%`,
          isUp: close >= open,
        });
      }
    });

    const handleResize = () => {
      if (chartContainerRef.current && chartRef.current) {
        chartRef.current.applyOptions({ width: chartContainerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, []);

  useEffect(() => {
    if (!ticker) return;

    fetch(`http://localhost:8000/api/candles/${ticker}?timeframe=${timeframe}`)
      .then((res) => {
        if (!res.ok) throw new Error("데이터 조회 실패");
        return res.json();
      })
      .then((resData) => {
        if (resData.candles && seriesRef.current) {
          seriesRef.current.setData(resData.candles);
          if (chartRef.current) {
            chartRef.current.timeScale().fitContent();
          }
        }
      })
      .catch((err) => console.error("캔들 로드 실패:", err));
  }, [ticker, timeframe]);

  return (
    <div style={{ position: "relative", width: "100%", background: "#131722", borderRadius: "8px", overflow: "hidden" }}>
      <div
        style={{
          position: "absolute",
          top: "12px",
          left: "16px",
          zIndex: 10,
          background: "rgba(19, 23, 34, 0.85)",
          padding: "6px 12px",
          borderRadius: "6px",
          border: "1px solid #2B2B43",
          display: "flex",
          gap: "12px",
          fontSize: "13px",
          fontFamily: "monospace",
          pointerEvents: "none",
        }}
      >
        <span style={{ fontWeight: "bold", color: "#38bdf8" }}>{ticker}</span>
        {hoverData ? (
          <>
            <span style={{ color: "#848e9c" }}>{hoverData.time}</span>
            <span>시: <strong style={{ color: "#d1d4dc" }}>${hoverData.open}</strong></span>
            <span>고: <strong style={{ color: "#26a69a" }}>${hoverData.high}</strong></span>
            <span>저: <strong style={{ color: "#ef5350" }}>${hoverData.low}</strong></span>
            <span>종: <strong style={{ color: hoverData.isUp ? "#26a69a" : "#ef5350" }}>${hoverData.close}</strong></span>
            <span style={{ color: hoverData.isUp ? "#26a69a" : "#ef5350", fontWeight: "bold" }}>
              ({hoverData.changeRate})
            </span>
          </>
        ) : (
          <span style={{ color: "#5d6588" }}>캔들 위에 마우스를 올리면 가격 정보가 표시됩니다</span>
        )}
      </div>
      <div ref={chartContainerRef} style={{ width: "100%", height: "480px" }} />
    </div>
  );
}