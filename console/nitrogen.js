/* NITROGEN presentation only. Durable account events drive all activity. */
'use strict';
window.NitrogenView=(()=>{
  const roles=['SEARCH','RISK','WHALE','SNIPER','EXIT','JUDGE'];
  const paths={SEARCH:'M10 17a7 7 0 1 1 0-14 7 7 0 0 1 0 14Zm5-2 6 6',RISK:'m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Zm-4 9 3 3 5-6',WHALE:'M3 15c4 0 4-9 8-9s4 9 10 9M3 20c4 0 4-5 8-5s4 5 10 5',SNIPER:'m13 2-9 12h7l-1 8 10-13h-8l1-7',EXIT:'m3 17 6-6 4 3 8-10M15 4h6v6',JUDGE:'m8 4 8 8m-5-11 8 8-4 4-8-8 4-4M12 10 3 19m8 2h10'};
  const $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const known=v=>v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v));
  const num=(v,d=2)=>known(v)?Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
  const money=v=>known(v)?(Number(v)<0?'−':'')+'$'+num(Math.abs(Number(v))):'—';
  const signed=v=>known(v)?(Number(v)>=0?'+':'−')+'$'+num(Math.abs(Number(v))):'—';
  const pct=v=>known(v)?num(Number(v)*100,0)+'%':'—';
  const time=t=>t?new Date(t*1000).toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
  const date=t=>t?new Date(t*1000).toLocaleString():'—';
  const icon=r=>`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[r]}"/></svg>`;
  let active=false,state=null,account=null,seq=0,baseline=true,offline=false,epoch=0,request=null,resize=null,raf=0,timers=new Set(),drawerKind=null,queue=[],toasting=false,moreBefore=null;
  const later=(fn,ms)=>{const t=setTimeout(()=>{timers.delete(t);if(active)fn();},ms);timers.add(t);return t;};
  const set=(id,v)=>{const el=$(id);if(el)el.textContent=v;};
  const token=mint=>state?.cockpit?.tokens?.find(t=>t.mint===mint);
  const symbol=mint=>token(mint)?.symbol|| (mint?mint.slice(0,4)+'…'+mint.slice(-4):'—');
  const initial=mint=>esc(symbol(mint).replace(/^\$/,'').slice(0,1));
  function mount(){
    $('nitrogen-panel').innerHTML=`<div class="ni-cockpit">
      <div class="ni-session"><time id="ni-time">—</time><button class="ni-head" data-detail="operations"><i></i> HEAD <span id="ni-head-state">waiting</span></button><div class="ni-session-right"><span id="ni-mode">—</span><span>7 roles</span><select id="ni-bot-select" aria-label="Choose bot"><option value="nitrogen">N₂ ▾</option>${[['robinhood','Radeon'],['solana','Xenon'],['neon','Neon'],['jev','Argon'],['krypton','Krypton'],['helium','Helium'],['radon','Radon · 8']].map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select></div></div>
      <header class="ni-brand"><div class="ni-identity">N₂</div><div class="ni-brand-copy"><h1>NITROGEN</h1><p>Solana · Jev judgment desk · 7 roles</p></div><button id="ni-worker" class="ni-status" data-detail="operations"><i></i><span>loading</span></button></header>
      <div class="ni-ticker" aria-label="Recorded token observations"><div id="ni-ticker-track"><span class="ni-muted">Waiting for token observations</span></div></div>
      <section class="ni-hero" aria-label="Account equity"><div class="ni-hero-top"><button class="ni-context" id="ni-context" data-detail="account">Equity · waiting for account</button><span id="ni-multiple" class="ni-multiple">— ×</span></div><div class="ni-equity-line"><button class="ni-equity" data-detail="account" aria-label="Equity and accounting details"><span id="ni-whole">—</span><span id="ni-cents"></span></button><span id="ni-result" class="ni-result" hidden></span></div><div class="ni-performance"><span id="ni-pnl">Waiting for account observations</span><span class="ni-winloss" id="ni-winloss">— W <span>/</span> — L</span></div><div class="ni-chart-wrap"><canvas id="ni-chart" role="img" aria-label="Recorded account equity; missing valuations remain gaps"></canvas><span id="ni-chart-empty">Waiting for equity observations</span></div></section>
      <section class="ni-card ni-desk"><div class="ni-card-heading"><h2>Desk <span>· 7 roles</span></h2><span id="ni-evidence" class="ni-evidence">Loading</span></div><div class="ni-roles">${roles.map(r=>`<button class="ni-role" data-role="${r}" data-detail="${r}"><span class="ni-role-icon">${icon(r)}</span><span class="ni-role-name"><b>${r}</b><small id="ni-sub-${r}">Waiting</small></span><span class="ni-role-middle" id="ni-middle-${r}"><span class="ni-row-message">Waiting for observations</span></span><span class="ni-role-badge" id="ni-badge-${r}">—</span></button>`).join('')}</div></section>
      <section class="ni-card ni-log"><button class="ni-card-heading ni-heading-button" data-detail="history"><h2>Decision log</h2><span id="ni-vetoes">— vetoes</span></button><div id="ni-log-rows"></div></section>
      <section class="ni-card ni-kills"><button class="ni-card-heading ni-heading-button" data-detail="rules"><h2>Killed by rule</h2><span id="ni-bestworst">No closed trades</span></button><div class="ni-kill-grid" id="ni-kill-grid"></div></section>
      <div class="ni-bottom"><span id="ni-foot-state">Loading NITROGEN</span><button data-detail="operations">Details +</button></div>
      <div class="ni-toast-host" aria-live="polite" aria-atomic="true"></div>
    </div><dialog id="ni-drawer" class="ni-drawer" aria-labelledby="ni-drawer-title"><div class="ni-drawer-heading"><h2 id="ni-drawer-title">Details</h2><button id="ni-close" aria-label="Close details">×</button></div><div id="ni-drawer-body"></div></dialog>`;
    $('nitrogen-panel').onclick=e=>{const b=e.target.closest('[data-detail]');if(b)open(b.dataset.detail);};
    $('ni-close').onclick=()=>{$('ni-drawer').close();drawerKind=null;document.body.classList.remove('nitrogen-detail-open');};
    $('ni-drawer').addEventListener('close',()=>{drawerKind=null;document.body.classList.remove('nitrogen-detail-open');});
    $('ni-bot-select').onchange=e=>document.querySelector(`.bot-button[data-bot="${e.target.value}"]`)?.click();
    resize=new ResizeObserver(()=>{cancelAnimationFrame(raf);raf=requestAnimationFrame(draw);});resize.observe($('ni-chart'));
  }
  function setActive(value){
    if(value===active)return;
    active=value;epoch++;document.body.classList.toggle('nitrogen-active',active);
    request?.abort();request=null;timers.forEach(clearTimeout);timers.clear();cancelAnimationFrame(raf);resize?.disconnect();resize=null;
    state=null;account=null;seq=0;baseline=true;offline=false;queue=[];toasting=false;
    $('ni-drawer')?.close();drawerKind=null;document.body.classList.remove('nitrogen-detail-open');
    if(active){$('nitrogen-panel').hidden=false;mount();freshness();}else $('nitrogen-panel').innerHTML='';
  }
  function freshness(){
    if(!active)return;
    set('ni-time',new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',hour12:false}));
    const c=state?.cockpit||{};let status=offline?'disconnected':!state?'loading':!state.initialized?'waiting':c.worker_state||'waiting';
    if(!offline&&state?.status?.finished_at&&Date.now()/1000-state.status.finished_at>3*(state.config?.poll_seconds||60)&&status==='updating')status='stale';
    $('ni-worker').dataset.state=status;set('ni-worker', '');$('ni-worker').innerHTML=`<i></i><span>${esc(status)}</span>`;
    set('ni-head-state',offline?'disconnected':c.head_state||'waiting');
    set('ni-foot-state',offline?'Disconnected · last recorded values':!state?'Reading account…':!state.initialized?'Not initialized · independent account':`${c.period?.label||'Account lifetime'} · ${status} · ${state.demo?'synthetic preview':'recorded paper activity'}`);
  }
  function render(s){
    if(!active||s.bot!=='nitrogen')return;
    if(s.cockpit?.account_id&&s.cockpit.account_id!==s.account_id)return;
    const changed=account!==s.account_id;
    if(changed){epoch++;baseline=true;seq=0;account=s.account_id;queue=[];request?.abort();request=null;timers.forEach(clearTimeout);timers.clear();toasting=false;document.querySelector('.ni-toast-host').innerHTML='';$('ni-result').hidden=true;$('ni-drawer').close();drawerKind=null;}
    if(!baseline&&Number(s.cockpit?.cursor?.split(':').at(-1)||0)<seq)return;
    const oldEquity=state?.summary?.equity;
    state=s;set('version',s.version?'v'+s.version+' · LOCAL / READ ONLY':'LOCAL / READ ONLY');const c=s.cockpit||{},m=s.summary||{},cfg=s.config||{};
    set('ni-mode',s.mode?String(s.mode).toUpperCase():'—');set('ni-evidence',s.demo?'SYNTHETIC PREVIEW':s.initialized?'PAPER ACCOUNT':'NOT INITIALIZED');
    $('ni-evidence').classList.toggle('ni-synthetic',!!s.demo);
    set('ni-context',`Equity · ${known(m.starting_cash)?money(m.starting_cash).replace('.00','')+' start':'no account'} · ${pct(cfg.max_position_pct)} max`);
    if(known(m.equity)){const fixed=num(Math.abs(Number(m.equity))).split('.');set('ni-whole',(Number(m.equity)<0?'−':'')+'$'+fixed[0]);set('ni-cents','.'+fixed[1]);}else{set('ni-whole','—');set('ni-cents','');}
    $('ni-whole').parentElement.dataset.digits=String($('ni-whole').textContent.length>10?'long':'normal');
    if(!baseline&&oldEquity!==m.equity){const el=$('ni-whole').parentElement;el.classList.remove('ni-number-change');void el.offsetWidth;el.classList.add('ni-number-change');}
    const multiple=known(m.equity)&&Number(m.starting_cash)>0?Number(m.equity)/Number(m.starting_cash):null;
    set('ni-multiple',known(multiple)?num(multiple,2)+'×':'— ×');$('ni-multiple').classList.toggle('ni-negative',known(multiple)&&multiple<1);
    const net=known(m.net_pnl)?Number(m.net_pnl):null;
    set('ni-pnl',net!==null?`${signed(net)} (${net>=0?'+':''}${num(net/Number(m.starting_cash)*100)}%)`:(s.initialized?'Net P&L unavailable':'Waiting for account observations'));
    $('ni-pnl').className=net!==null?(net<0?'ni-negative':'ni-positive'):'ni-muted';
    $('ni-winloss').innerHTML=`${num(m.wins,0)} W <span>/</span> ${num(m.losses,0)} L`;
    $('ni-ticker-track').innerHTML=c.tokens?.length?c.tokens.map(t=>`<button class="ni-tick" data-detail="token:${esc(t.mint)}" title="${esc(t.mint)} · ${Date.now()/1000-t.at>(cfg.quote_max_age_seconds||60)?'stale · ':''}observed ${esc(date(t.at))}"><span class="ni-token-icon">${initial(t.mint)}</span><b>${esc(t.symbol)}</b><span class="ni-price">${known(t.price)?'$'+Number(t.price).toLocaleString('en-US',{maximumSignificantDigits:5}):'—'}</span><span class="ni-change ${Number(t.change_pct)<0?'ni-negative':''}">${known(t.change_pct)?(Number(t.change_pct)>=0?'+':'')+num(t.change_pct,1)+'%':'—'}</span></button>`).join(''):'<span class="ni-muted">Waiting for token observations</span>';
    $('ni-ticker-track').classList.toggle('ni-scroll',$('ni-ticker-track').scrollWidth>$('ni-ticker-track').parentElement.clientWidth);
    const subs={SEARCH:'On-chain leads',RISK:`Liq ≥ $${num(Number(cfg.min_liquidity_usd)/1000,0)}k`,WHALE:'Tracked purchases',SNIPER:`${pct(cfg.max_position_pct)} max · approval`,EXIT:`−${num(cfg.exit?.stop_pct,0)}% · ${num(cfg.exit?.trail_pct,0)}% trail`,JUDGE:'Jev · 4 questions'};
    roles.forEach(r=>{
      const e=c.roles?.[r];set('ni-sub-'+r,subs[r]);
      let message=e?.message||(!s.initialized?'Account not initialized':'Waiting for observations');
      if(r==='SNIPER'&&c.pending)message='Awaiting approval';
      if(r==='EXIT'&&s.positions?.length&&!e)message=`Monitoring ${s.positions.length} position${s.positions.length>1?'s':''}`;
      if(r==='JUDGE'&&e?.outcome==='WATCH'){const l=s.leads?.find(l=>l.mint===e.mint&&l.state==='WATCH');if(l)message='WATCH · recheck '+time(l.due_at);}
      const p=r==='SNIPER'&&c.pending?s.orders.find(o=>o.status==='PENDING')?.payload.mint:e?.mint;
      const rejected=e?.action==='RISK_REJECTED'||(r==='JUDGE'&&e?.outcome==='PASS');
      $('ni-middle-'+r).innerHTML=`${p?`<span class="ni-token-icon">${initial(p)}</span><span class="ni-row-token ${rejected?'ni-struck':''}">${esc(symbol(p))}</span>`:''}<span class="ni-row-message ${rejected?'ni-negative':''}" title="${esc(message)}">${esc(message)}</span>`;
      let badge=c.observations_available?`${num(c.counts?.[r]||0,0)}${r==='SEARCH'?' leads':r==='RISK'?' killed':r==='WHALE'?' buys':''}`:'—';
      if(r==='SNIPER')badge=c.pending?`${c.pending} pending`:s.summary?.fill_count?`${s.summary.fill_count} fills`:'waiting';
      if(r==='EXIT')badge=s.initialized?`${s.positions?.length||0} open`:'—';
      if(r==='JUDGE')badge=c.observations_available?`${c.vetoes} vetoes`:'—';
      set('ni-badge-'+r,badge);
    });
    set('ni-vetoes',c.observations_available?`${c.vetoes} vetoes`:'— vetoes');
    $('ni-log-rows').innerHTML=(c.events||[]).filter(e=>e.action!=='PASS').slice(0,5).map(logRow).join('')||'<p class="ni-empty">Decisions appear here as the desk works.</p>';
    const rejects=c.rejections||[],max=Math.max(1,...rejects.map(r=>r.count));
    $('ni-kill-grid').innerHTML=rejects.length?rejects.slice(0,6).map(r=>`<div class="ni-kill"><span>${esc(r.label)}</span><b>${r.count}</b><i></i></div>`).join(''):`<p class="ni-empty">${c.observations_available?'No recorded rule rejections':'Waiting for observed decisions'}</p>`;
    $('ni-kill-grid').querySelectorAll('.ni-kill i').forEach((el,i)=>el.style.setProperty('--magnitude',rejects[i].count/max*100+'%'));
    set('ni-bestworst',known(c.best_trade)?`Best ${signed(c.best_trade)} · worst ${signed(c.worst_trade)}`:'No closed trades');
    $('ni-bestworst').title='Closed-trade net trading P&L · account lifetime';
    const events=(c.events||[]).filter(e=>e.account_id===account).sort((a,b)=>a.seq-b.seq);
    const last=events.at(-1)?.seq||0;
    if(baseline||offline){seq=last;baseline=false;offline=false;}
    else if(last>seq){
      const newer=events.filter(e=>e.seq>seq);
      if(newer[0]?.seq===seq+1)consume(newer);else void catchUp();
    }
    freshness();draw();if(drawerKind&&drawerKind!=='history')detail(drawerKind);
  }
  function logRow(e){return `<div class="ni-log-row" data-role="${esc(e.role)}" data-event-id="${esc(e.id)}"><i></i><time title="${esc(date(e.at))}">${time(e.at)}</time><b>${esc(e.role)}</b><span><strong>${esc(symbol(e.mint))}</strong> ${esc(e.message)}</span></div>`;}
  function consume(events){
    for(const e of events){if(e.account_id!==account||e.seq<=seq)continue;seq=e.seq;
      const row=document.querySelector(`#nitrogen-panel [data-role="${e.role}"].ni-role`);
      if(row){row.classList.remove('ni-activity');void row.offsetWidth;row.classList.add('ni-activity');}
      document.querySelector(`#ni-log-rows [data-event-id="${CSS.escape(e.id)}"]`)?.classList.add('ni-log-new');
      if(e.toast){queue.push(e);if(queue.length>6)queue.shift();}
    }showToast();
  }
  async function catchUp(){
    if(request||!active||!account)return;const bound=account,version=epoch;request=new AbortController();
    try{for(let page=0;page<10;page++){
      const cursor=bound+':'+seq;
      const res=await fetch('/api/nitrogen-journal?'+new URLSearchParams({after:cursor,limit:100}),{signal:AbortSignal.any([request.signal,AbortSignal.timeout(18000)]),cache:'no-store'});if(!res.ok)throw Error('Journal unavailable');
      const data=await res.json();if(!active||version!==epoch||bound!==account)return;
      if(data.account_id!==bound||data.prev_cursor!==cursor)throw Error('Journal account mismatch');
      consume(data.events);if(data.events.length<100)break;
    }}catch(e){if(e.name!=='AbortError'){baseline=true;set('ni-foot-state','Journal interrupted · notifications paused');}}finally{if(version===epoch)request=null;}
  }
  function showToast(){
    if(toasting||!queue.length||!active)return;const e=queue.shift();toasting=true;
    const tone=e.action==='STAGED'?'amber':e.outcome==='PASS'||e.action==='REJECTED'||Number(e.amount)<0?'coral':'mint';
    const host=document.querySelector('.ni-toast-host');host.innerHTML=`<button class="ni-toast" data-tone="${tone}" data-detail="${e.role}" data-event-id="${esc(e.id)}"><span class="ni-toast-icon">${initial(e.mint)}</span><span class="ni-toast-copy"><span class="ni-toast-source">NITROGEN · ${state?.mode?.toUpperCase()||'—'} <time>${time(e.at)}</time></span><b>${esc(e.title)}</b><span>${esc(symbol(e.mint))} · ${esc(e.message)}</span></span><strong>${known(e.amount)?signed(e.amount):e.action==='STAGED'?'PENDING':esc(e.outcome)}</strong></button>`;
    if(known(e.amount)){set('ni-result',signed(e.amount));$('ni-result').hidden=false;$('ni-result').classList.toggle('ni-negative',Number(e.amount)<0);later(()=>{$('ni-result').hidden=true;},2500);}
    later(()=>{host.querySelector('.ni-toast')?.classList.add('ni-leaving');later(()=>{host.innerHTML='';toasting=false;showToast();},250);},4500);
  }
  function draw(){
    if(!active||!$('ni-chart'))return;
    const canvas=$('ni-chart'),rect=canvas.getBoundingClientRect(),dpr=Math.min(devicePixelRatio||1,3);
    if(!rect.width||!rect.height)return;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);
    const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);const w=rect.width,h=rect.height;
    ctx.strokeStyle='rgba(138,165,168,.10)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(0,h-2);ctx.lineTo(w,h-2);ctx.stroke();
    const rows=state?.cockpit?.history||[],valid=rows.filter(p=>known(p.equity));
    $('ni-chart-empty').hidden=valid.length>0;set('ni-chart-empty',state?.initialized?'No recorded equity history yet':'Waiting for equity observations');
    if(!valid.length)return;
    const values=valid.map(p=>Number(p.equity));let lo=Math.min(...values),hi=Math.max(...values);const pad=Math.max((hi-lo)*.18,Math.abs(hi)*.001,.1);lo-=pad;hi+=pad;
    const start=rows[0].at,end=rows.at(-1).at,span=end-start||1;
    const xy=p=>[6+(p.at-start)/span*(w-12),h-8-(Number(p.equity)-lo)/(hi-lo)*(h-16)];
    let segments=[],part=[],prev=null;
    for(const p of rows){if(!known(p.equity)||prev&&p.at-prev.at>(state.cockpit.history_max_gap_seconds||180)){if(part.length)segments.push(part);part=[];}if(known(p.equity))part.push(xy(p));prev=p;}if(part.length)segments.push(part);
    const fill=ctx.createLinearGradient(0,0,0,h);fill.addColorStop(0,'rgba(42,207,163,.08)');fill.addColorStop(1,'rgba(42,207,163,0)');
    for(const points of segments){if(points.length<2)continue;
      ctx.beginPath();points.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.lineTo(points.at(-1)[0],h);ctx.lineTo(points[0][0],h);ctx.closePath();ctx.fillStyle=fill;ctx.fill();
      ctx.beginPath();points.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.strokeStyle='#2acfa3';ctx.lineWidth=Math.max(1.5,w/250);ctx.lineJoin='round';ctx.shadowColor='rgba(42,207,163,.48)';ctx.shadowBlur=6;ctx.stroke();ctx.shadowBlur=0;
    }
    if(known(rows.at(-1)?.equity)){const [x,y]=xy(rows.at(-1));ctx.beginPath();ctx.arc(x,y,Math.max(2,w/150),0,Math.PI*2);ctx.fillStyle='#2acfa3';ctx.shadowColor='#2acfa3';ctx.shadowBlur=9;ctx.fill();}
    canvas.setAttribute('aria-label',`${valid.length} recorded equity observations, ${date(rows[0].at)} to ${date(rows.at(-1).at)}. Latest ${money(rows.at(-1).equity)}. Missing valuations remain gaps.`);
  }
  function disconnected(){if(!active)return;offline=true;baseline=true;request?.abort();request=null;queue=[];freshness();}
  const json=x=>`<pre>${esc(JSON.stringify(x,null,2))}</pre>`;
  const pairs=items=>`<dl>${items.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>`;
  function open(kind){drawerKind=kind;document.body.classList.add('nitrogen-detail-open');detail(kind);$('ni-drawer').showModal();}
  function detail(kind){
    const s=state||{},c=s.cockpit||{},m=s.summary||{};let title=kind,body='';
    if(kind==='account'){title='Paper account';body=pairs([['Account',s.account_id||'Not initialized'],['Starting capital',money(m.starting_cash)],['Cash',money(m.cash)],['Equity',money(m.equity)],['Net P&L',signed(m.net_pnl)],['Realized trading P&L',signed(m.realized_pnl)],['Open unrealized P&L',signed(m.unrealized_pnl)],['Deployed basis',money(m.deployed_basis)],['Network costs',money(m.network_costs)],['Embedded modeled friction',money(m.embedded_friction)],['Model billed / reserved',money(m.model_billed)+' / '+money(m.model_reserved)],['Retained rent capital',money(m.rent_reserved)]])+`<p>Unknown prices and unknown model bills remain unresolved. The chart contains recorded account observations only.</p>`+json(s.positions||[]);}
    else if(kind==='JUDGE'){title='Jev · four answers';body=(s.assessments||[]).map(a=>{const x=a.response?.answers;return `<article><h3>${esc(symbol(a.dossier?.mint))} · ${esc(a.result||a.status)}</h3><p>${esc(date(a.finished||a.started))} · ${esc(a.reason)}</p>${x?pairs([['Selected action',x.action.choice],['P(action = buy)',String(x.action.probabilities.buy)],['Separate action confidence',String(x.action.confidence)],['P(organic = yes)',String(x.organic.noul)],['Late',x.late.noul<.5?'NO':'YES / TIE'],['P(late = yes)',String(x.late.noul)],['Exit risk · displayed 1–5',String(x.exit_risk.score+1)],['Exit risk · raw 0–4',String(x.exit_risk.score)]]):'<p>No model answer was returned.</p>'}<details><summary>Full distributions and sourced dossier</summary>${json({response:a.response,dossier:a.dossier})}</details></article>`;}).join('')||'<p>No assessments. Required evidence must pass before Jev is called.</p>';body+='<h3>WATCH rechecks</h3>'+json((s.leads||[]).filter(l=>l.state==='WATCH').map(l=>({mint:l.mint,watch_count:l.watch_count,due_at:date(l.due_at)})));}
    else if(kind==='SNIPER'){title='Staged paper orders';body='<p>A passing judgment stages an order. Operator approval is required before a paper fill. This screen is read-only.</p>'+(s.orders||[]).map(o=>`<article><h3>${esc(symbol(o.payload.mint))} · ${esc(o.status)}</h3>${pairs([['Order',o.id],['Mint',o.payload.mint],['Ticket',money(o.payload.ticket_usd)],['Expires',date(o.expires)],['Assessment',o.payload.assessment]])}${o.status==='PENDING'?`<b>APPROVE #${esc(o.id)}</b><code>python nitrogen_bot.py approve --id ${esc(o.id)}</code><code>python nitrogen_bot.py reject --id ${esc(o.id)}</code><p>Approval refreshes evidence, quote, cash and exposure.</p>`:''}</article>`).join('');}
    else if(kind==='history'){title='Decision history';body='<p>Recorded structured outcomes · newest first</p><div id="ni-full-log">'+(c.events||[]).map(logRow).join('')+'</div><button class="ni-more" id="ni-more">Load older events</button>';moreBefore=Math.min(...(c.events||[]).map(e=>e.seq));}
    else if(kind==='rules'){title='Killed by rule';body=`<p>${esc(c.period?.label||'Account lifetime')} · since ${esc(date(c.period?.since))}. Distinct candidate/rule pairs. Missing evidence is a blocker, not a rejection.</p>`+pairs((c.rejections||[]).map(r=>[r.label,r.count]))+json(s.judge_policy||{});}
    else if(kind.startsWith('token:')){const mint=kind.slice(6),t=token(mint);title=symbol(mint);body=pairs([['Exact Solana mint',mint],['Price',known(t?.price)?'$'+Number(t.price).toPrecision(5):'Unknown'],['Observed',date(t?.at)],['Price age',t?.at?Math.max(0,Math.floor(Date.now()/1000-t.at))+' seconds':'Unknown'],['Comparable one-hour change',known(t?.change_pct)?num(t.change_pct)+'%':'Unknown']])+json((s.leads||[]).filter(l=>l.mint===mint));}
    else if(kind==='EXIT'){title='Positions & recorded fills';body='<p>Exits run independently of entry approval and model availability. Missing marks retain unresolved holdings.</p><h3>Open positions</h3>'+json(s.positions||[])+'<h3>Recorded paper fills</h3>'+json(s.fills||[]);}
    else if(['SEARCH','RISK','WHALE'].includes(kind)){title=kind+' · evidence';body='<p>'+esc(s.status?.source_coverage||'No source observations')+'</p>'+pairs(Object.entries(s.pipeline||{}))+json((s.decisions||[]).filter(d=>d.action.startsWith(kind)))+'<h3>Candidate dossiers</h3>'+json((s.leads||[]).map(l=>({lead:l.lead_id,state:l.state,dossier:l.last_dossier})));}
    else{title='HEAD · operations';body=!s.initialized?'<p>No NITROGEN account initialized.</p><code>python nitrogen_bot.py init</code><p>Creates an independent $500 virtual account. Paid research remains disabled.</p>':'';body+=pairs([['Mode',s.mode?.toUpperCase()||'Unknown'],['Evidence',s.demo?'SYNTHETIC PREVIEW':'Real-data paper'],['Account',s.account_id||'None'],['Worker status',offline?'Disconnected':c.worker_state||'Waiting'],['Last completed worker',date(s.status?.finished_at)],...Object.entries(c.last_successful_at||{}).map(([kind,at])=>['Last successful '+kind,date(at)]),['Coverage',s.status?.source_coverage||'No provider observations']])+`<p>${esc([...(s.status?.blockers||[]),...(s.status?.model_gate?.blockers||[]),...(c.notes||[])].join(' · '))}</p><details><summary>Workers, providers and policy</summary>${json({runs:s.runs,providers:s.providers,config:s.config})}</details><details><summary>Evening report and observed outcomes</summary>${json(s.latest_report?.payload||{})}</details>`;}
    set('ni-drawer-title',title);$('ni-drawer-body').innerHTML=body;
    if(kind==='history')$('ni-more').onclick=loadOlder;
  }
  async function loadOlder(){
    if(!Number.isFinite(moreBefore))return;const version=epoch,bound=account;const ctrl=new AbortController();request?.abort();request=ctrl;
    $('ni-more').disabled=true;
    try{const res=await fetch('/api/nitrogen-journal?'+new URLSearchParams({before:moreBefore,limit:100}),{signal:AbortSignal.any([ctrl.signal,AbortSignal.timeout(18000)]),cache:'no-store'});if(!res.ok)throw Error();const data=await res.json();if(version!==epoch||bound!==account||data.account_id!==account||drawerKind!=='history')return;
      $('ni-full-log').insertAdjacentHTML('beforeend',data.events.map(logRow).join(''));moreBefore=data.next_before;
      $('ni-more').textContent=data.events.length?'Load older events':'End of recorded history';$('ni-more').disabled=!data.events.length;
    }catch(e){if(e.name!=='AbortError'&&active&&drawerKind==='history'){$('ni-more').textContent='History unavailable · retry';$('ni-more').disabled=false;}}finally{if(request===ctrl)request=null;}
  }
  return {setActive,render,freshness,disconnected};
})();
