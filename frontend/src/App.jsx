import React, { useState, useEffect } from 'react';
import CandleChart from './CandleChart';
import StockSearch from './StockSearch';
import AccountView from './AccountView';
import './App.css';

const TIMEFRAMES = [
  { label: '1분', val: '1m' },
  { label: '10분', val: '10m' },
  { label: '30분', val: '30m' },
  { label: '1시간', val: '1h' },
  { label: '3시간', val: '3h' },
  { label: '일봉', val: '1D' },
  { label: '주봉', val: '1W' },
  { label: '월봉', val: '1M' },
];

function App() {
  const [activeTab, setActiveTab] = useState('chart'); // 'chart' | 'account'
  const [selectedTicker, setSelectedTicker] = useState('NVDA');
  const [timeframe, setTimeframe] = useState('1D');
  const [candleData, setCandleData] = useState([]);

  const fetchCandles = async (ticker, tf) => {
    try {
      const res = await fetch(`http://127.0.0.1:8000/api/candles/${ticker}?timeframe=${tf}`);
      const data = await res.json();
      setCandleData(data.candles || []);
    } catch (err) {
      console.error('캔들 로드 실패', err);
    }
  };

  const handleSelectTicker = (ticker) => {
    setSelectedTicker(ticker);
    setActiveTab('chart');
    fetchCandles(ticker, timeframe);
  };

  useEffect(() => {
    fetchCandles(selectedTicker, timeframe);
  }, []);

  return (
    <div className="dashboard-container">
      {/* 1. 상단 내비게이션 바 */}
      <header className="header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 style={{ fontSize: '22px', margin: 0 }}>⚡ QUANT SENTINEL TRADING DESK</h1>
          <p className="subtitle" style={{ margin: '4px 0 0 0', color: '#64748b', fontSize: '13px' }}>
            키움증권 실계좌 연동 & 나스닥 전종목 AI 트레이딩 시스템
          </p>
        </div>

        {/* 탭 전환 버튼 */}
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            onClick={() => setActiveTab('chart')}
            style={{
              padding: '8px 16px',
              borderRadius: '8px',
              border: 'none',
              background: activeTab === 'chart' ? '#0284c7' : '#1e293b',
              color: '#fff',
              fontWeight: 'bold',
              cursor: 'pointer'
            }}
          >
            📈 차트 & 시세 감시
          </button>
          <button
            onClick={() => setActiveTab('account')}
            style={{
              padding: '8px 16px',
              borderRadius: '8px',
              border: 'none',
              background: activeTab === 'account' ? '#0284c7' : '#1e293b',
              color: '#fff',
              fontWeight: 'bold',
              cursor: 'pointer'
            }}
          >
            💼 내 계좌 자산 현황
          </button>
        </div>
      </header>

      {/* 2. 탭별 화면 렌더링 */}
      {activeTab === 'account' ? (
        <section className="section" style={{ marginTop: '20px' }}>
          <AccountView onSelectTicker={handleSelectTicker} />
        </section>
      ) : (
        <section className="section" style={{ marginTop: '20px' }}>
          {/* 차트 상단 컨트롤러: 전종목 검색창 & 타임프레임 */}
          <div className="section-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h2>📊 {selectedTicker} 인터랙티브 캔들 차트</h2>
            <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
              <StockSearch onSelectTicker={handleSelectTicker} currentTicker={selectedTicker} />
            </div>
          </div>

          <div className="timeframe-bar" style={{ marginBottom: '14px' }}>
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf.val}
                className={`tf-btn ${timeframe === tf.val ? 'active' : ''}`}
                onClick={() => {
                  setTimeframe(tf.val);
                  fetchCandles(selectedTicker, tf.val);
                }}
              >
                {tf.label}
              </button>
            ))}
          </div>

          <div className="candle-container">
            <CandleChart data={candleData} ticker={selectedTicker} timeframe={timeframe} />
          </div>
        </section>
      )}
    </div>
  );
}

export default App;