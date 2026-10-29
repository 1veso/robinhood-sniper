/* Read-only NEON workspace. The book, not this projection, owns financial truth. */
(() => {
  'use strict';
  const root=document.getElementById('neon-workspace');
  const E=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite=v=>v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v));
  const num=(v,d=2)=>finite(v)?Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
  const usd=v=>finite(v)?'$'+num(v):'Unavailable';
  const signed=v=>finite(v)?(Number(v)<0?'−':'+')+'$'+num(Math.abs(Number(v))):'Unavailable';
  const price=v=>finite(v)?'$'+num(v,Number(v)<.001?8:Number(v)<1?6:4):'Unavailable';
  const tone=v=>finite(v)?Number(v)>0?'n-signed-positive':Number(v)<0?'n-signed-negative':'':'';
  const short=v=>v?String(v).slice(0,5)+'…'+String(v).slice(-5):'Unavailable';
  const time=t=>finite(t)&&Number(t)>0?new Date(Number(t)*1000).toLocaleTimeString('en-GB',{timeZone:'Europe/Berlin',hour12:false}):'Unavailable';
  const dated=t=>finite(t)&&Number(t)>0?new Date(Number(t)*1000).toLocaleString('en-GB',{timeZone:'Europe/Berlin',hour12:false}):'Unavailable';
  const age=t=>{if(!finite(t)||Number(t)<=0)return 'unknown';const s=Math.max(0,Math.floor(Date.now()/1000-Number(t)));return s<60?s+'s':s<3600?Math.floor(s/60)+'m '+s%60+'s':Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m';};
  const readable=v=>String(v??'not run').replace(/_/g,' ');
  const substage=v=>({claim:'Candidate intake',limits:'Portfolio and cooldown checks',facts:'Rugcheck facts',pool:'Pinned pool checks',judge:'Model judgment',jev:'Additional-review applicability',refresh:'Fresh pinned-pool quote',entry:'Final risk check / paper commit',watch:'Position watcher',signals:'Leader/source collection'}[v]||readable(v));
  const svg=(path,cls='')=>`<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
  const icons={scout:'<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5M10 7v6M7 10h6"/>',filter:'<path d="M4 5h16l-6 7v6l-4 2v-8Z"/>',analyst:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/>',execution:'<path d="m13 3-8 11h7l-1 7 8-11h-7Z"/>',guard:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',chart:'<path d="M3 3v18h18M6 16l4-5 4 2 6-8"/>',list:'<path d="M8 6h12M8 12h12M8 18h12M3 6h1M3 12h1M3 18h1"/>'};
  const stageNames={scout:'SCOUT',filter:'FILTER',analyst:'ANALYST',execution:'PAPER EXECUTION',guard:'GUARD'};
  const stages=Object.keys(stageNames);
  const pendingPaint=new Map();
  let activeCandidateMint=null;
  let active=false,mounted=false,state=null,workspace=null,refreshCallback=()=>{},cursor=null,account=null,epoch=0,request=null,streamRunning=false,streamFailures=0,lastReceipt=0,lastEventReceipt=0,serverOffset=0,journalGeneration=0,journalBusy=false,journalRows=[],nextBefore=null,follow=true,pending=new Set(),rowSeen=new Set(),notifications=new Set(),chartRange=86400,chartSignature='',detailMint='',detailGeneration=0,journalTimer=null,snapshotTimer=null,activeCategory='',activeReason='',lastFocus=null;
  const metrics=window.NEON_UI_METRICS={events:[],interactions:[],resyncs:0,duplicate_events:0,out_of_order_events:0,gaps:0,transport:'JSON long poll',account_id:null};
  const $=id=>document.getElementById(id);
  const text=(id,value)=>{const el=$(id);if(el&&el.textContent!==String(value??''))el.textContent=value??'';};
  const setHTML=(id,value)=>{const el=$(id);if(el&&el.innerHTML!==value)el.innerHTML=value;};
  function interaction(name,start=performance.now()){requestAnimationFrame(()=>{metrics.interactions.push({name,duration_ms:performance.now()-start,at:Date.now()});if(metrics.interactions.length>200)metrics.interactions.shift();});}
  function mount(){
    if(mounted)return;mounted=true;root.className='neon';
    root.innerHTML=`<div id="n-replay" class="n-replay" hidden><strong>REPLAY</strong><span>Isolated acceptance account. Synthetic evidence and fills are excluded from NEON strategy results.</span></div>
      <header class="n-top"><div class="n-identity"><div class="n-logo">${svg('<path d="M5 19V5h4l6 14h4V5M5 5l14 14"/>')}</div><div><h1>Neon</h1><div class="n-account"><span>Solana Leader Follower</span><button id="n-copy-account" class="n-copy" title="Copy selected account ID">${svg(icons.copy)}<span id="n-account">Reading account</span></button></div></div></div><div class="n-header-status"><div class="n-badges"><span id="n-data" class="n-pill">DATA CONNECTING</span><span class="n-pill">PAPER EXECUTION</span></div><div><span id="n-worker" class="n-pill" data-tone="warn">Reading worker</span></div><span id="n-last-worker" class="n-small">Waiting for recorded completion</span></div></header>
      <section class="n-hero" aria-label="NEON paper performance"><div class="n-hero-top"><div><div id="n-main-label" class="n-eyebrow">Reading authoritative book</div><div id="n-main-value" class="n-hero-value">—</div><div id="n-net" class="n-hero-net">Reading paper P&amp;L</div><p id="n-cost-treatment" class="n-hero-caption">Financial values come from the existing account ledger.</p></div><div class="n-ranges" aria-label="Performance chart range"><button data-n-range="3600" aria-pressed="false">1H</button><button data-n-range="86400" aria-pressed="true">24H</button><button data-n-range="604800" aria-pressed="false">7D</button><button data-n-range="0" aria-pressed="false">ALL</button></div></div><div class="n-chart-wrap"><canvas id="n-performance-chart" role="img" aria-label="Recorded paper performance observations"></canvas><div id="n-chart-empty" class="n-chart-empty">No observations loaded</div></div><div class="n-chart-foot"><span id="n-chart-label">Persisted observations · gaps remain visible</span><span id="n-chart-range">—</span></div><div class="n-hero-bottom"><div><label>REALIZED P&amp;L</label><strong id="n-realized">—</strong><small>Recorded closes and costs</small></div><div><label>UNREALIZED P&amp;L</label><strong id="n-unrealized">—</strong><small id="n-valuation">Net liquidation marks</small></div><div><label>OPEN / PAPER ENTRIES</label><strong id="n-position-count">— / —</strong><small>Positive quantity / committed buys</small></div><div><label>CLOSED WIN RATE</label><strong id="n-win-rate">—</strong><small id="n-win-denominator">No closed-position evidence</small></div></div></section>
      <div id="n-ticker" class="n-ticker" aria-label="Timestamped market observations"><strong>MARKET TAPE</strong><span class="n-ticker-empty">No recorded pool observations</span></div>
      <div class="n-grid"><section class="n-card n-pipeline-card"><div class="n-heading"><div class="n-heading-label">${svg(icons.execution)}<h2>Live pipeline</h2></div><span class="n-scope">Responsibilities · not agents</span></div><div id="n-pipeline" class="n-pipeline">${stages.map(id=>`<div id="n-stage-${id}" class="n-stage" data-stage="${id}"><div class="n-stage-icon">${svg(icons[id])}</div><div><div class="n-stage-title"><strong>${stageNames[id]}</strong><span id="n-count-${id}" class="n-stage-count"></span></div><div id="n-sub-${id}" class="n-stage-sub">Reading recorded stage</div></div><div><div id="n-status-${id}" class="n-stage-status" data-state="not run">NOT RUN</div><span id="n-elapsed-${id}" class="n-stage-elapsed"></span></div></div>`).join('')}</div><div id="n-blockers" class="n-blockers" hidden></div><div class="n-pipeline-footer"><span id="n-last-scans">No recorded scan / watch</span><span id="n-next-run">Next run unavailable</span></div></section>
      <section class="n-card n-positions-card"><div class="n-heading"><div class="n-heading-label">${svg(icons.guard)}<h2>Positions</h2></div><span id="n-open-label" class="n-scope">Reading book</span></div><div id="n-positions" class="n-position-list"></div><div class="n-stats-foot"><span id="n-watch-at">Watcher completion unavailable</span><span id="n-costs">Costs unavailable</span></div></section>
      <section class="n-card n-stream-card"><div class="n-heading"><div class="n-heading-label">${svg(icons.list)}<h2>Decision stream</h2></div><button id="n-follow" class="n-follow" aria-pressed="true"><span class="n-live-dot"></span>Following</button></div><div class="n-toolbar"><label class="n-search">${svg(icons.scout)}<input id="n-search" type="search" maxlength="100" placeholder="Token or mint…" aria-label="Filter journal token or mint"></label><label><span class="sr-only">Stage</span><select id="n-filter-stage" aria-label="Filter journal stage"><option value="">All stages</option>${stages.map(id=>`<option value="${id}">${stageNames[id]}</option>`).join('')}</select></label><label><span class="sr-only">Outcome</span><select id="n-outcome" aria-label="Filter journal outcome"><option value="">All outcomes</option><option value="running">Running</option><option value="passed">Passed</option><option value="rejected">Rejected</option><option value="blocked">Blocked</option><option value="error">Error</option><option value="filled">Entry / fill records</option></select></label><label><span class="sr-only">Time</span><select id="n-time" aria-label="Filter journal time range"><option value="3600">Last hour</option><option value="86400" selected>Last 24h</option><option value="604800">Last 7d</option><option value="0">All recorded</option></select></label><button id="n-reset-filters" title="Reset journal filters">Reset</button></div><div id="n-active-filter" class="n-active-filter" hidden></div><div class="n-stream-head"><span>BERLIN</span><span>STAGE · DECISION · EVIDENCE</span></div><div id="n-log" class="n-log" tabindex="0" aria-label="Recorded decision journal"></div><div class="n-log-footer"><span id="n-log-total">Reading complete journal scope…</span><button id="n-new" class="n-new" hidden>New events</button><button id="n-older" class="n-load-older" hidden>Load older</button></div></section>
      <section class="n-card n-rejections-card"><div class="n-heading"><div class="n-heading-label">${svg(icons.filter)}<h2>Why candidates stop</h2></div><span class="n-scope" id="n-rejection-scope">Selected journal scope</span></div><div class="n-rejection-tabs" aria-label="Rejection categories"><button data-n-category="" aria-pressed="true">All</button><button data-n-category="strategy" aria-pressed="false">Strategy</button><button data-n-category="missing_evidence" aria-pressed="false">Missing evidence</button><button data-n-category="provider" aria-pressed="false">Provider</button><button data-n-category="portfolio" aria-pressed="false">Limits</button></div><div id="n-reasons" class="n-reason-list"></div><div class="n-stats-foot"><span id="n-candidate-total">Unique candidates unavailable</span><span id="n-error-total">Failures unavailable</span></div></section>
      <footer class="n-connection-strip"><span id="n-source-age">Source observation unavailable · heartbeat is not a market quote</span><span id="n-transport">Read-only book stream · connecting</span></footer></div>
      <div id="n-toasts" class="n-toasts" role="status" aria-live="polite" aria-atomic="false"></div><dialog id="n-drawer" class="n-drawer" aria-labelledby="n-detail-title"><div class="n-drawer-head"><div><span class="n-eyebrow n-dim">RECORDED EVIDENCE</span><h2 id="n-detail-title">Candidate</h2><div id="n-detail-sub" class="n-small"></div></div><button id="n-close-drawer" class="n-drawer-close" aria-label="Close candidate evidence">×</button></div><div id="n-detail-body" class="n-drawer-body"></div></dialog>`;
    root.addEventListener('click',click);
    root.addEventListener('change',e=>{if(['n-filter-stage','n-outcome','n-time'].includes(e.target.id)){interaction('filter');loadJournal(true);}});
    let searchTimer; $('n-search').addEventListener('input',()=>{clearTimeout(searchTimer);interaction('search');searchTimer=setTimeout(()=>loadJournal(true),160);});
    $('n-log').addEventListener('scroll',()=>{if($('n-log').scrollTop>24&&follow){follow=false;followState();}},{passive:true});
    $('n-drawer').addEventListener('close',()=>{detailMint='';detailGeneration++;if(lastFocus?.isConnected)lastFocus.focus();});
    $('n-drawer').addEventListener('click',e=>{if(e.target===$('n-drawer')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
    new ResizeObserver(()=>{if(active&&state)drawChart(true);}).observe($('n-performance-chart').parentElement);
  }
  function setActive(value){
    mount();active=value;root.hidden=!value;document.body.classList.toggle('neon-active',value);
    if(!value){epoch++;journalGeneration++;journalBusy=false;detailGeneration++;request?.abort();streamRunning=false;clearTimeout(journalTimer);journalTimer=null;clearTimeout(snapshotTimer);snapshotTimer=null;if($('n-drawer').open)$('n-drawer').close();return;}
    if(state){renderState();startStream();}
  }
  function readNotifications(){try{notifications=new Set(JSON.parse(sessionStorage.getItem('neon-notices:'+account)||'[]'));}catch{notifications=new Set();}}
  function update(next){
    mount();if(!active)setActive(true);
    if(account===next.account_id&&cursor&&next.event_stream?.cursor){const incoming=next.event_stream.cursor.split(':'),known=cursor.split(':');if(incoming[0]===known[0]&&Number(incoming.at(-1))<Number(known.at(-1))){metrics.stale_snapshots=(metrics.stale_snapshots||0)+1;scheduleSnapshot(100);return;}}
    if(account!==next.account_id){epoch++;request?.abort();streamRunning=false;account=next.account_id;lastEventReceipt=0;streamFailures=0;journalGeneration++;journalBusy=false;detailGeneration++;metrics.events=[];cursor=next.event_stream?.cursor||null;rowSeen.clear();pending.clear();pendingPaint.clear();journalRows=[];readNotifications();metrics.account_id=account;activeCandidateMint=null;$('n-toasts').replaceChildren();}
    state=next;workspace=next.neon_workspace||{};lastReceipt=Date.now();renderState();
    const snapshotSeq=Number(next.event_stream?.cursor?.split(':').at(-1));for(const [id,item] of pendingPaint){if(item.event.seq<=snapshotSeq){measureVisible(item.event,item.receivedAt);pendingPaint.delete(id);}}
    if(!cursor)cursor=next.event_stream?.cursor||null;
    if(!journalRows.length||follow)loadJournal(false);
    startStream();
  }
  function renderState(){
    if(!state||!active)return;
    const s=state.snapshot||{},w=workspace||{},counts=w.counts||{},worker=w.worker||{},data=w.data||{};
    $('n-replay').hidden=!(w.replay||state.replay);text('n-account',short(account));$('n-copy-account').title='Copy account '+account;
    const useEquity=s.capital_baseline_supported===true&&finite(s.equity);text('n-main-label',useEquity?'PAPER ACCOUNT EQUITY':'NET PAPER P&L');
    const value=useEquity?s.equity:s.net_pnl,formatted=finite(value)?num(Math.abs(Number(value))):null;
    setHTML('n-main-value',formatted?`${Number(value)<0?'−':''}$${E(formatted.split('.')[0])}<span class="n-decimal">.${E(formatted.split('.')[1]||'00')}</span>`:'<span class="n-decimal">Unavailable</span>');
    setHTML('n-net',`${E(signed(s.net_pnl))}<span>net paper P&amp;L${finite(s.return_pct)?' · '+E(num(s.return_pct))+'% of recorded baseline':''}</span>`);
    text('n-cost-treatment',s.api_costs_known?'After modeled execution costs and recorded model charges. Rent stays reserved capital.':'Full net P&L unavailable: model charges remain unreconciled. Known costs are not a complete bill.');
    text('n-realized',signed(s.realized_pnl));text('n-unrealized',signed(s.unrealized_pnl));text('n-position-count',`${num(s.open_positions,0)} / ${num(counts.paper_entries,0)}`);
    text('n-win-rate',finite(counts.closed_win_rate_pct)?num(counts.closed_win_rate_pct,1)+'%':'—');text('n-win-denominator',finite(counts.closed_positions)?`${num(counts.closed_wins,0)} wins / ${num(counts.closed_positions,0)} closed positions`:'Closed-position evidence unavailable');
    text('n-valuation',s.unresolved_positions?`${num(s.unresolved_positions,0)} unresolved · conservative marks`:'After modeled liquidation costs');
    text('n-open-label',`${num(s.open_positions,0)} OPEN · ${num(counts.paper_entries,0)} ENTRIES`);
    text('n-costs',`${usd(s.fees_paid)} modeled costs`);text('n-watch-at','Watch completed '+time(worker.last_watch_at));
    text('n-candidate-total',`${num(counts.token_candidates,0)} token candidates · ${num(counts.research_traders,0)} research traders`);text('n-error-total',`${num(counts.operational_failures,0)} failures · account`);
    text('n-last-scans',`Scan ${time(worker.last_scan_at)} · Watch ${time(worker.last_watch_at)}`);
    const next=[worker.next_scan_at,worker.next_watch_at].filter(finite).map(Number).filter(x=>x>Date.now()/1000).sort((a,b)=>a-b)[0];text('n-next-run',next?'Next scheduled '+time(next):'Next schedule unavailable');
    const blockers=state.blockers||state.readiness?.blockers||[];$('n-blockers').hidden=!blockers.length;setHTML('n-blockers',`<strong>Entries are blocked</strong><ul>${[...new Set(blockers)].map(b=>'<li>'+E(b)+'</li>').join('')}</ul>`);
    for(const id of stages){const row=(w.pipeline||[]).find(p=>p.id===id);renderStage(id,row||{state:'not run',substage:'No recorded stage',count:null});}
    text('n-count-scout',`${num(counts.research_traders,0)} research traders · ${num(counts.token_candidates,0)} token candidates`);
    renderPositions();renderTicker();drawChart();tick();
    if(!w.pipeline)text('n-transport','Workspace evidence unavailable; waiting for the matching backend version');
  }
  function renderStage(id,row){
    const result=readable(row.state||'not run').toLowerCase();text('n-status-'+id,result.toUpperCase());$('n-status-'+id).dataset.state=result;
    text('n-count-'+id,finite(row.count)?`${num(row.count,0)} ${row.count_label||'recorded'}`:row.count_label||'No count available');
    const mint=row.mint;setHTML('n-sub-'+id,`${E(substage(row.substage||result))}${mint?' · <button data-n-mint="'+E(mint)+'">'+E(row.symbol||short(mint))+'</button>':''}`);
    $('n-stage-'+id).dataset.started=finite(row.started_at)?String(row.started_at):'';$('n-stage-'+id).dataset.state=result;
  }
  function renderPositions(){
    const rows=state.snapshot?.positions||[];
    if(!rows.length){setHTML('n-positions','<div class="n-empty"><div class="n-empty-symbol">◇</div><strong>No open paper positions</strong>Only a committed buy appears here.<br>Rejected candidates never become positions.</div>');return;}
    setHTML('n-positions',rows.map(p=>{const mint=p.mint||p.asset_id,id=p.id;return `<article class="n-position" data-position="${E(id)}"><div class="n-position-head"><button data-n-mint="${E(mint)}">${E(p.symbol||short(mint))}<small>${E(short(p.pool||p.pool_id))} · pinned pool</small></button><strong class="n-mono ${tone(p.unrealized_pnl)}">${E(signed(p.unrealized_pnl))}</strong></div><div class="n-position-metrics"><div><span>Entry</span><b>${E(price(p.entry_price))}</b></div><div><span>Observed</span><b>${E(price(p.mark_price))}</b></div><div><span>Remaining</span><b>${E(num(p.quantity??p.remaining,5))}</b></div><div><span>Cost basis</span><b>${E(usd(p.cost_basis??p.remaining_basis))}</b></div><div><span>Stop</span><b>${E(price(p.stop_price))}</b></div><div><span>Next take</span><b>${E(price(p.next_take_price??p.take_price))}</b></div><div><span>Realized</span><b>${E(signed(p.realized_pnl))}</b></div><div><span>Trailing</span><b>${E(p.trailing_active?'Armed':'Not armed')}</b></div></div><div class="n-position-note">${p.quote_unavailable?'Quote unavailable · holding retained, no sale':'Quote '+E(age(p.last_quote_at??p.mark_at))+' old'} · ${E(p.exit_status||'No committed exit recorded')}</div></article>`;}).join(''));
  }
  function renderTicker(){
    const unique=new Map();for(const m of state.market||[]){const key=m.mint+':'+m.pool;if(!unique.has(key))unique.set(key,m);}
    setHTML('n-ticker','<strong>MARKET TAPE</strong>'+([...unique.values()].slice(0,5).map(m=>`<button class="n-ticker-item" data-n-mint="${E(m.mint)}"><span>${E(m.symbol||short(m.mint))}</span><b class="n-mono">${E(price(m.price))}</b><small>${E(time(m.observed_at))}</small></button>`).join('')||'<span class="n-ticker-empty">No recorded pool observations · no invented prices</span>'));
  }
  function drawChart(force=false){
    if(!active||!state)return;
    const equity=state.snapshot?.capital_baseline_supported===true&&finite(state.snapshot.equity),field=equity?'equity':'net_pnl';
    const all=(equity?state.equity_history:state.pnl_history)||[],end=Math.max(0,...all.map(p=>Number(p.observed_at)||0));
    const points=all.filter(p=>(!chartRange||p.observed_at>=end-chartRange));
    const canvas=$('n-performance-chart'),rect=canvas.getBoundingClientRect();if(!rect.width||!rect.height)return;
    const signature=JSON.stringify([equity,chartRange,rect.width,rect.height,points.map(p=>[p.observed_at,p[field]])]);if(!force&&signature===chartSignature)return;chartSignature=signature;
    const dpr=window.devicePixelRatio||1;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);const width=rect.width,height=rect.height;
    text('n-chart-label',(equity?'Equity':'Net P&L')+' · recorded observations; gaps unfilled');canvas.setAttribute('aria-label',(equity?'Equity':'Net paper P&L')+' from persisted observations, with gaps left unfilled');
    const valid=points.filter(p=>finite(p[field]));$('n-chart-empty').hidden=valid.length>1;
    text('n-chart-empty',valid.length===1?'One recorded valuation · waiting for the next observation':'No recorded performance series for this range');text('n-chart-range',valid.length?time(valid[0].observed_at)+' — '+time(valid.at(-1).observed_at):'No history');
    if(valid.length<2)return;
    let lo=Math.min(...valid.map(p=>Number(p[field]))),hi=Math.max(...valid.map(p=>Number(p[field])));const pad=Math.max((hi-lo)*.15,.01);lo-=pad;hi+=pad;
    const start=valid[0].observed_at,finish=Math.max(start+1,valid.at(-1).observed_at),x=t=>7+(t-start)/(finish-start)*(width-14),y=v=>10+(hi-v)/(hi-lo)*(height-22);
    ctx.strokeStyle='#f9cce71e';ctx.lineWidth=1;ctx.setLineDash([2,6]);for(let i=0;i<3;i++){ctx.beginPath();ctx.moveTo(0,12+i*(height-24)/2);ctx.lineTo(width,12+i*(height-24)/2);ctx.stroke();}ctx.setLineDash([]);
    ctx.strokeStyle='#fbe7ff';ctx.lineWidth=1.6;ctx.beginPath();let prev=null;for(const p of points){if(!finite(p[field])){prev=null;continue;}if(!prev||p.observed_at-prev.observed_at>900)ctx.moveTo(x(p.observed_at),y(Number(p[field])));else ctx.lineTo(x(p.observed_at),y(Number(p[field])));prev=p;}ctx.stroke();
    const last=valid.at(-1);ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(x(last.observed_at),y(Number(last[field])),2.7,0,Math.PI*2);ctx.fill();
  }
  function tick(){
    if(!active||!state)return;const worker=workspace?.worker||{},data=workspace?.data||{},connected=lastReceipt&&Date.now()-lastReceipt<15000;
    const dataState=connected?String(data.state||'unknown').toLowerCase():'disconnected';const fresh=['live','fresh','connected'].includes(dataState);text('n-data',!connected?'DATA DISCONNECTED':fresh?'LIVE DATA':dataState==='stale'?'DATA STALE':'DATA '+readable(dataState).toUpperCase());$('n-data').dataset.tone=!connected?'error':fresh?'good':'warn';
    const status=connected?readable(worker.state||state.readiness?.state||'unknown').toLowerCase():'disconnected';text('n-worker',status.toUpperCase());$('n-worker').dataset.tone=['running','idle'].includes(status)?'good':status==='error'||status==='disconnected'?'error':'warn';
    const completed=Math.max(Number(worker.last_scan_at)||0,Number(worker.last_watch_at)||0);text('n-last-worker','Last completion '+age(completed)+' ago');
    for(const id of stages){const row=$('n-stage-'+id),started=Number(row.dataset.started);text('n-elapsed-'+id,row.dataset.state==='running'&&started?age(started)+' elapsed':'');}
    text('n-source-age','Source observed '+(data.source_observed_at?age(data.source_observed_at)+' ago':'at an unknown time')+' · connection heartbeat is not a price');
    text('n-transport',(streamFailures?'Polling recovery':'Book stream connected')+' · UI received '+(lastEventReceipt?new Date(lastEventReceipt).toLocaleTimeString('en-GB',{hour12:false}):'snapshot')+' · Berlin evidence times');
  }
  function disconnected(){lastReceipt=0;tick();}
  async function json(path,signal){const response=await fetch(path,{cache:'no-store',signal:signal||AbortSignal.timeout(18000)});if(!response.ok){const e=new Error('Read-only endpoint returned '+response.status);e.status=response.status;throw e;}return response.json();}
  function resetStream(reason){metrics.resyncs++;metrics.last_resync_reason=reason;cursor=null;rowSeen.clear();pendingPaint.clear();pending.clear();request?.abort();streamRunning=false;scheduleSnapshot(0);}
  async function startStream(){
    if(!active||!cursor||streamRunning)return;streamRunning=true;const run=epoch;
    while(active&&run===epoch&&cursor){
      const after=cursor;request=new AbortController();const timeout=setTimeout(()=>request?.abort(),16000);
      try{
        const received=await json('/api/events?'+new URLSearchParams({bot:'neon',after,limit:'200',wait_ms:'1000'}),request.signal);clearTimeout(timeout);
        if(!active||run!==epoch)break;
        if(received.account_id!==account||received.prev_cursor&&received.prev_cursor!==after){resetStream('Account or cursor boundary changed');break;}
        if(received.available===false){resetStream('Stream unavailable');break;}
        const stream=after.split(':')[0];if(!received.cursor?.startsWith(stream+':')){resetStream('Stream identity changed');break;}
        if(finite(received.server_time_ms))serverOffset=Date.now()-Number(received.server_time_ms);
        const lastSeq=Number(after.split(':').at(-1));let previous=lastSeq,expectedCursor=after,accepted=0;
        for(const event of received.events||[]){
          if(event.account_id!==account||!event.id?.startsWith(stream+':')){resetStream('Event identity mismatch');break;}
          if(event.seq<=lastSeq||rowSeen.has(event.id)){metrics.duplicate_events++;continue;}
          if(event.seq<previous){metrics.out_of_order_events++;resetStream('Response event order changed');break;}
          if(event.previous_cursor&&event.previous_cursor!==expectedCursor){metrics.gaps++;resetStream('Event checkpoint gap; resynchronizing from book');break;}
          expectedCursor=event.id;accepted++;previous=event.seq;rowSeen.add(event.id);if(rowSeen.size>4000)rowSeen.delete(rowSeen.values().next().value);receive(event);
        }
        if(!cursor)break;if(received.cursor!==expectedCursor){metrics.gaps++;resetStream('Page cursor does not match accepted event checkpoint');break;}cursor=received.cursor;lastReceipt=Date.now();streamFailures=0;tick();
        if(!received.events?.length)await new Promise(r=>setTimeout(r,80));
      }catch(e){clearTimeout(timeout);if(!active||run!==epoch)break;if(e.status===409){resetStream('Server requested snapshot recovery');break;}streamFailures++;if(streamFailures>=2)disconnected();await new Promise(r=>setTimeout(r,Math.min(5000,500*streamFailures)));scheduleSnapshot(100);}
    }
    if(run===epoch)streamRunning=false;
  }
  function mapStage(name){if(stages.includes(name))return name;if(['claim','discovery','research','signals','selection','leader','admission','chain','event','signal'].includes(name))return 'scout';if(['limits','facts','pool','risk','token','seen_once'].includes(name))return 'filter';if(['judge','jev','model','x','grok'].includes(name))return 'analyst';if(['refresh','entry','fill'].includes(name))return 'execution';if(['watch','exit','stop','take_profit','trailing'].includes(name))return 'guard';return 'scout';}
  function eventRow(event){
    if(event.journal_item)return event.journal_item;
    const d=event.data||{},stage=event.type==='paper_fill'?(d.kind==='buy'?'execution':'guard'):event.type==='position_changed'?'guard':mapStage(d.stage||d.job||event.type);
    let outcome=d.status==='STARTED'||d.status==='running'||d.status==='RUNNING'?'running':d.status==='PASSED'?'passed':d.verdict==='SKIP'?'rejected':d.status==='error'?'error':d.entry||event.type==='paper_fill'?'filled':d.verdict==='BUY'?'passed':'passed';
    if(d.status==='COMPLETED'&&!d.entry){const reason=String(d.reason||'').toLowerCase();if(/stage failed|timeout|http|provider failed|feed_unavailable|network|rpc error|interrupted/.test(reason))outcome='error';else if(/missing|unavailable|disabled|not started|not active|not current|not configured|unqualified|no current|not selected|stale|unverified|unreconciled|evidence|selection|cooldown|maximum|daily|portfolio|capital|limit reached/.test(reason))outcome='blocked';else if(d.verdict==='SKIP'||d.verdict==='BUY')outcome='rejected';}
    if(event.type==='readiness_changed')outcome=d.entry_enabled===true?'passed':'blocked';if(event.type==='source_observation')outcome=d.status==='RUNNING'?'running':d.status==='OK'?'passed':'error';
    let message=d.reason||readable(d.stage||d.job||event.type);
    if(event.type==='source_observation')message=(d.source_name||'Source')+' · '+readable(d.status);
    if(event.type==='candidate_intake')message='Unqualified trader profile observed · '+(d.handle||short(d.wallet));
    if(event.type==='readiness_changed')message='Entry readiness · '+readable(d.trial_state)+' · '+(d.blockers||[]).join('; ');if(event.type==='paper_fill')message=`${d.kind==='buy'?'Paper entry':String(d.reason||'').startsWith('take_profit')?'Partial paper exit':'Paper close'} recorded · ${num(d.quantity,5)} at ${price(d.price)}${finite(d.pnl)?' · P&L '+signed(d.pnl):''}`;
    return {...event,stage,outcome,mint:d.mint,symbol:d.symbol,candidate_id:d.candidate_id,position_id:d.position_id||d.id,message};
  }
  function receive(event){
    const receivedAt=Date.now();lastEventReceipt=receivedAt;const item=eventRow(event),d=event.data||{};
    if(event.type==='candidate_stage'&&d.mint&&activeCandidateMint!==d.mint){activeCandidateMint=d.mint;for(const id of ['filter','analyst','execution']){const prior=(workspace?.pipeline||[]).find(r=>r.id===id)||{};renderStage(id,{...prior,mint:d.mint,state:'not run',substage:'Not run for this candidate',started_at:null});}}
    if(['candidate_stage','source_observation'].includes(event.type)){
      const row={state:item.outcome,substage:d.stage||d.source_name||event.type,mint:d.mint,started_at:item.outcome==='running'?event.observed_at:null,count:(workspace?.pipeline||[]).find(r=>r.id===item.stage)?.count,count_label:(workspace?.pipeline||[]).find(r=>r.id===item.stage)?.count_label};renderStage(item.stage,row);
      if(['rejected','blocked','error'].includes(item.outcome)){for(const id of stages.slice(stages.indexOf(item.stage)+1).filter(x=>x!=='guard'))renderStage(id,{state:'not run',substage:'Upstream candidate stopped',mint:d.mint});}
    }
    if(['candidate_stage','paper_fill','position_changed','worker_state','source_observation','candidate_intake'].includes(event.type)){
      const el=$('n-stage-'+item.stage);el.classList.add('n-arrived');setTimeout(()=>el.classList.remove('n-arrived'),220);
    }
    if(!['market_observation','pnl_observation','position_changed'].includes(event.type)){
      if(follow&&matches(item)){journalRows=[item,...journalRows.filter(r=>r.id!==item.id)].slice(0,100);renderJournalRows();}
      else{pending.add(item.id);followState();}
    }
    if(event.type==='paper_fill'||event.type==='worker_state'&&d.status==='error'||item.outcome==='error')toastEvent(item);
    if(['paper_fill','position_changed','pnl_observation','worker_state','candidate_stage','market_observation','source_observation','candidate_intake','readiness_changed'].includes(event.type))scheduleSnapshot(120);
    if(follow){clearTimeout(journalTimer);journalTimer=setTimeout(()=>loadJournal(false),200);}
    if(['paper_fill','position_changed','pnl_observation'].includes(event.type))pendingPaint.set(event.id,{event,receivedAt});else measureVisible(event,receivedAt);
  }
  function measureVisible(event,receivedAt){
    const owner=account,run=epoch;
    requestAnimationFrame(()=>requestAnimationFrame(()=>{
      if(!active||owner!==account||run!==epoch)return;
      const visible=Date.now(),persisted=finite(event.recorded_at_ms)?Number(event.recorded_at_ms):null;
      metrics.events.push({id:event.id,type:event.type,persisted_at_ms:persisted,event_recorded_at:event.recorded_at,source_observed_at:event.observed_at,ui_received_at_ms:receivedAt,visible_at_ms:visible,receipt_to_visible_ms:visible-receivedAt,persisted_to_visible_ms:persisted!==null?visible-serverOffset-persisted:null,timestamp_resolution:persisted===null?'seconds; not sufficient for sub-second proof':'milliseconds'});if(metrics.events.length>500)metrics.events.shift();
    }));
  }
  function scheduleSnapshot(delay=120){if(snapshotTimer)return;snapshotTimer=setTimeout(()=>{snapshotTimer=null;if(active)refreshCallback();},delay);}
  function filters(){const params=new URLSearchParams({limit:'100'}),stage=$('n-filter-stage').value,outcome=$('n-outcome').value,token=$('n-search').value.trim(),seconds=Number($('n-time').value);if(stage)params.set('stage',stage);if(outcome)params.set('outcome',outcome);if(token)params.set('token',token);if(seconds)params.set('since',Math.floor(Date.now()/1000-seconds));if(activeCategory)params.set('category',activeCategory);if(activeReason)params.set('reason',activeReason);return params;}
  function matches(row){const params=filters();return (!params.has('stage')||params.get('stage')===row.stage)&&(!params.has('outcome')||params.get('outcome')===row.outcome)&&(!params.has('token')||(String(row.mint||'')+' '+String(row.symbol||'')).toLowerCase().includes(params.get('token').toLowerCase()))&&(!params.has('since')||row.observed_at>=Number(params.get('since')))&&(!activeCategory||row.category===activeCategory)&&(!activeReason||row.message===activeReason||row.data?.reason===activeReason);}
  async function loadJournal(reset=false,older=false){
    if(!active||!account)return;if(journalBusy&&!reset)return;const token=++journalGeneration;journalBusy=true;
    if(reset){follow=true;pending.clear();followState();$('n-log').scrollTop=0;}
    const params=filters();if(older&&nextBefore)params.set('before',nextBefore);
    try{const data=await json('/api/neon-journal?'+params);if(!active||token!==journalGeneration)return;
      if(data.account_id&&data.account_id!==account)throw new Error('Journal account mismatch');
      const items=data.items||[];
      if(older){const merged=new Map(journalRows.map(r=>[r.id,r]));items.forEach(r=>merged.set(r.id,r));journalRows=[...merged.values()].slice(-200);follow=false;}
      else if(follow||reset||!journalRows.length)journalRows=items;
      nextBefore=data.next_before||null;
      if(follow||reset||older||!$('n-log').children.length)renderJournalRows();
      text('n-log-total',`${num(data.total,0)} matching records · ${journalRows.length} displayed`);$('n-older').hidden=!nextBefore;
      renderReasons(data.rejections||workspace?.rejections||[]);text('n-rejection-scope',data.scope?.label||'SELECTED JOURNAL SCOPE');
      text('n-active-filter',activeReason?'Reason: '+activeReason:activeCategory?'Category: '+readable(activeCategory):'');$('n-active-filter').hidden=!activeReason&&!activeCategory;
    }catch(e){if(token===journalGeneration){text('n-log-total','Journal temporarily unavailable · retrying');if(!journalRows.length)setHTML('n-log','<div class="n-empty">The journal could not be read.<br>No decision or fill is assumed.</div>');}}
    finally{if(token===journalGeneration)journalBusy=false;followState();}
  }
  function journalMessage(row){
    const d=row.data||{};
    if(row.type==='paper_fill')return `${d.kind==='buy'?'Paper entry':String(d.reason||'').startsWith('take_profit')?'Partial paper exit':'Paper close'} recorded · quantity ${num(d.quantity,6)} · modeled price ${price(d.price)}${d.kind!=='buy'?' · realized P&L '+signed(d.pnl):''}`;
    if(row.type==='position_changed'&&!d.feed_error&&d.remaining!==undefined)return 'Position observation recorded · remaining quantity '+num(d.remaining,6);
    return row.message||row.reason||'Recorded event';
  }
  function rowHTML(row){return `<time datetime="${finite(row.observed_at)?E(new Date(row.observed_at*1000).toISOString()):''}">${E(time(row.observed_at))}</time><div class="n-log-main"><div class="n-log-top"><strong>${E(row.symbol||row.mint&&short(row.mint)||stageNames[row.stage]||'WORKER')}</strong><span class="n-event-tag" data-state="${E(row.outcome)}">${E((row.outcome==='filled'?(row.type==='paper_fill'?'PAPER FILL':'ENTRY CONFIRMED'):readable(row.outcome)).toUpperCase())}</span><span class="n-log-mint">${E(stageNames[row.stage]||readable(row.stage))}</span></div><div class="n-log-message">${E(journalMessage(row))}</div>${row.mint?'<div class="n-log-mint">'+E(short(row.mint))+'</div>':''}</div>`;}
  function renderJournalRows(){
    const log=$('n-log');if(!journalRows.length){setHTML('n-log','<div class="n-empty">No recorded events match these filters.<br>Waiting for actual worker activity.</div>');return;}
    const existing=new Map([...log.querySelectorAll('[data-event-id]')].map(el=>[el.dataset.eventId,el]));
    if(!existing.size)log.replaceChildren();const fragment=document.createDocumentFragment();
    for(const row of journalRows.slice(0,200)){let el=existing.get(row.id);if(!el){el=document.createElement('button');el.type='button';el.className='n-log-row';el.dataset.eventId=row.id;}const body=rowHTML(row);if(el.innerHTML!==body)el.innerHTML=body;el.setAttribute('aria-label',(row.message||'Recorded event')+'; open evidence');fragment.appendChild(el);existing.delete(row.id);}
    existing.forEach(el=>el.remove());log.appendChild(fragment);if(follow)log.scrollTop=0;
  }
  function renderReasons(rows){
    const list=rows.slice(0,9),maximum=Math.max(1,...list.map(r=>Number(r.count)||0));
    setHTML('n-reasons',list.map((r,i)=>`<button class="n-reason" data-n-reason="${E(r.reason)}" data-n-reason-category="${E(r.category||'')}" title="Filter journal by ${E(r.reason)}"><span class="n-reason-label"><span>${E(r.reason)}</span><b>${E(num(r.count,0))}</b></span><div class="n-reason-track"><div class="n-reason-fill" data-bar-width="${Math.max(2,Number(r.count)/maximum*100)}"></div></div></button>`).join('')||'<div class="n-empty">No rejection records in this scope.<br>Missing observations are not a pass.</div>');
    $('n-reasons').querySelectorAll('[data-bar-width]').forEach(el=>el.style.width=el.dataset.barWidth+'%');
  }
  function followState(){if(!mounted)return;const button=$('n-follow');button.setAttribute('aria-pressed',String(follow));button.innerHTML=follow?'<span class="n-live-dot"></span>Following':'Follow paused';$('n-new').hidden=!pending.size;text('n-new',pending.size+' new events');}
  function toastEvent(row){
    if(notifications.has(row.id))return;notifications.add(row.id);while(notifications.size>1000)notifications.delete(notifications.values().next().value);
    try{sessionStorage.setItem('neon-notices:'+account,JSON.stringify([...notifications]));}catch{}
    const container=$('n-toasts');if(container.children.length>=3)container.firstElementChild.remove();const toast=document.createElement('div');toast.className='n-toast';
    toast.innerHTML=svg(icons[row.stage]||icons.guard)+`<div><strong>${E(row.type==='paper_fill'?'Committed paper fill':'Worker needs attention')}</strong><p>${E(journalMessage(row))}</p></div><button aria-label="Dismiss notification">×</button>`;
    toast.querySelector('button').onclick=()=>toast.remove();container.appendChild(toast);setTimeout(()=>{toast.classList.add('n-toast-out');setTimeout(()=>toast.remove(),200);},6500);
  }
  async function copy(value,button){try{await navigator.clipboard.writeText(value);const old=button.title;button.title='Copied';button.setAttribute('aria-label','Copied');setTimeout(()=>{button.title=old;button.removeAttribute('aria-label');},1500);}catch{button.title='Copy unavailable; select the displayed identifier';}interaction('copy');}
  function click(e){
    const start=performance.now(),range=e.target.closest('[data-n-range]');if(range){chartRange=Number(range.dataset.nRange);root.querySelectorAll('[data-n-range]').forEach(b=>b.setAttribute('aria-pressed',String(b===range)));drawChart(true);interaction('chart range',start);return;}
    const mint=e.target.closest('[data-n-mint]');if(mint){openDetail(mint.dataset.nMint,null,mint);interaction('candidate drawer',start);return;}
    const event=e.target.closest('[data-event-id]');if(event){const row=journalRows.find(r=>r.id===event.dataset.eventId);if(row)openDetail(row.mint,row,event);interaction('event evidence',start);return;}
    const reason=e.target.closest('[data-n-reason]');if(reason){activeReason=reason.dataset.nReason;activeCategory=reason.dataset.nReasonCategory;syncCategories();loadJournal(true);interaction('rejection filter',start);return;}
    const category=e.target.closest('[data-n-category]');if(category){activeCategory=category.dataset.nCategory;activeReason='';syncCategories();loadJournal(true);interaction('category filter',start);return;}
    const target=e.target.closest('button');if(!target)return;
    if(target.id==='n-copy-account')copy(account,target);
    if(target.dataset.nCopy)copy(target.dataset.nCopy,target);
    if(target.id==='n-follow'){follow=!follow;if(follow){pending.clear();loadJournal(true);}followState();}
    if(target.id==='n-new'){follow=true;pending.clear();loadJournal(true);}
    if(target.id==='n-older')loadJournal(false,true);
    if(target.id==='n-reset-filters'){$('n-search').value='';$('n-filter-stage').value='';$('n-outcome').value='';$('n-time').value='86400';activeCategory='';activeReason='';syncCategories();loadJournal(true);}
    if(target.id==='n-close-drawer')$('n-drawer').close();
    interaction(target.id||'button',start);
  }
  function syncCategories(){root.querySelectorAll('[data-n-category]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.nCategory===activeCategory)));}
  function safeURL(value){try{const url=new URL(value);return ['https:','http:'].includes(url.protocol)?url.href:null;}catch{return null;}}
  function displayValue(value){if(value===null||value===undefined)return 'Missing';if(typeof value==='object')return JSON.stringify(value);return String(value);}
  function eventEvidence(row){if(!row)return '';const metric=metrics.events.find(e=>e.id===row.id);return `<h3>Selected journal record</h3><p class="n-judgment">${E(journalMessage(row))}</p>${row.data&&['quantity','price','pnl','remaining'].some(k=>row.data[k]!==undefined)?'<details><summary class="n-small">Exact recorded amounts</summary><div class="n-drawer-address">'+['quantity','price','pnl','remaining'].filter(k=>row.data[k]!==undefined).map(k=>E(k)+': '+E(row.data[k])).join('<br>')+'</div></details>':''}<div class="n-event-times"><div>Source observation<b>${E(dated(row.observed_at))}</b></div><div>Event persisted<b>${E(dated(row.recorded_at))}</b></div><div>UI received<b>${metric?E(new Date(metric.ui_received_at_ms).toLocaleString('en-GB')):'Historical record; no original UI receipt'}</b></div><div>Stable event ID<b class="n-drawer-address">${E(row.id)}</b></div></div>`;}
  async function openDetail(mint,row,trigger){
    const dialog=$('n-drawer');lastFocus=trigger||document.activeElement;detailMint=mint||'';const token=++detailGeneration;text('n-detail-title',mint?short(mint):'Worker observation');text('n-detail-sub',mint?'Exact candidate · '+short(account):'Account '+short(account));
    setHTML('n-detail-body',eventEvidence(row)+(mint?'<p class="n-dim">Reading candidate evidence…</p>':'<p class="n-dim">This event describes worker activity, not a token judgment or a fill.</p>'));
    if(!dialog.open)dialog.showModal();$('n-close-drawer').focus();if(!mint)return;
    try{const detail=await json('/api/neon-candidate?'+new URLSearchParams({mint}));if(token!==detailGeneration||!dialog.open)return;if(detail.account_id!==account)throw new Error('Candidate account mismatch');
      text('n-detail-title',detail.symbol||short(mint));text('n-detail-sub','Candidate observed '+dated(detail.observed_at));
      const checks=detail.checks||[],judgment=detail.judgment||{},links=[];for(const item of judgment.references||judgment.x_references||judgment.sources||[]){const url=safeURL(typeof item==='string'?item:item.url);if(url)links.push(`<a href="${E(url)}" target="_blank" rel="noopener noreferrer">${E(typeof item==='string'?url:item.title||url)} ↗</a>`);}
      const positions=detail.positions||[],closed=detail.closed_positions||[],fills=detail.fills||[];
      setHTML('n-detail-body',`<div class="n-drawer-address">Mint ${E(mint)} <button class="n-copy" data-n-copy="${E(mint)}" title="Copy mint">${svg(icons.copy)} Copy</button><br>Pool ${E(detail.pool||'Not established')}<br>Account ${E(account)}</div>${eventEvidence(row)}<h3>14 configured checks · facts and model separated</h3><p class="n-small">Missing evidence stays missing. Checks after an early rejection were not run.</p><div class="n-evidence-grid">${checks.map(c=>`<div class="n-check"><div class="n-check-head"><strong>${E(c.name)}</strong><span class="n-check-result" data-result="${E(String(c.result).toLowerCase())}">${E(readable(c.result))}</span></div><div class="n-check-value">${E(displayValue(c.value))}</div><div class="n-check-rule">Rule: ${E(displayValue(c.threshold))}</div>${c.missing?'<div class="n-check-rule">'+E(c.missing)+'</div>':''}<div class="n-check-source">${E(c.source||'Source unavailable')} · ${E(dated(c.observed_at))}</div></div>`).join('')}</div><h3>Grok / X assessment · separate from facts</h3><div class="n-judgment"><strong>${E(judgment.verdict||'NOT RUN')}</strong>${finite(judgment.confidence)?' · confidence '+E(num(judgment.confidence,3)):''}<p>${E(judgment.why||judgment.reason||'No recorded model judgment for this candidate.')}</p><p>Main risk: ${E(judgment.main_risk||'Not supplied')}</p>${links.length?'<div class="n-evidence-links">'+links.join('')+'</div>':'<p class="n-small">No verified X references supplied.</p>'}</div>${positions.length?'<h3>Position ledger</h3>'+positions.map(p=>`<div class="n-judgment"><strong>${E(short(p.id))} · ${E(p.exit_status||(p.closed_at?'Closed':'Open'))}</strong><p>Remaining ${E(num(p.remaining??p.quantity,6))} · basis ${E(usd(p.remaining_cost_basis_usd??p.cost_basis))}</p><p>Realized ${E(signed(p.realized_pnl))} · unrealized ${E(signed(p.unrealized_pnl))}</p><p>Entry ${E(price(p.entry_price))} · current observed ${E(price(p.mark_price))}</p><p>Stop ${E(price(p.stop_price))} · next take ${E(price(p.next_take_price??p.take_price))} · trailing ${E(price(p.trailing_price))}</p><p>Quote age ${E(age(p.last_quote_at??p.last_observed_at))}; watcher ${E(dated(workspace?.worker?.last_watch_at))}</p></div>`).join(''):''}${closed.length?'<h3>Closed positions · authoritative book</h3>'+closed.map(p=>'<div class="n-detail-event"><strong>'+E(short(p.id))+' · CLOSED</strong><br>Remaining '+E(num(p.remaining,0))+' · realized '+E(signed(p.realized_pnl))+'<time>'+E(dated(p.closed_at))+'</time></div>').join(''):''}<h3>Committed paper fills</h3>${fills.length?fills.map(f=>`<div class="n-detail-event"><time>${E(dated(f.at||f.observed_at))}</time>${E(readable(f.kind||f.action))} · ${E(num(f.quantity,6))} at ${E(price(f.price||f.fill_price))}<br>${E(f.reason||'')} · realized ${E(signed(f.pnl))}</div>`).join(''):'<p class="n-small">No committed fill. A BUY judgment alone is not an entry.</p>'}`);
    }catch{if(token===detailGeneration)setHTML('n-detail-body',eventEvidence(row)+'<div class="n-drawer-error">Candidate evidence is unavailable. No missing check is assumed to have passed.</div>');}
  }
  setInterval(tick,1000);
  window.NeonWorkspace={configure(options){refreshCallback=options.refresh||refreshCallback;},setActive,update,disconnected,diagnostics:()=>({account_id:account,cursor,active,following:follow,pending_events:pending.size,notification_count:notifications.size})};
})();
