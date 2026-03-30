@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _ok: bool = Depends(_require_token)):
        return HTMLResponse(content="""
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Trading Terminal</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root{--bg:#0b1220;--card:#0f1724;--muted:#94a3b8;--accent:#60a5fa;--success:#22c55e;--danger:#ef4444}
        body{font-family:Inter,system-ui,Arial,sans-serif;margin:0;background:linear-gradient(180deg,#071020 0%,#081423 100%);color:#e6eef8}
        .container{max-width:1200px;margin:20px auto;padding:18px}
        header{display:flex;align-items:center;justify-content:space-between}
        h1{margin:0;font-weight:700;color:var(--accent)}
        .row{display:flex;gap:12px;margin-top:14px}
        .card{background:var(--card);padding:14px;border-radius:10px;box-shadow:0 6px 18px rgba(2,6,23,0.6)}
        .card.small{padding:10px;min-width:140px}
        .stats{display:flex;gap:10px}
        table{width:100%;border-collapse:collapse;margin-top:12px}
        th,td{padding:10px;border-bottom:1px solid rgba(255,255,255,0.04);text-align:left}
        .green{color:var(--success)}.red{color:var(--danger)}
        .controls{display:flex;gap:8px;align-items:center}
        input,select{padding:8px;border-radius:6px;border:1px solid rgba(255,255,255,0.06);background:transparent;color:inherit}
        button{background:var(--accent);color:#06202d;border:none;padding:8px 12px;border-radius:6px;cursor:pointer}
        .col{flex:1}
        .col.small{flex:0 0 340px}
        .muted{color:var(--muted);font-size:13px}
        .chart-wrap{height:220px}
        .section-title{display:flex;align-items:center;justify-content:space-between}
    </style>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <div class="container">
        <header>
            <h1>🚀 Trading Terminal</h1>
            <div class="muted">Live · <span id="last-update">-</span></div>
        </header>

        <div class="row">
            <div class="col">
                <div class="card stats" id="stats-cards"></div>

                <div class="card" style="margin-top:12px">
                    <div class="section-title"><strong>PnL History</strong><span class="muted">Last 50 trades</span></div>
                    <div class="chart-wrap"><canvas id="pnlChart"></canvas></div>
                </div>

                <div class="card" style="margin-top:12px">
                    <div class="section-title"><strong>Recent Trades</strong><span class="muted">Realtime</span></div>
                    <table id="trades-table"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Size</th><th>PnL</th></tr></thead><tbody></tbody></table>
                </div>
            </div>

            <div class="col small">
                <div class="card">
                    <strong>Quick Order</strong>
                    <div style="margin-top:8px" class="controls">
                        <input id="order-symbol" placeholder="Symbol (e.g. BTC_USDT)" value="BTC_USDT">
                        <select id="order-side"><option value="buy">BUY</option><option value="sell">SELL</option></select>
                    </div>
                    <div style="margin-top:8px" class="controls">
                        <input id="order-size" type="number" min="1" value="1">
                        <button id="order-send">Send</button>
                    </div>
                    <div id="order-res" class="muted" style="margin-top:8px"></div>
                </div>

                <div class="card" style="margin-top:12px">
                    <strong>Positions</strong>
                    <div id="positions" class="muted" style="margin-top:8px">Loading…</div>
                </div>

                <div class="card" style="margin-top:12px">
                    <strong>Logs</strong>
                    <pre id="logs" style="max-height:220px;overflow:auto;margin:8px 0 0 0;white-space:pre-wrap" class="muted">Loading…</pre>
                </div>
            </div>
        </div>
    </div>

    <script>
        const TOKEN = new URLSearchParams(window.location.search).get('token') || '';
        const headers = TOKEN ? { 'x-dashboard-token': TOKEN } : {};

        const pnlCtx = document.getElementById('pnlChart').getContext('2d');
        const pnlChart = new Chart(pnlCtx, {
            type: 'bar',
            data: { labels: [], datasets: [{ label: 'PnL', data: [], backgroundColor: [] }] },
            options: { animation:false, plugins:{legend:{display:false}}, scales:{x:{display:false}} }
        });

        function updateStatsUI(s) {
            document.getElementById('stats-cards').innerHTML = `
                <div class="card small"><div class="muted">Total PnL</div><div style="font-weight:700">$${s.total_pnl}</div></div>
                <div class="card small"><div class="muted">Trades</div><div style="font-weight:700">${s.total_trades}</div></div>
                <div class="card small"><div class="muted">Win Rate</div><div style="font-weight:700">${s.win_rate}%</div></div>
            `;
            document.getElementById('last-update').textContent = new Date(s.last_update_ts * 1000).toLocaleString();
        }

        function renderTrades(trades) {
            const tbody = document.querySelector('#trades-table tbody');
            tbody.innerHTML = '';
            trades.forEach(t => {
                const tr = document.createElement('tr');
                tr.innerHTML = `<td>${t.entry_time || t.exit_time || ''}</td><td>${t.symbol}</td><td>${t.side}</td><td>${t.size_lots || t.size || ''}</td><td class='${t.pnl>=0?"green":"red"}'>${t.pnl}</td>`;
                tbody.prepend(tr);
            });
        }

        function updateChart(trades) {
            const labels = trades.map((t,i)=> i+1);
            const data = trades.map(t => Number(t.pnl || 0));
            pnlChart.data.labels = labels;
            pnlChart.data.datasets[0].data = data;
            pnlChart.data.datasets[0].backgroundColor = data.map(v=> v>=0? 'rgba(34,197,94,0.8)':'rgba(239,68,68,0.8)');
            pnlChart.update();
        }

        async function fetchPositions(){
            try{ const res = await fetch('/api/positions', { headers }); if(res.ok){ const j=await res.json(); document.getElementById('positions').textContent = JSON.stringify(j.positions,null,2);} }
            catch(e){ document.getElementById('positions').textContent = 'Error'; }
        }

        async function fetchLogs(){ const res = await fetch('/api/logs', { headers }); if(res.ok){ const j=await res.json(); document.getElementById('logs').textContent = j.lines.join(''); } }

        document.getElementById('order-send').addEventListener('click', async ()=>{
            const symbol = document.getElementById('order-symbol').value;
            const side = document.getElementById('order-side').value;
            const size = Number(document.getElementById('order-size').value)||1;
            const res = await fetch('/api/order', { method:'POST', headers: Object.assign({'Content-Type':'application/json'}, headers), body: JSON.stringify({ symbol, side, size_lots: size }) });
            const txt = await res.text(); document.getElementById('order-res').textContent = txt;
            setTimeout(()=>fetchPositions(),500);
        });

        // SSE stream for live updates
        const src = new EventSource('/stream' + (TOKEN? ('?token='+TOKEN):''));
        src.onmessage = (ev)=>{
            try{
                const d = JSON.parse(ev.data);
                updateStatsUI(d.stats);
                renderTrades(d.recent || []);
                updateChart(d.recent || []);
                fetchPositions(); fetchLogs();
            }catch(e){ console.error(e); }
        };
        src.onerror = ()=>{ console.warn('SSE error'); };

        // initial fetch
        (async ()=>{ const r = await fetch('/api/stats', { headers }); if(r.ok){ updateStatsUI(await r.json()); } const t = await fetch('/api/trades', { headers }); if(t.ok){ const jt=await t.json(); renderTrades(jt.trades); updateChart(jt.trades); } fetchPositions(); fetchLogs(); })();
    </script>
</body>
</html>
""")