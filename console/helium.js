/* Helium's read-only cockpit uses NEON's presentation over its own account journal. */
(() => {
  'use strict';
  const root=document.getElementById('helium-panel');
  const E=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite=v=>v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v));
  const num=(v,d=2)=>finite(v)?Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
  const usd=v=>finite(v)?'$'+num(v):'Unavailable';
  const signed=v=>finite(v)?(Number(v)<0?'−':'+')+'$'+num(Math.abs(Number(v))):'Unavailable';
  const price=v=>finite(v)?'$'+num(v,Number(v)<.001?8:Number(v)<1?6:4):'Unavailable';
  const short=v=>v?String(v).slice(0,6)+'…'+String(v).slice(-5):'Unassigned';
  const words=v=>String(v??'not run').replace(/_/g,' ');
  const time=t=>finite(t)&&Number(t)>0?new Date(Number(t)*1000).toLocaleTimeString('en-GB',{timeZone:'Europe/Berlin',hour12:false}):'Unavailable';
  const dated=t=>finite(t)&&Number(t)>0?new Date(Number(t)*1000).toLocaleString('en-GB',{timeZone:'Europe/Berlin',hour12:false}):'Unavailable';
  const age=t=>{if(!finite(t)||Number(t)<=0)return 'unknown';const s=Math.max(0,Math.floor(Date.now()/1000-Number(t)));return s<60?s+'s':s<3600?Math.floor(s/60)+'m '+s%60+'s':Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m';};
  const tone=v=>finite(v)?Number(v)>0?'n-signed-positive':Number(v)<0?'n-signed-negative':'':'';
  const $=id=>document.getElementById(id);
  const text=(id,value)=>{const el=$(id);if(el&&el.textContent!==String(value??''))el.textContent=value??'';};
  const html=(id,value)=>{const el=$(id);if(el&&el.innerHTML!==value)el.innerHTML=value;};
  const svg=path=>`<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
  const icons={scout:'<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5M10 7v6M7 10h6"/>',filter:'<path d="M4 5h16l-6 7v6l-4 2v-8Z"/>',analyst:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/>',execution:'<path d="m13 3-8 11h7l-1 7 8-11h-7Z"/>',guard:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',list:'<path d="M8 6h12M8 12h12M8 18h12M3 6h1M3 12h1M3 18h1"/>'};
  const names={scout:'SCOUT',filter:'FILTER',analyst:'ANALYST',execution:'EXECUTION',guard:'GUARD'};
  const stages=Object.keys(names);
  let mounted=false,active=false,state=null,workspace={},account=null,lastReceipt=0,failed=false,baseline=true,lastSeq=0,chartRange=86400,chartSignature='',follow=true,reasonFilter='',pending=0,displayEvents=[],lastFocus=null,detailGeneration=0,detailReason='',nextDecisionBefore=null,detailBusy=false,gap=false;
  const pulseTimers=new Map();
  let processedSeq=0,eventGeneration=0,catchingUp=false;
  const cursorSeq=v=>{const tail=String(v??'').split(':').at(-1);return /^\d+$/.test(tail)?Number(tail):0;};
  const outcomeKey=e=>e.kind==='fill'?'filled':({skip:'rejected',watch:'blocked',buy:'passed',sell:'passed',ok:'passed',completed:'passed'}[String(e.outcome).toLowerCase()]||String(e.outcome||'observed').toLowerCase());

  function mount(){
    if(mounted)return;mounted=true;root.className='neon helium';
    root.innerHTML=`<div id="h-replay" class="n-replay" hidden><strong>REPLAY</strong><span>Isolated acceptance account · synthetic observations and paper fills.</span></div>
      <header class="n-top"><div class="n-identity"><div class="n-logo">${svg('<path d="M6 4v16M18 4v16M6 12h12"/>')}</div><div><h1>Helium</h1><div class="n-account"><span>Solana trading team</span><span id="h-account" class="n-mono">Reading account</span></div></div></div><div class="n-header-status"><div class="n-badges"><span id="h-data" class="n-pill">DATA CONNECTING</span><span class="n-pill h-paper">PAPER EXECUTION</span></div><span id="h-worker" class="n-pill" data-tone="warn">Reading workers</span><span id="h-last-worker" class="n-small">No completion received</span></div></header>
      <div id="h-uninitialized" class="n-blockers h-initialization" hidden><strong>Helium is waiting for its paper account</strong><p>Initialize Helium and start its scan and watch workers. This cockpit displays their recorded activity.</p></div>
      <section class="n-hero" aria-label="Helium account performance"><div class="n-hero-top"><div><div id="h-main-label" class="n-eyebrow">PAPER ACCOUNT EQUITY</div><div id="h-main-value" class="n-hero-value">—</div><div id="h-net" class="n-hero-net">Reading account performance</div><p id="h-cost-treatment" class="n-hero-caption">Recorded account values · execution costs and model charges shown separately.</p></div><div class="n-ranges" aria-label="Helium chart range"><button data-h-range="3600" aria-pressed="false">1H</button><button data-h-range="86400" aria-pressed="true">24H</button><button data-h-range="604800" aria-pressed="false">7D</button><button data-h-range="0" aria-pressed="false">ALL</button></div></div><div class="n-chart-wrap"><canvas id="h-performance-chart" role="img" aria-label="Helium equity from recorded account observations"></canvas><div id="h-chart-empty" class="n-chart-empty">Waiting for account observations</div></div><div class="n-chart-foot"><span id="h-chart-label">Recorded equity · missing valuations leave gaps</span><span id="h-chart-range">No history</span></div><div class="n-hero-bottom"><div><label>SPENDABLE CASH</label><strong id="h-cash">—</strong><small>Paper account balance</small></div><div><label>REALIZED TRADE P&amp;L</label><strong id="h-realized">—</strong><small>Before separate model charges</small></div><div><label>OPEN / CLOSED</label><strong id="h-position-count">— / —</strong><small>Committed position records</small></div><div><label>CLOSED WIN RATE</label><strong id="h-win-rate">—</strong><small id="h-win-denominator">No closed-trade evidence</small></div></div></section>
      <div id="h-ticker" class="n-ticker" aria-label="Helium recorded market observations"><strong>MARKET TAPE</strong><span class="n-ticker-empty">No recorded pool observations</span></div>
      <div class="n-grid"><section class="n-card n-pipeline-card"><div class="n-heading"><div class="n-heading-label">${svg(icons.execution)}<h2>Trading pipeline</h2></div><span class="n-scope">Helium workflow</span></div><div class="n-pipeline">${stages.map(id=>`<div id="h-stage-${id}" class="n-stage" data-stage="${id}"><div class="n-stage-icon">${svg(icons[id])}</div><div><div class="n-stage-title"><strong>${names[id]}</strong><span id="h-count-${id}" class="n-stage-count"></span></div><div id="h-sub-${id}" class="n-stage-sub">Waiting for recorded activity</div></div><div><div id="h-status-${id}" class="n-stage-status" data-state="not run">NOT RUN</div><span id="h-stage-time-${id}" class="n-stage-elapsed"></span></div></div>`).join('')}</div><div id="h-blockers" class="n-blockers" hidden></div><div class="n-pipeline-footer"><span id="h-last-scans">Scan / watch not recorded</span><span>60s worker cadence</span></div></section>
      <section class="n-card n-positions-card"><div class="n-heading"><div class="n-heading-label">${svg(icons.guard)}<h2>Open positions</h2></div><span id="h-open-label" class="n-scope">Reading account</span></div><div id="h-positions" class="n-position-list"></div><div class="n-stats-foot"><span id="h-watch-at">Watcher not recorded</span><span>Full stop / take-profit exits</span></div></section>
      <section class="n-card n-stream-card"><div class="n-heading"><div class="n-heading-label">${svg(icons.list)}<h2>Decision stream</h2></div><button id="h-follow" class="n-follow" aria-pressed="true">Following</button></div><div class="n-toolbar"><label class="n-search">${svg(icons.scout)}<input id="h-search" type="search" maxlength="100" placeholder="Token, mint or reason…" aria-label="Filter Helium event text"></label><label><select id="h-stage-filter" aria-label="Filter Helium stage"><option value="">All stages</option>${stages.map(id=>`<option value="${id}">${names[id]}</option>`).join('')}</select></label><label><select id="h-outcome-filter" aria-label="Filter Helium outcome"><option value="">All outcomes</option><option value="running">Running</option><option value="passed">Passed</option><option value="rejected">Rejected</option><option value="blocked">Blocked</option><option value="error">Error</option><option value="filled">Paper fills</option></select></label><button id="h-reset-filters">Reset</button></div><div id="h-active-filter" class="n-active-filter" hidden></div><div class="n-stream-head"><span>BERLIN</span><span>STAGE · DECISION · EVIDENCE</span></div><div id="h-log" class="n-log" tabindex="0" aria-label="Helium recorded decision stream"></div><div class="n-log-footer"><span id="h-log-total">No recorded events</span><button id="h-new" class="n-new" hidden>Show new events</button></div><div id="h-gap" class="h-stream-note" hidden>Some events are outside this recent window. The account journal retains the complete history.</div></section>
      <section class="n-card n-rejections-card"><div class="n-heading"><div class="n-heading-label">${svg(icons.filter)}<h2>Why candidates stop</h2></div><span class="n-scope">Account to date</span></div><div id="h-reasons" class="n-reason-list"></div><div class="n-stats-foot"><span id="h-candidate-total">No candidates recorded</span><span id="h-error-total">No worker errors recorded</span></div></section>
      <section class="n-card n-full h-operating"><div class="n-heading"><div class="n-heading-label">${svg(icons.guard)}<h2>Operating account</h2></div><span class="n-scope">Independent Helium account</span></div><div class="h-operating-body"><div><div class="h-mode-buttons" role="group" aria-label="Execution account mode"><button disabled aria-pressed="true">PAPER</button><button disabled aria-describedby="h-live-reason">LIVE · UNAVAILABLE</button></div><p id="h-live-reason" class="n-small">Live account activation is not available for Helium yet.</p><p id="h-execution-note" class="n-small">Eligible paper entries and exits run automatically in the workers.</p></div><details class="h-policy"><summary>Active strategy &amp; evidence policy</summary><div id="h-policy"></div></details></div></section>
      <details class="h-observations n-full"><summary>Recorded chart observations</summary><div id="h-observations" class="h-observation-scroll"></div></details>
      <footer class="n-connection-strip"><span id="h-source-age">Market observation unavailable</span><span id="h-transport">Read-only account view · waiting for snapshot</span></footer></div>
      <div id="h-toasts" class="n-toasts" role="status" aria-live="polite" aria-atomic="false"></div><dialog id="h-drawer" class="n-drawer" aria-labelledby="h-detail-title"><div class="n-drawer-head"><div><span class="n-eyebrow n-dim">RECORDED EVIDENCE</span><h2 id="h-detail-title">Helium candidate</h2><div id="h-detail-sub" class="n-small"></div></div><button id="h-close-drawer" class="n-drawer-close" aria-label="Close Helium evidence">×</button></div><div id="h-detail-body" class="n-drawer-body"></div></dialog>`;
    root.addEventListener('click',click);
    for(const id of ['h-stage-filter','h-outcome-filter'])$(id).addEventListener('change',renderJournal);
    $('h-search').addEventListener('input',renderJournal);
    $('h-log').addEventListener('scroll',()=>{if($('h-log').scrollTop>24&&follow){follow=false;followState();}},{passive:true});
    $('h-drawer').addEventListener('close',()=>{detailGeneration++;if(lastFocus?.isConnected)lastFocus.focus();});
    if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>drawChart(true)).observe($('h-performance-chart').parentElement);
  }
  function setActive(value){
    mount();const changed=active!==value;active=value;root.hidden=!value;document.body.classList.toggle('helium-active',value);
    if(changed&&!value){baseline=true;eventGeneration++;catchingUp=false;detailGeneration++;if($('h-drawer').open)$('h-drawer').close();$('h-toasts').replaceChildren();}
    if(changed&&value&&state){renderState();drawChart(true);}
  }
  function update(next){
    mount();if(next?.bot&&next.bot!=='helium')return;if(!active)setActive(true);
    const same=account===next.account_id,w=next.helium_workspace||{},seq=cursorSeq(w.cursor);
    if(same&&!baseline&&seq<lastSeq)return;
    if(!same){account=next.account_id;baseline=true;lastSeq=0;processedSeq=0;eventGeneration++;catchingUp=false;gap=false;follow=true;pending=0;reasonFilter='';displayEvents=[];chartSignature='';detailGeneration++;$('h-toasts').replaceChildren();if($('h-drawer').open)$('h-drawer').close();}
    const events=(w.events||[]).filter(e=>validEvent(e,account)).sort((a,b)=>b.seq-a.seq);
    const unseen=baseline?[]:events.filter(e=>e.seq>lastSeq);
    if(!baseline&&unseen.length&&Math.min(...unseen.map(e=>e.seq))>lastSeq+1)gap=true;
    state=next;workspace=w;lastReceipt=Date.now();failed=false;
    if(follow||baseline)displayEvents=events;else pending+=unseen.length;
    renderState();
    lastSeq=seq;
    if(baseline)processedSeq=seq;
    else if(!catchingUp){
      const available=events.filter(e=>e.seq>processedSeq).reverse();
      const complete=available.length===seq-processedSeq&&available.every((e,i)=>e.seq===processedSeq+i+1&&(e.kind!=='fill'||matchesFill(e)));
      if(complete){for(const e of available)receive(e);gap=false;}
      else if(processedSeq<seq){gap=true;catchUp();}
    }
    baseline=false;
  }
  function validEvent(e,owner){return e&&Number.isSafeInteger(e.seq)&&e.seq>0&&e.id===owner+':'+e.seq&&(!e.account_id||e.account_id===owner);}
  function matchesFill(e){return (state.fills||[]).some(f=>String(f.id)===String(e.fill_id)&&f.mint===e.mint&&f.side===e.side);}
  function receive(e){
    if(e.seq<=processedSeq)return;pulse(e.stage);
    if(e.kind==='fill'&&['BUY','SELL'].includes(e.side)&&Number.isSafeInteger(e.fill_id)&&e.fill_id>0&&finite(e.price)&&Number(e.price)>0&&finite(e.quantity)&&Number(e.quantity)>0)toast(e);
    processedSeq=e.seq;
  }
  async function catchUp(){
    if(catchingUp||!active||!account)return;catchingUp=true;renderJournal();const token=++eventGeneration,owner=account;
    try{
      for(let page=0;page<5&&processedSeq<lastSeq;page++){
        const after=owner+':'+processedSeq,response=await fetch('/api/helium-journal?'+new URLSearchParams({after,limit:'200'}),{cache:'no-store',signal:AbortSignal.timeout(10000)});
        if(!response.ok)throw new Error('Journal read failed');const result=await response.json();
        if(!active||token!==eventGeneration||account!==owner)return;
        if(result.account_id!==owner||result.prev_cursor!==after)throw new Error('Journal account or cursor changed');
        let previous=processedSeq;const rows=result.events||[];
        for(const e of rows){if(!validEvent(e,owner)||e.seq!==previous+1)throw new Error('Journal sequence gap');previous=e.seq;}
        if(result.cursor!==owner+':'+previous||previous<=processedSeq)throw new Error('Journal did not advance');
        for(const e of rows){if(e.seq>lastSeq)break;receive(e);}
      }
      gap=processedSeq<lastSeq;
    }catch{if(active&&token===eventGeneration&&account===owner)gap=true;}
    finally{if(token===eventGeneration){catchingUp=false;renderJournal();}}
  }
  function renderState(){
    if(!state||!active)return;const s=state.summary||{},w=workspace,counts=w.counts||{},worker=w.worker||{},initialized=state.initialized!==false;
    $('h-uninitialized').hidden=initialized;$('h-replay').hidden=!state.demo;text('h-account',account?'Account '+short(account):'Account not initialized');$('h-account').title=account||'';
    const accountingUnresolved=s.accounting_unresolved_positions??s.unresolved_positions??0,sourceStale=s.source_stale_positions||0,valuationCurrent=s.valuation_current!==false;
    text('h-main-label',sourceStale?'RECORDED PAPER EQUITY':accountingUnresolved?'CONSERVATIVE PAPER EQUITY':'PAPER ACCOUNT EQUITY');
    const value=finite(s.equity)?num(Math.abs(Number(s.equity))):null;
    html('h-main-value',value?`${Number(s.equity)<0?'−':''}$${E(value.split('.')[0])}<span class="n-decimal">.${E(value.split('.')[1]||'00')}</span>`:'<span class="n-decimal">Unavailable</span>');
    const known=finite(s.net_pnl),pnl=known?s.net_pnl:s.paper_estimated_net_pnl,pct=valuationCurrent&&finite(pnl)&&Number(s.starting_cash)>0?100*Number(pnl)/Number(s.starting_cash):null;
    html('h-net',valuationCurrent?`${E(signed(pnl))}<span>${known?'net paper P&amp;L':'estimated paper P&amp;L'}${finite(pct)?' · '+E(num(pct))+'% return':''}</span>`:'Current P&amp;L unavailable<span>waiting for fresh position prices</span>');
    text('h-cost-treatment',!initialized?'Account observations will appear after the workers start.':(s.unknown_api_bills?'Full net P&L awaits model bills; equity includes provisional cost allowances. ':'Equity includes modeled execution costs and known model charges. ')+(accountingUnresolved?num(accountingUnresolved,0)+' positions have unresolved accounting marks and conservative zero value; holdings remain open.':sourceStale?num(sourceStale,0)+' positions lack current source prices. Displayed equity retains the recorded accounting marks; current valuation is incomplete.':'Reserved rent is excluded from spendable cash.'));
    text('h-cash',usd(s.cash));text('h-realized',signed(s.realized_pnl));text('h-position-count',`${num(counts.open_positions??state.positions?.length,0)} / ${num(counts.closed_positions??s.completed_trades,0)}`);
    text('h-win-rate',finite(s.win_rate)?num(Number(s.win_rate)*100,1)+'%':'—');text('h-win-denominator',finite(s.completed_trades)?num(s.completed_trades,0)+' completed trades':'No completed trades');
    text('h-open-label',num(state.positions?.length??0,0)+' OPEN');text('h-watch-at','Watch completed '+time(worker.last_watch_at));text('h-last-scans',`Scan ${time(worker.last_scan_at)} · Watch ${time(worker.last_watch_at)}`);
    const blockers=[...new Set([...(worker.blockers||[]),...(state.status?.team_gate?.blockers||[])])];$('h-blockers').hidden=!blockers.length;
    html('h-blockers','<strong>'+E(initialized?'Waiting / blocked':'Account setup required')+'</strong><ul>'+blockers.map(b=>'<li>'+E(b)+'</li>').join('')+'</ul>');
    for(const id of stages){const row=(w.pipeline||[]).find(p=>p.id===id)||{},status=words(row.state||'not_run').toLowerCase();text('h-status-'+id,status.toUpperCase());$('h-status-'+id).dataset.state=status;$('h-stage-'+id).dataset.state=status;text('h-count-'+id,finite(row.count)?num(row.count,0)+' recorded':'');html('h-sub-'+id,E(row.reason||'No recorded activity')+(row.mint?' · <button data-h-mint="'+E(row.mint)+'">'+E(row.symbol||short(row.mint))+'</button>':''));$('h-sub-'+id).title=row.reason||'';text('h-stage-time-'+id,row.updated_at?time(row.updated_at):'');}
    text('h-candidate-total',num(counts.candidates??0,0)+' candidates · '+num(counts.rejected??0,0)+' rejected');text('h-error-total',num(counts.errors??0,0)+' recorded errors');
    const live=w.execution||{};text('h-live-reason',(live.blockers||[]).join(' · ')||'Helium live account selection and activation are not available yet.');text('h-execution-note','Eligible paper entries and exits run automatically. Paper history stays with this account.');
    renderPolicy();renderPositions();renderTicker();renderReasons();renderJournal();drawChart();tick();
  }
  function renderPolicy(){
    const c=state.config||{},t=state.team?.policy||{};
    const rows=[['Baseline','Krypton · '+(c.version||'unrecorded')],['Position cap',finite(c.max_position_pct)?num(Number(c.max_position_pct)*100)+'% of conservative equity':'Unavailable'],['Stop / full take profit',finite(c.stop_loss_pct)&&finite(c.take_profit_pct)?'−'+num(Number(c.stop_loss_pct)*100)+'% / +'+num(Number(c.take_profit_pct)*100)+'%':'Unavailable'],['Daily loss halt',finite(c.max_daily_loss_pct)?num(Number(c.max_daily_loss_pct)*100)+'% · Berlin day':'Unavailable'],['Liquidity / pool age',usd(c.min_liquidity_usd)+' / '+num(c.min_pool_age_minutes,0)+' min'],['Grok risk limit',finite(c.grok_max_risk)?num(c.grok_max_risk,0)+' / 10':'Unavailable'],['Review model',t.review_model||t.model||'Unrecorded'],['Review acceptance',finite(t.min_probability??t.jev_probability_min)?num(Number(t.min_probability??t.jev_probability_min)*100,0)+'% minimum':'Unrecorded']];
    html('h-policy',rows.map(([k,v])=>`<div class="h-policy-row"><span>${E(k)}</span><b>${E(v)}</b></div>`).join('')+'<p>Take profit closes the full position. This baseline has no partial-exit or trailing rule. Reviewer probabilities describe evidence support.</p>');
  }
  function renderPositions(){
    const rows=state.positions||[];if(!rows.length){html('h-positions','<div class="n-empty"><div class="n-empty-symbol">◇</div><strong>No open paper positions</strong>Entries appear after the account records a fill.</div>');return;}
    html('h-positions',rows.map(p=>{const fresh=p.mark_current===true&&!p.latest_quote_failed,observed=p.observed_price??p.mark?.quote?.price,at=p.observed_at??p.mark?.at;return `<article class="n-position"><div class="n-position-head"><button data-h-mint="${E(p.mint)}">${E(p.symbol||short(p.mint))}<small>${E(short(p.pool))} · pinned pool</small></button><strong class="n-mono ${fresh?tone(p.unrealized_pnl):''}" data-h-pnl-at="${E(at)}">${fresh?E(signed(p.unrealized_pnl)):'P&amp;L unresolved'}</strong></div><div class="n-position-metrics"><div><span>Entry price</span><b>${E(price(p.entry_price))}</b></div><div><span>${fresh?'Observed price':'Last observed'}</span><b>${E(price(observed))}</b></div><div><span>Position size</span><b>${E(num(p.quantity,5))}</b></div><div><span>Cost basis</span><b>${E(usd(p.basis))}</b></div><div><span>Stop trigger</span><b>${E(price(p.stop_price))}</b></div><div><span>Full take profit</span><b>${E(price(p.take_price))}</b></div><div><span>Trailing</span><b>Not configured</b></div><div><span>Reserved rent</span><b>${E(usd(p.rent_reserved))}</b></div></div><div class="n-position-note" data-h-quote-at="${E(at)}">${fresh?'Observed '+E(age(at))+' ago':'STALE / MISSING PRICE · holding retained; no sale assumed'}</div></article>`;}).join(''));
  }
  function renderTicker(){
    const rows=state.market||[];html('h-ticker','<strong>MARKET TAPE</strong>'+(rows.slice(0,6).map(m=>`<button class="n-ticker-item" data-h-mint="${E(m.mint)}"><span>${E(m.symbol||short(m.mint))}</span><b class="n-mono">${E(price(m.price))}</b><small data-h-market-at="${E(m.observed_at)}">${E(time(m.observed_at))} · ${E(words(m.state||'observed'))}</small></button>`).join('')||'<span class="n-ticker-empty">No recorded pool prices</span>'));
  }
  function renderReasons(){
    const rows=workspace.rejections||[],max=Math.max(1,...rows.map(r=>Number(r.count)||0));
    html('h-reasons',rows.length?rows.map((r,i)=>`<button class="n-reason" data-h-reason="${i}" aria-label="Show decisions: ${E(r.reason)} (${E(r.count)})"><span class="n-reason-label"><span>${E(r.reason)}</span><b>${E(num(r.count,0))}</b></span><progress class="h-reason-progress" value="${Math.max(0,Number(r.count)||0)}" max="${max}">${E(r.count)}</progress></button>`).join(''):'<div class="n-empty">No rejected candidates recorded.</div>');
  }
  function renderJournal(){
    if(!mounted)return;const query=$('h-search').value.trim().toLowerCase(),stage=$('h-stage-filter').value,outcome=$('h-outcome-filter').value;
    const rows=displayEvents.filter(e=>(!query||[e.symbol,e.mint,e.reason].join(' ').toLowerCase().includes(query))&&(!stage||stage===e.stage)&&(!outcome||outcome===outcomeKey(e))&&(!reasonFilter||e.reason===reasonFilter));
    const scroll=$('h-log').scrollTop;
    html('h-log',rows.map(e=>`<button class="n-log-row" data-h-event="${E(e.id)}"><time title="${E(dated(e.at))}">${E(time(e.at))}</time><div class="n-log-main"><div class="n-log-top"><strong>${E(e.symbol||e.mint&&short(e.mint)||names[e.stage]||'WORKER')}</strong><span class="n-event-tag" data-state="${E(outcomeKey(e))}">${E(e.kind==='fill'?'PAPER '+e.side:words(e.outcome).toUpperCase())}</span><span class="n-log-mint">${E(names[e.stage]||words(e.stage))}</span></div><div class="n-log-message">${E(e.kind==='fill'?(e.side==='BUY'?'Paper entry recorded':'Paper position closed')+' · '+num(e.quantity,5)+' at '+price(e.price)+(e.reason?' · '+words(e.reason):''):e.reason||words(e.kind))}</div></div></button>`).join('')||'<div class="n-empty">'+(reasonFilter?'No matching events in the recent stream. Open the rejection evidence to inspect its recorded decisions.':'No recorded events match this view.')+'</div>');
    $('h-log').scrollTop=follow?0:scroll;
    text('h-log-total',`${rows.length} shown · ${displayEvents.length} recent / ${num(workspace.total_events??workspace.counts?.events??0,0)} recorded`);text('h-active-filter',reasonFilter?'Reason: '+reasonFilter:'');$('h-active-filter').hidden=!reasonFilter;$('h-gap').hidden=!(gap||workspace.events_truncated);text('h-gap',gap?(catchingUp?'Reading missed account events from the journal…':'Earlier events await journal catch-up. Automatic retry on the next snapshot; notifications are not assumed.'):'Showing the recent event window. Older decisions are available through candidate and rejection evidence.');followState();
  }
  function followState(){text('h-follow',follow?'Following':'Paused');$('h-follow').setAttribute('aria-pressed',String(follow));$('h-new').hidden=!pending;text('h-new','Show '+pending+' new events');}
  function pulse(stage){
    if(!active||!stages.includes(stage))return;const el=$('h-stage-'+stage);el.classList.add('n-arrived');clearTimeout(pulseTimers.get(stage));pulseTimers.set(stage,setTimeout(()=>el.classList.remove('n-arrived'),650));
  }
  function toast(e){
    const el=document.createElement('div');el.className='n-toast';el.innerHTML=`${svg(icons[e.side==='BUY'?'execution':'guard'])}<div><strong>${E(e.side==='BUY'?'Paper entry committed':'Paper position closed')}</strong><p>${E(e.symbol||short(e.mint))} · ${E(num(e.quantity,5))} at ${E(price(e.price))}<br>${E(dated(e.at))}</p></div><button aria-label="Dismiss fill notification">×</button>`;el.querySelector('button').addEventListener('click',()=>el.remove());$('h-toasts').append(el);while($('h-toasts').children.length>3)$('h-toasts').firstElementChild.remove();setTimeout(()=>el.remove(),8000);
  }
  function drawChart(force=false){
    if(!active||!state)return;const all=(state.equity_history||[]).slice().sort((a,b)=>a.observed_at-b.observed_at),end=Math.max(0,...all.map(p=>Number(p.observed_at)||0));
    const points=all.filter(p=>!chartRange||p.observed_at>=end-chartRange),canvas=$('h-performance-chart'),rect=canvas.getBoundingClientRect();if(!rect.width||!rect.height)return;
    const signature=JSON.stringify([chartRange,rect.width,rect.height,points.map(p=>[p.observed_at,p.chart_equity])]);if(!force&&signature===chartSignature)return;chartSignature=signature;
    const dpr=window.devicePixelRatio||1;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);const width=rect.width,height=rect.height;
    const valid=points.filter(p=>finite(p.chart_equity));$('h-chart-empty').hidden=valid.length>1;text('h-chart-empty',valid.length===1?'One recorded valuation · waiting for the next observation':'No resolved equity observations in this range');text('h-chart-range',points.length?time(points[0].observed_at)+' — '+time(points.at(-1).observed_at):'No history');
    text('h-chart-label','Recorded equity · '+num(points.length,0)+' / latest '+num(workspace.history_limit??500,0)+' retained · gaps unfilled');
    html('h-observations','<table><thead><tr><th>Berlin observation</th><th>Equity</th><th>Net P&amp;L</th><th>Unresolved</th></tr></thead><tbody>'+points.slice(-20).reverse().map(p=>`<tr><td>${E(dated(p.observed_at))}</td><td>${finite(p.chart_equity)?E(usd(p.chart_equity)):'Unresolved'}</td><td>${E(signed(p.net_pnl))}</td><td>${E(num(p.unresolved_positions,0))}</td></tr>`).join('')+'</tbody></table><p>Latest 20 returned observations in the selected range. The view returns at most '+E(num(workspace.history_limit??500,0))+' recent observations; a selected 24H or 7D range may contain less history. Model bills must be reconciled for full net P&amp;L.</p>');
    if(valid.length<2)return;
    let lo=Math.min(...valid.map(p=>Number(p.chart_equity))),hi=Math.max(...valid.map(p=>Number(p.chart_equity)));const pad=Math.max((hi-lo)*.15,.01);lo-=pad;hi+=pad;
    const start=points[0].observed_at,finish=Math.max(start+1,points.at(-1).observed_at),x=t=>8+(t-start)/(finish-start)*(width-16),y=v=>10+(hi-v)/(hi-lo)*(height-24);
    ctx.strokeStyle='#c5f6e02b';ctx.lineWidth=1;ctx.setLineDash([2,6]);for(let i=0;i<3;i++){ctx.beginPath();ctx.moveTo(0,12+i*(height-24)/2);ctx.lineTo(width,12+i*(height-24)/2);ctx.stroke();}ctx.setLineDash([]);
    ctx.strokeStyle='#e2fff4';ctx.lineWidth=1.8;ctx.beginPath();let previous=null;for(const p of points){if(!finite(p.chart_equity)){previous=null;continue;}if(!previous||p.observed_at-previous.observed_at>180)ctx.moveTo(x(p.observed_at),y(Number(p.chart_equity)));else ctx.lineTo(x(p.observed_at),y(Number(p.chart_equity)));previous=p;}ctx.stroke();
    for(const p of valid){ctx.fillStyle='#e7fff6';ctx.beginPath();ctx.arc(x(p.observed_at),y(Number(p.chart_equity)),1.8,0,Math.PI*2);ctx.fill();}
  }
  function tick(){
    if(!active||!state)return;const connected=!failed&&lastReceipt&&Date.now()-lastReceipt<15000,d=workspace.data||{},w=workspace.worker||{},now=Date.now()/1000;
    const observed=Number(d.observed_at),fresh=finite(d.observed_at)&&observed>0&&now-observed>=0&&now-observed<=Number(d.max_age_seconds||60);
    const status=state.demo?'replay':!connected?'disconnected':d.state==='live'&&!fresh?'stale':d.state||'unavailable';
    text('h-data',status==='live'?'LIVE DATA':status==='stale'?'STALE DATA':status==='replay'?'REPLAY DATA':status==='disconnected'?'DATA DISCONNECTED':'DATA UNAVAILABLE');$('h-data').dataset.tone=status==='live'?'good':status==='disconnected'?'error':'warn';
    let worker=!connected?'disconnected':w.state||'not_started';const complete=Math.max(Number(w.last_scan_at)||0,Number(w.last_watch_at)||0);if(['idle','running'].includes(worker)&&complete&&now-complete>180)worker='overdue';
    text('h-worker',words(worker).toUpperCase());$('h-worker').dataset.tone=['idle','running'].includes(worker)?'good':['error','disconnected'].includes(worker)?'error':'warn';text('h-last-worker',complete?'Last completion '+age(complete)+' ago':'No recorded worker completion');
    text('h-source-age','Market observed '+(observed?'at '+time(observed)+' · '+age(observed)+' ago':'at an unknown time')+' · refresh is separate from market freshness');
    text('h-transport',(connected?'Console connected':'Console disconnected')+' · snapshots every 5s · times in Berlin');
    for(const el of root.querySelectorAll('[data-h-quote-at]')){const at=Number(el.dataset.hQuoteAt);if(!at||now-at>Number(state.config?.quote_max_age_seconds||60))el.textContent='STALE / MISSING PRICE · holding retained; no sale assumed';}
    for(const el of root.querySelectorAll('[data-h-pnl-at]')){const at=Number(el.dataset.hPnlAt);if(!at||now-at>Number(state.config?.quote_max_age_seconds||60)){el.textContent='P&L unresolved';el.classList.remove('n-signed-positive','n-signed-negative');}}
    for(const el of root.querySelectorAll('[data-h-market-at]')){const at=Number(el.dataset.hMarketAt),fresh=at>0&&now-at>=0&&now-at<=Number(state.config?.quote_max_age_seconds||60);el.textContent=time(at)+' · '+(state.demo?'replay':fresh?'fresh':'stale');}
    const expired=(state.positions||[]).some(p=>{const at=Number(p.observed_at??p.mark?.at);return !at||now-at<0||now-at>Number(state.config?.quote_max_age_seconds||60);});
    if(expired&&state.summary?.valuation_current!==false){text('h-main-label','RECORDED PAPER EQUITY');html('h-net','Current P&amp;L unavailable<span>waiting for fresh position prices</span>');text('h-cost-treatment','Displayed equity is the last received accounting value. Current position prices are stale; holdings remain open.'+(state.summary?.unknown_api_bills?' Model bills also remain unresolved.':''));}
  }
  function disconnected(){failed=true;tick();}
  function decisionHTML(d){
    const p=d.payload||d,checks=p.checks||[],grok=p.grok||{},review=p.review||{};
    return `<section class="h-decision-evidence"><div class="n-detail-event"><strong>${E(d.action)} · ${E(dated(d.at))}</strong><p>${E(p.reason||'Reason not recorded')}</p><span>Decision ${E(d.id)} · ${E(short(d.mint))}</span></div>${checks.length?'<div class="n-evidence-grid">'+checks.map(c=>`<div class="n-check"><div class="n-check-head"><strong>${E(words(c.name))}</strong><span class="n-check-result" data-result="${E(String(c.state).toLowerCase())}">${E(c.state)}</span></div><div class="n-check-value">${E(c.reason||c.detail||'')}</div></div>`).join('')+'</div>':''}${finite(grok.risk)?'<p class="h-evidence-note">Grok risk '+E(num(grok.risk,0))+'/10 · '+E(grok.rationale||grok.reason||'Recorded bound analysis')+'</p>':''}${review.decision?'<p class="h-evidence-note">Jev: '+E(review.decision)+' · evidence review result</p>':''}<details class="h-record"><summary>Recorded evidence fields</summary><pre>${E(JSON.stringify(p,null,2))}</pre></details></section>`;
  }
  function fillHTML(f){return `<div class="n-detail-event"><strong>${E(f.side)} · ${E(price(f.payload?.fill_price))}</strong><p>${E(num(f.payload?.quantity,6))} units · ${E(dated(f.at))}</p><span>Fill ${E(f.id)}${f.payload?.reason?' · '+E(words(f.payload.reason)):''}</span></div>`;}
  function openDrawer(mint,eventId,reason){
    lastFocus=document.activeElement;detailReason=reason||'';nextDecisionBefore=null;detailBusy=false;const event=displayEvents.find(e=>e.id===eventId)||(workspace.events||[]).find(e=>e.id===eventId),decisions=(state.decisions||[]).filter(d=>reason?d.payload?.reason===reason:mint&&d.mint===mint),fills=(state.fills||[]).filter(f=>mint&&f.mint===mint);
    text('h-detail-title',reason?'Recorded rejection':event?.symbol||((state.positions||[]).find(p=>p.mint===mint)?.symbol)||'Helium candidate');text('h-detail-sub',reason||mint||event?.kind||'Account observation');
    let content=event?`<h3>Event</h3><div class="n-judgment"><strong>${E(names[event.stage]||words(event.stage))} · ${E(words(event.outcome))}</strong><p>${E(event.reason||words(event.kind))}</p><p>${E(dated(event.at))} · ${E(event.id)}</p></div>`:'';
    if(mint)content+=`<div class="n-drawer-address">${E(mint)}</div>`;
    content+='<h3>Recorded decisions</h3><div id="h-detail-decisions">'+(decisions.map(decisionHTML).join('')||'<p class="n-small">Reading the candidate journal…</p>')+'</div>';
    content+='<div id="h-detail-artifacts"></div><div id="h-detail-fills">'+(fills.length?'<h3>Committed paper fills</h3>'+fills.map(fillHTML).join(''):'')+'</div><p id="h-detail-loading" class="h-evidence-note">Reading account evidence…</p><button id="h-detail-older" class="n-load-older" hidden>Load older matching decisions</button>';
    html('h-detail-body',content);if(!$('h-drawer').open)$('h-drawer').showModal();
    if(mint||reason)loadDetail(mint,reason);else text('h-detail-loading','Worker observations have no candidate decision.');
  }
  async function loadDetail(mint,reason,before=null){
    if(detailBusy)return;detailBusy=true;const token=++detailGeneration,owner=account,params=new URLSearchParams(reason?{reason,limit:'100'}:{mint});if(before)params.set('before',String(before));
    text('h-detail-loading','Reading account evidence…');$('h-detail-older').disabled=true;
    try{
      const response=await fetch((reason?'/api/helium-journal?':'/api/helium-candidate?')+params,{cache:'no-store',signal:AbortSignal.timeout(10000)});
      if(!response.ok)throw new Error('Evidence request returned '+response.status);
      const data=await response.json();if(!active||token!==detailGeneration||account!==owner||!$('h-drawer').open)return;
      if(data.account_id!==owner)throw new Error('Evidence account changed');
      const decisions=data.decisions||[],fills=data.fills||[];
      if(before)$('h-detail-decisions').insertAdjacentHTML('beforeend',decisions.map(decisionHTML).join(''));
      else html('h-detail-decisions',decisions.map(decisionHTML).join('')||'<p class="n-small">No matching decision has been recorded.</p>');
      if(fills.length)html('h-detail-fills','<h3>Committed paper fills</h3>'+fills.map(fillHTML).join(''));
      if(data.artifacts?.length)html('h-detail-artifacts','<h3>Analysis and review records</h3>'+data.artifacts.map(a=>`<div class="n-detail-event"><strong>${E(words(a.kind))}</strong><p>${finite(a.risk)?'Risk '+E(num(a.risk,0))+'/10 · ':''}${a.decision?'Decision '+E(a.decision)+' · ':''}${E(dated(a.at))}</p><span class="n-drawer-address" title="${E(a.hash)}">Evidence ${E(short(a.hash))}</span></div>`).join(''));
      nextDecisionBefore=data.has_more_decisions?data.next_before:null;$('h-detail-older').hidden=!nextDecisionBefore;
      text('h-detail-loading',reason?num(data.total_matching_decisions??decisions.length,0)+' matching decisions recorded in this account.':(decisions.length>=Number(data.limits?.decisions||50)?'Showing the latest recorded candidate decisions.':'Evidence read from this Helium account.'));
    }catch(e){if(active&&token===detailGeneration&&account===owner)text('h-detail-loading','Full evidence is temporarily unavailable. Showing only the recorded snapshot; retry by reopening.');}
    finally{if(token===detailGeneration){detailBusy=false;if($('h-detail-older'))$('h-detail-older').disabled=false;}}
  }
  function click(e){
    const el=e.target.closest('button');if(!el||!root.contains(el))return;
    if(el.hasAttribute('data-h-range')){chartRange=Number(el.dataset.hRange);root.querySelectorAll('[data-h-range]').forEach(b=>b.setAttribute('aria-pressed',String(b===el)));drawChart(true);return;}
    if(el.hasAttribute('data-h-event')){const event=displayEvents.find(v=>v.id===el.dataset.hEvent);if(event)openDrawer(event.mint,event.id);return;}
    if(el.hasAttribute('data-h-mint')){openDrawer(el.dataset.hMint);return;}
    if(el.hasAttribute('data-h-reason')){const row=(workspace.rejections||[])[Number(el.dataset.hReason)];if(row){reasonFilter=row.reason;renderJournal();openDrawer(null,null,row.reason);}return;}
    if(el.id==='h-close-drawer'){$('h-drawer').close();return;}
    if(el.id==='h-detail-older'&&nextDecisionBefore){loadDetail(null,detailReason,nextDecisionBefore);return;}
    if(el.id==='h-reset-filters'){$('h-search').value='';$('h-stage-filter').value='';$('h-outcome-filter').value='';reasonFilter='';renderJournal();return;}
    if(el.id==='h-follow'||el.id==='h-new'){follow=el.id==='h-new'||!follow;if(follow){displayEvents=(workspace.events||[]).slice().sort((a,b)=>b.seq-a.seq);pending=0;renderJournal();}followState();}
  }
  window.HeliumView={render:update,update,setActive,disconnected,tick};
})();
