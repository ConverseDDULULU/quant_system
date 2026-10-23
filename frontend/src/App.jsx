import React, { useState, useEffect } from 'react';
import { Activity, ShieldAlert, Cpu, Bot, TrendingUp, Key, Terminal } from 'lucide-react';

export default function App() {
  const [watchlist, setWatchlist] = useState([]);
  const [logs, setLogs] = useState([]);
  const [aiBriefings, setAiBriefings] = useState([]);
  const [apiKey, setApiKey] = useState('');
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [systemStatus, setSystemStatus] = useState('CONNECTING');

  useEffect(() => {
    // 1. Level 1 감시 목록 조회
    fetch('http://127.0.0.1:8000/api/watchlist')
      .then((res) => res.json())
      .then((data) => setWatchlist(data))
      .catch((err) => console.error('Watchlist fetch error:', err));

    // 2. 실시간 웹소켓 연결
    const ws = new WebSocket('ws://127.0.0.1:8000/ws/market');

    ws.onopen = () => {
      setSystemStatus('ONLINE');
      console.log('Quant WebSocket Connected');
    };

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'EXECUTION_SIGNAL') {
        setLogs((prev) => [data.payload, ...prev.slice(0, 19)]);
      } else if (data.type === 'AI_BRIEFING') {
        setAiBriefings((prev) => [data.payload, ...prev.slice(0, 9)]);
      }
    };

    ws.onclose = () => setSystemStatus('DISCONNECTED');
    ws.onerror = () => setSystemStatus('ERROR');

    return () => ws.close();
  }, []);

  const handleSaveKey = async () => {
    if (!apiKey) return;
    try {
      const res = await fetch('http://127.0.0.1:8000/api/save-key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: apiKey }),
      });
      const result = await res.json();
      if (result.success) {
        alert('API 키가 로컬 config/.env에 안전하게 등록되었습니다.');
        setIsModalOpen(false);
        setApiKey('');
      }
    } catch (e) {
      alert('키 등록 중 통신 오류가 발생했습니다.');
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      {/* 상단 네비게이션 헤더 */}
      <header className="border-b border-slate-800 bg-slate-900/60 px-6 py-4 flex items-center justify-between backdrop-blur">
        <div className="flex items-center gap-3">
          <Activity className="h-6 w-6 text-emerald-400" />
          <h1 className="text-xl font-bold tracking-tight text-white">QUANT SENTINEL CORE</h1>
          <span className="text-xs px-2.5 py-0.5 rounded-full bg-slate-800 text-slate-400 border border-slate-700">
            S&P 500 Sub-ms Engine
          </span>
        </div>

        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2 text-sm">
            <span
              className={`h-2.5 w-2.5 rounded-full ${
                systemStatus === 'ONLINE' ? 'bg-emerald-400 animate-pulse' : 'bg-rose-500'
              }`}
            />
            <span className="text-slate-300 font-mono text-xs">{systemStatus}</span>
          </div>

          <button
            onClick={() => setIsModalOpen(true)}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-xs text-slate-200 transition"
          >
            <Key className="h-3.5 w-3.5 text-amber-400" />
            <span>Gemini Key 설정</span>
          </button>
        </div>
      </header>

      {/* 대시보드 본문 레이아웃 */}
      <main className="flex-1 p-6 grid grid-cols-12 gap-6">
        {/* 좌측 영역: 타깃 감시 및 실시간 체결 로그 (8 cols) */}
        <div className="col-span-8 flex flex-col gap-6">
          {/* Level 1 Watchlist 그리드 */}
          <section className="bg-slate-900/50 border border-slate-800 rounded-xl p-5">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <TrendingUp className="h-4 w-4 text-sky-400" />
                <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
                  Level 1 Screening Targets (RSI ≤ 38)
                </h2>
              </div>
              <span className="text-xs font-mono text-slate-400">{watchlist.length} Tickers Tracked</span>
            </div>

            <div className="grid grid-cols-5 gap-3">
              {watchlist.map((item) => (
                <div
                  key={item.ticker}
                  className="bg-slate-950 border border-slate-800/80 hover:border-slate-700 rounded-lg p-3 transition"
                >
                  <div className="flex justify-between items-center mb-1">
                    <span className="font-bold text-sm text-slate-100">{item.ticker}</span>
                    <span className="text-xs font-mono px-1.5 py-0.5 rounded bg-rose-950/60 text-rose-400 border border-rose-900/40">
                      RSI {item.rsi_14}
                    </span>
                  </div>
                  <div className="text-xs text-slate-400 font-mono">
  ${Number(item.close).toFixed(2)}
</div>
                </div>
              ))}
            </div>
          </section>

          {/* Level 2 초저지연 체결 로그 피드 */}
          <section className="flex-1 bg-slate-900/50 border border-slate-800 rounded-xl p-5 flex flex-col">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <Terminal className="h-4 w-4 text-emerald-400" />
                <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
                  Level 2 Ultra-Low Latency Execution Feed
                </h2>
              </div>
              <span className="text-xs text-slate-400 font-mono">&lt; 0.1ms Engine Active</span>
            </div>

            <div className="flex-1 bg-slate-950 border border-slate-800 rounded-lg p-4 font-mono text-xs overflow-y-auto max-h-80 space-y-2">
              {logs.length === 0 ? (
                <div className="text-slate-500 py-6 text-center">실시간 틱 트리거 신호 대기 중...</div>
              ) : (
                logs.map((log, idx) => (
                  <div
                    key={idx}
                    className="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800/50"
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-emerald-400 font-bold">⚡ [BUY TRIGGER]</span>
                      <span className="text-slate-200 font-bold">{log.ticker}</span>
                      <span className="text-slate-400">${log.current_price}</span>
                      <span className="text-emerald-300">+{log.rebound_pct}% 반등</span>
                    </div>
                    <div className="text-slate-400 text-[11px]">지연시간: {log.latency_ms}ms</div>
                  </div>
                ))
              )}
            </div>
          </section>
        </div>

        {/* 우측 영역: Level 3 AI 멘탈 가이드 및 리스크 프로토콜 (4 cols) */}
        <div className="col-span-4 flex flex-col gap-6">
          {/* 리스크 가드 안내 카드 */}
          <section className="bg-slate-900/50 border border-slate-800 rounded-xl p-5">
            <div className="flex items-center gap-2 mb-3">
              <ShieldAlert className="h-4 w-4 text-rose-400" />
              <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
                Disciplined Risk Guard
              </h2>
            </div>
            <p className="text-xs text-slate-400 leading-relaxed">
              감정적 뇌동매매 및 레버리지 추격 차단 프로토콜이 가동 중입니다.
            </p>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {['SOXL', 'TQQQ', 'OKLO', 'NLR'].map((t) => (
                <span
                  key={t}
                  className="text-[11px] font-mono px-2 py-0.5 rounded bg-rose-950/40 text-rose-300 border border-rose-900/50"
                >
                  {t} 동결
                </span>
              ))}
            </div>
          </section>

          {/* Level 3 AI Briefing 스트림 */}
          <section className="flex-1 bg-slate-900/50 border border-slate-800 rounded-xl p-5 flex flex-col">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <Bot className="h-4 w-4 text-purple-400" />
                <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
                  Level 3 Gemini 3.8 Flash Mental Guide
                </h2>
              </div>
              <span className="text-[11px] font-mono text-purple-300">Async Non-blocking</span>
            </div>

            <div className="flex-1 bg-slate-950 border border-slate-800 rounded-lg p-3 space-y-3 overflow-y-auto max-h-[460px]">
              {aiBriefings.length === 0 ? (
                <div className="text-slate-500 py-10 text-center text-xs">
                  트리거 발생 시 Gemini 3.8 Flash 브리핑이 실시간 수신됩니다.
                </div>
              ) : (
                aiBriefings.map((b, idx) => (
                  <div key={idx} className="p-3 rounded-lg bg-slate-900/60 border border-purple-900/30 text-xs">
                    <div className="flex justify-between items-center mb-1.5 border-b border-slate-800 pb-1">
                      <span className="font-bold text-purple-300">{b.ticker} 리스크 가이드</span>
                      <span className="text-[10px] text-slate-400 font-mono">{b.timestamp}</span>
                    </div>
                    <div className="text-slate-300 whitespace-pre-wrap leading-relaxed text-[11px]">
                      {b.content}
                    </div>
                  </div>
                ))
              )}
            </div>
          </section>
        </div>
      </main>

      {/* Gemini API Key 설정 모달 */}
      {isModalOpen && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center p-4 backdrop-blur-sm z-50">
          <div className="bg-slate-900 border border-slate-800 rounded-xl max-w-md w-full p-6 shadow-2xl">
            <h3 className="text-base font-bold text-white mb-2 flex items-center gap-2">
              <Key className="h-4 w-4 text-amber-400" /> Gemini API Key 등록
            </h3>
            <p className="text-xs text-slate-400 mb-4">
              입력된 키는 외부 서버로 전송되지 않고 로컬 config/.env 파일에만 안전하게 보관됩니다.
            </p>
            <input
              type="password"
              placeholder="AIzaSy... 또는 AQ..."
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:outline-none focus:border-emerald-500 mb-4 font-mono"
            />
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setIsModalOpen(false)}
                className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs text-slate-300 transition"
              >
                취소
              </button>
              <button
                onClick={handleSaveKey}
                className="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-xs font-semibold text-white transition"
              >
                저장하기
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}