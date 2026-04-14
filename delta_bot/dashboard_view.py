from __future__ import annotations


def render_dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Delta Bot Ops Dashboard</title>
  <style>
    :root{
      --bg:#08111f;--bg2:#0b1730;--panel:rgba(8,20,39,.88);--panel2:rgba(10,27,52,.94);
      --line:rgba(145,189,255,.14);--txt:#eef5ff;--muted:#96a9c8;--cyan:#58d6c2;
      --amber:#f5b35c;--green:#4ade80;--red:#fb7185;--blue:#6ea8ff
    }
    *{box-sizing:border-box} body{margin:0;color:var(--txt);font-family:"Segoe UI","Trebuchet MS",sans-serif;
      background:
        radial-gradient(circle at 0% 0%, rgba(88,214,194,.17), transparent 28%),
        radial-gradient(circle at 100% 0%, rgba(245,179,92,.18), transparent 26%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg2) 100%);
      min-height:100vh}
    .page{width:min(1460px,calc(100vw - 20px));margin:0 auto;padding:18px 0 28px}
    .hero,.main,.stats,.twocol{display:grid;gap:16px}
    .hero{grid-template-columns:1.6fr 1fr}
    .main{grid-template-columns:1.35fr 1fr;align-items:start}
    .stats{grid-template-columns:repeat(4,1fr)}
    .twocol{grid-template-columns:repeat(2,1fr)}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:18px;backdrop-filter:blur(14px);box-shadow:0 24px 70px rgba(0,0,0,.28)}
    .title{display:flex;justify-content:space-between;gap:14px;align-items:flex-start}
    h1,h2,p{margin:0} h1{font-size:clamp(1.8rem,2vw,2.5rem);letter-spacing:.02em} h2{font-size:1rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:14px}
    .subtle,.muted{color:var(--muted)} .pill{display:inline-flex;padding:7px 11px;border-radius:999px;background:rgba(255,255,255,.07);font-size:.86rem}
    .controls{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
    select,input,button{font:inherit;border-radius:12px;padding:10px 14px;color:var(--txt);background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.12)}
    input{min-width:180px} button{cursor:pointer;background:linear-gradient(135deg, rgba(88,214,194,.22), rgba(110,168,255,.2))}
    .metric{padding:16px;border-radius:18px;background:var(--panel2);border:1px solid var(--line)}
    .metric .label{font-size:.88rem;color:var(--muted);margin-bottom:8px}.metric .value{font-size:clamp(1.2rem,1.7vw,2rem);font-weight:700}
    .good{color:var(--green)} .bad{color:var(--red)} .warn{color:var(--amber)} .info{color:var(--blue)}
    .list{display:grid;gap:10px;max-height:520px;overflow:auto}
    .item{padding:12px;border-radius:15px;background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.08)}
    .item .head{display:flex;justify-content:space-between;gap:8px;margin-bottom:6px;font-size:.87rem;color:var(--muted)}
    table{width:100%;border-collapse:collapse;font-size:.93rem}
    th,td{padding:10px 8px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left;vertical-align:top}
    th{font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
    .chart{height:310px;border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:12px;background:linear-gradient(180deg, rgba(255,255,255,.035), rgba(255,255,255,.01))}
    canvas{width:100%;height:100%;display:block}
    .errors{display:grid;gap:10px;margin-top:12px}.error{padding:12px 14px;border-radius:14px;background:rgba(251,113,133,.12);border:1px solid rgba(251,113,133,.25);color:#ffe8ed}
    @media(max-width:1080px){.hero,.main,.stats,.twocol{grid-template-columns:1fr}.page{width:min(100vw - 12px,1460px)}}
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <div class="card">
        <div class="title">
          <div>
            <p class="subtle">Phase 2 operational UI</p>
            <h1>Delta Bot Control Center</h1>
            <p class="subtle" id="headline">Loading portfolio, execution, and risk telemetry...</p>
          </div>
          <div class="pill" id="statePill">Connecting</div>
        </div>
        <div class="controls">
          <input id="symbolInput" value="ETH_USDT" />
          <button id="refreshBtn" type="button">Refresh</button>
        </div>
        <div id="errors" class="errors"></div>
      </div>
      <div class="card">
        <h2>System Health</h2>
        <div class="stats">
          <div class="metric"><div class="label">API</div><div class="value" id="apiConfigured">--</div></div>
          <div class="metric"><div class="label">Kill Switch</div><div class="value" id="killSwitch">--</div></div>
          <div class="metric"><div class="label">Open Positions</div><div class="value" id="openPositions">--</div></div>
          <div class="metric"><div class="label">Recent Errors</div><div class="value" id="errorCount">--</div></div>
        </div>
      </div>
    </section>
    <section class="stats" style="margin:16px 0">
      <div class="metric"><div class="label">Portfolio Equity</div><div class="value" id="equityValue">--</div></div>
      <div class="metric"><div class="label">Drawdown</div><div class="value" id="drawdownValue">--</div></div>
      <div class="metric"><div class="label">Daily Loss</div><div class="value" id="dailyLossValue">--</div></div>
      <div class="metric"><div class="label">Open Risk / Exposure</div><div class="value" id="exposureValue">--</div></div>
    </section>
    <section class="main">
      <div>
        <div class="card">
          <h2>Portfolio Curve</h2>
          <div class="chart"><canvas id="equityChart" width="1000" height="320"></canvas></div>
          <p class="muted" id="curveMeta" style="margin-top:10px"></p>
        </div>
        <div class="twocol" style="margin-top:16px">
          <div class="card"><h2>Positions</h2><div id="positionsTable"></div></div>
          <div class="card"><h2>Orders</h2><div id="ordersTable"></div></div>
          <div class="card"><h2>Recent Trades</h2><div id="tradesTable"></div></div>
          <div class="card"><h2>Runtime State</h2><div id="runtimeTable"></div></div>
        </div>
      </div>
      <div style="display:grid;gap:16px">
        <div class="card">
          <h2>Trade Summary</h2>
          <div class="stats">
            <div class="metric"><div class="label">Closed Trades</div><div class="value" id="closedTrades">--</div></div>
            <div class="metric"><div class="label">Win Rate</div><div class="value" id="winRate">--</div></div>
            <div class="metric"><div class="label">Realized PnL</div><div class="value" id="realizedPnl">--</div></div>
            <div class="metric"><div class="label">Best Setup</div><div class="value" id="bestSetup">--</div></div>
          </div>
        </div>
        <div class="card"><h2>Risk Events</h2><div id="riskEvents" class="list"></div></div>
        <div class="card"><h2>Execution & Errors</h2><div id="errorEvents" class="list"></div></div>
      </div>
    </section>
  </div>
  <script>
    const state={symbol:"ETH_USDT"};
    const $=id=>document.getElementById(id);
    const fmt=(v,d=2)=>Number.isFinite(Number(v))?new Intl.NumberFormat("en-US",{minimumFractionDigits:d,maximumFractionDigits:d}).format(Number(v)):"--";
    const pct=v=>Number.isFinite(Number(v))?`${fmt(Number(v)*100,2)}%`:"--";
    const signed=(v,d=2)=>Number.isFinite(Number(v))?`${Number(v)>=0?"+":""}${fmt(v,d)}`:"--";
    const table=(headers,rows)=>rows.length?`<table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`:`<p class="muted">No data available.</p>`;
    function eventList(id,items){
      $(id).innerHTML=items.length?items.map(item=>`<div class="item"><div class="head"><span>${item.event_type}</span><span>${new Date(item.event_time).toLocaleString()}</span></div><div>${item.symbol||"portfolio"} · <span class="${item.severity==='error'?'bad':item.severity==='warning'?'warn':'info'}">${item.severity}</span></div><div class="muted">${JSON.stringify(item.payload)}</div></div>`).join(""):`<p class="muted">No events recorded.</p>`;
    }
    function drawCurve(points){
      const canvas=$("equityChart"),ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;
      ctx.clearRect(0,0,w,h);
      if(!points.length){ctx.fillStyle="#96a9c8";ctx.font="18px Segoe UI";ctx.fillText("No portfolio history yet",24,44);return;}
      const vals=points.map(p=>Number(p.current_equity||0)); const min=Math.min(...vals), max=Math.max(...vals), range=max-min||1;
      const padX=18,padY=18,innerW=w-padX*2,innerH=h-padY*2;
      ctx.strokeStyle="rgba(255,255,255,.08)";
      for(let i=0;i<5;i++){const y=padY+innerH*(i/4); ctx.beginPath(); ctx.moveTo(padX,y); ctx.lineTo(w-padX,y); ctx.stroke();}
      ctx.beginPath();
      points.forEach((p,i)=>{const x=padX+(innerW*i/Math.max(points.length-1,1)); const y=padY+innerH*(1-((Number(p.current_equity||0)-min)/range)); if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);});
      ctx.strokeStyle="#58d6c2"; ctx.lineWidth=3; ctx.stroke();
      ctx.lineWidth=1;
    }
    async function refresh(){
      $("statePill").textContent="Refreshing";
      state.symbol=$("symbolInput").value.trim()||"ETH_USDT";
      const res=await fetch(`/api/dashboard/snapshot?symbol=${encodeURIComponent(state.symbol)}`);
      const data=await res.json();
      $("headline").textContent=`${state.symbol} · updated ${new Date(data.generated_at).toLocaleTimeString()}`;
      $("apiConfigured").textContent=data.health.api_configured?"Connected":"Missing";
      $("killSwitch").textContent=data.portfolio.kill_switch?"ON":"OFF";
      $("killSwitch").className=`value ${data.portfolio.kill_switch?'bad':'good'}`;
      $("openPositions").textContent=String(data.portfolio.open_positions ?? 0);
      $("errorCount").textContent=String(data.monitoring.error_count ?? 0);
      $("equityValue").textContent=fmt(data.portfolio.current_equity,2);
      $("drawdownValue").textContent=pct(data.portfolio.drawdown_pct);
      $("dailyLossValue").textContent=pct(data.portfolio.daily_loss_pct);
      $("exposureValue").textContent=`${fmt(data.portfolio.open_risk_usd,2)} / ${fmt(data.portfolio.open_notional_usd,2)}`;
      $("closedTrades").textContent=String(data.trade_summary.closed_trades);
      $("winRate").textContent=`${fmt(data.trade_summary.win_rate,1)}%`;
      $("realizedPnl").textContent=signed(data.trade_summary.realized_pnl,2);
      $("realizedPnl").className=`value ${Number(data.trade_summary.realized_pnl)>=0?'good':'bad'}`;
      $("bestSetup").textContent=data.trade_summary.best_setup||"--";
      $("curveMeta").textContent=`Snapshots: ${data.history.portfolio.length} · Risk events: ${data.monitoring.risk_count} · Runtime states: ${data.runtime.items.length}`;
      $("positionsTable").innerHTML=table(["Symbol","Side","Size","UPnL"], (data.account.positions||[]).map(p=>[p.symbol, `<span class="${p.side==='long'?'good':'bad'}">${p.side}</span>`, fmt(p.size,4), `<span class="${Number(p.unrealized_pnl)>=0?'good':'bad'}">${signed(p.unrealized_pnl,2)}</span>`]));
      $("ordersTable").innerHTML=table(["ID","Side","Type","Size"], (data.account.orders||[]).slice(0,8).map(o=>[String(o.id||"").slice(0,10), o.side||"--", o.order_type||"--", fmt(o.size,4)]));
      $("tradesTable").innerHTML=table(["Symbol","Status","PnL","Setup"], (data.trades.items||[]).slice(0,8).map(t=>[t.symbol||"--", t.status||"--", `<span class="${Number(t.pnl)>=0?'good':'bad'}">${signed(t.pnl,2)}</span>`, t.setup_type||"--"]));
      $("runtimeTable").innerHTML=table(["Key","Updated","Value"], (data.runtime.items||[]).slice(0,8).map(r=>[r.state_key, new Date(r.updated_at).toLocaleString(), JSON.stringify(r.value)]));
      eventList("riskEvents", data.events.risk||[]);
      eventList("errorEvents", data.events.execution||[]);
      $("errors").innerHTML=(data.errors||[]).map(msg=>`<div class="error">${msg}</div>`).join("");
      drawCurve(data.history.portfolio||[]);
      $("statePill").textContent="Live";
    }
    $("refreshBtn").addEventListener("click", refresh);
    refresh();
    setInterval(refresh, 10000);
  </script>
</body>
</html>"""
