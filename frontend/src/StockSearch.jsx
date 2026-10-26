import React, { useState, useEffect, useRef } from "react";

export default function StockSearch({ onSelectTicker, currentTicker }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef(null);

  useEffect(() => {
    if (query.trim().length === 0) {
      setResults([]);
      setIsOpen(false);
      return;
    }

    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`http://localhost:8000/api/search?q=${encodeURIComponent(query)}`);
        if (res.ok) {
          const data = await res.json();
          setResults(data);
          setIsOpen(data.length > 0);
        }
      } catch (err) {
        console.error("검색 오류:", err);
      }
    }, 200);

    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    function handleClickOutside(e) {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const handleSelect = (ticker) => {
    onSelectTicker(ticker);
    setQuery("");
    setIsOpen(false);
  };

  return (
    <div ref={containerRef} style={{ position: "relative", minWidth: "240px" }}>
      <div style={{ display: "flex", alignItems: "center", background: "#1e293b", borderRadius: "8px", padding: "4px 10px", border: "1px solid #334155" }}>
        <span style={{ marginRight: "6px", color: "#94a3b8" }}>🔍</span>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={`종목 검색 (현재: ${currentTicker})`}
          style={{
            background: "transparent",
            border: "none",
            outline: "none",
            color: "#f8fafc",
            fontSize: "13px",
            width: "100%",
          }}
        />
        {query && (
          <button
            onClick={() => setQuery("")}
            style={{ background: "none", border: "none", color: "#64748b", cursor: "pointer", fontSize: "12px" }}
          >
            ✕
          </button>
        )}
      </div>

      {isOpen && (
        <ul
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            right: 0,
            zIndex: 1000,
            background: "#0f172a",
            border: "1px solid #334155",
            borderRadius: "8px",
            marginTop: "6px",
            maxHeight: "260px",
            overflowY: "auto",
            listStyle: "none",
            padding: "4px 0",
            boxShadow: "0 10px 25px -5px rgba(0, 0, 0, 0.5)",
          }}
        >
          {results.map((item) => (
            <li
              key={item.ticker}
              onClick={() => handleSelect(item.ticker)}
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                padding: "8px 12px",
                cursor: "pointer",
                borderBottom: "1px solid #1e293b",
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = "#1e293b")}
              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
            >
              <div>
                <span style={{ fontWeight: "bold", color: "#38bdf8", marginRight: "8px" }}>{item.ticker}</span>
                <span style={{ fontSize: "12px", color: "#e2e8f0" }}>{item.name_kr}</span>
              </div>
              <span style={{ fontSize: "11px", color: "#64748b", background: "#1e293b", padding: "2px 6px", borderRadius: "4px" }}>
                {item.market}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
