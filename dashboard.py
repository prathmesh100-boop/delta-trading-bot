@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _ok: bool = Depends(_require_token)):
    return HTMLResponse(content="""
<!DOCTYPE html>
<html>
<head>
<title>Trading Terminal</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { font-family: Arial; background:#0f172a; color:#e2e8f0; padding:20px }
h1 { color:#38bdf8 }
.card { display:inline-block; background:#1e293b; padding:15px; margin:10px; border-radius:10px }
table { width:100%; border-collapse:collapse; margin-top:20px }
th, td { padding:8px; border-bottom:1px solid #334155 }
.green { color:#22c55e }
.red { color:#ef4444 }
button { padding:8px 12px; margin:5px; border:none; border-radius:5px; cursor:pointer }
.buy { background:#22c55e }
.sell { background:#ef4444 }
</style>
</head>

<body>

<h1>📊 Trading Terminal</h1>

<div id="stats"></div>

<h2>📈 Trades</h2>
<table id="trades"></table>

<h2>⚡ Quick Order</h2>
<button class="buy" onclick="placeOrder('buy')">BUY</button>
<button class="sell" onclick="placeOrder('sell')">SELL</button>

<script>

const TOKEN = new URLSearchParams(window.location.search).get("token");

async function loadStats() {
    const res = await fetch('/api/stats?token=' + TOKEN);
    const data = await res.json();

    document.getElementById('stats').innerHTML = `
        <div class="card">PnL: $${data.total_pnl}</div>
        <div class="card">Trades: ${data.total_trades}</div>
        <div class="card">Win Rate: ${data.win_rate}%</div>
    `;
}

async function loadTrades() {
    const res = await fetch('/api/trades?token=' + TOKEN);
    const data = await res.json();

    let html = "<tr><th>Symbol</th><th>Side</th><th>PnL</th></tr>";

    data.trades.forEach(t => {
        html += `
        <tr>
            <td>${t.symbol}</td>
            <td>${t.side}</td>
            <td class="${t.pnl >= 0 ? 'green' : 'red'}">${t.pnl}</td>
        </tr>`;
    });

    document.getElementById('trades').innerHTML = html;
}

async function placeOrder(side) {
    await fetch('/api/order?token=' + TOKEN, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            symbol: "BTC_USDT",
            side: side,
            size_lots: 1
        })
    });
    alert("Order sent");
}

setInterval(() => {
    loadStats();
    loadTrades();
}, 3000);

loadStats();
loadTrades();

</script>

</body>
</html>
""")