'use strict';
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const finite = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
const number = (v, digits=2) => finite(v) ? Number(v).toLocaleString('en-US',{minimumFractionDigits:digits,maximumFractionDigits:digits}) : '—';
const usd = v => finite(v) ? '$' + number(v) : '—';
const signed = v => finite(v) ? (Number(v) >= 0 ? '+' : '−') + '$' + number(Math.abs(Number(v))) : '—';
const pct = v => finite(v) ? (Number(v) > 0 ? '+' : '') + number(v) + '%' : '—';
const tone = v => !finite(v) ? 'muted' : Number(v) > 0 ? 'positive' : Number(v) < 0 ? 'negative' : '';
const price = v => !finite(v) ? '—' : '$' + Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:Number(v)<.001?9:Number(v)<1?7:4});
const short = v => v ? String(v).slice(0,7)+'…'+String(v).slice(-5) : '—';
const stamp = (t, date=false) => t ? new Date(t*1000).toLocaleString('en-GB',{timeZone:'Europe/Berlin',...(date?{day:'2-digit',month:'short'}:{}),hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '—';
const age = t => {if(!t)return 'unknown';let s=Math.max(0,Math.floor(Date.now()/1000-t));return s<60?s+'s':s<3600?Math.floor(s/60)+'m '+s%60+'s':Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m';};
const pretty = v => String(v ?? 'unknown').replace(/[_:]/g,' ');
const html = (id, content) => {if($(id).innerHTML !== content) $(id).innerHTML=content;};
let bot='robinhood', state=null, chart=null, selectedAsset='', tab='positions', range=21600, query='', generation=0, loading=false, chartKey='', error=false;
let plot=null;
let wallets=null, walletError=false, walletLoading=false, selectedWallet='', walletDetail=null, walletDetailError=false, walletDetailRequest=0, walletDetailLoading='';

async function fetchJSON(path){const response=await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(18000)});const data=await response.json();if(!response.ok)throw new Error(data.error||'Console connection interrupted');return data;}
async function refresh(){
  if(loading)return;loading=true;const token=generation;
  try{
    const next=await fetchJSON('/api/state?bot='+bot);
    if(token!==generation)return;
    state=next;error=false;$('offline').hidden=true;
    if(!state.assets.some(a=>a.asset_id===selectedAsset))selectedAsset=(state.assets.find(a=>a.state==='open')||state.assets[0]||{}).asset_id||'';
    render();await Promise.all([loadChart(token),tab==='wallets'?loadWallets(token):Promise.resolve()]);
  }catch(e){if(token===generation){error=true;$('offline').textContent='Data connection interrupted. '+e.message+' Last displayed values are retained; they are not current. Automatic retry every 5 seconds.';$('offline').hidden=false;}}
  finally{loading=false;freshness();}
}
async function loadChart(token=generation){
  const a=state?.assets.find(a=>a.asset_id===selectedAsset);
  if(!a?.pool_id){chart=null;chartKey='';drawChart();return;}
  const key=bot+':'+a.asset_id+':'+a.pool_id;
  $('instrument-state').textContent=a.state.toUpperCase()+' · PAPER';
  if(chartKey!==key){chart=null;chartKey=key;clearCanvas($('price-chart'));$('quote-price').textContent='—';$('quote-age').textContent='Loading quote';$('chart-empty').hidden=false;$('chart-empty').textContent='Loading recorded pool history…';}
  try{
    const next=await fetchJSON('/api/chart?'+new URLSearchParams({bot,asset:a.asset_id,pool:a.pool_id}));
    if(token!==generation||selectedAsset!==a.asset_id)return;
    chart=next;chartKey=key;drawChart();
  }catch(e){if(token===generation){chart=null;$('quote-price').textContent='—';$('quote-change').textContent='History unavailable';$('quote-age').textContent='Quote unavailable';html('ta','<span>Indicators unavailable until history reconnects</span>');$('chart-empty').hidden=false;$('chart-empty').textContent='Price history unavailable. The account ledger remains visible; retrying.';$('chart-source').textContent='History request failed';clearCanvas($('price-chart'));}}
}
async function loadWallets(token=generation){
  if(bot!=='robinhood'||walletLoading)return;
  walletLoading=true;
  try{
    const next=await fetchJSON('/api/wallets');
    if(token!==generation)return;
    wallets=next;walletError=false;
    if(selectedWallet&&walletDetailLoading!==selectedWallet&&walletDetail?.generated_at!==next.generated_at)loadWalletDetail(selectedWallet,token);
  }catch(e){if(token===generation)walletError=true;}
  finally{if(token===generation){walletLoading=false;if(tab==='wallets')renderActivity();}}
}
async function loadWalletDetail(wallet,token=generation){
  const request=++walletDetailRequest;walletDetailLoading=wallet;
  selectedWallet=wallet;walletDetail=null;walletDetailError=false;
  if(tab==='wallets')renderActivity();
  try{
    const next=await fetchJSON('/api/wallet-evidence?'+new URLSearchParams({wallet}));
    if(token!==generation||wallet!==selectedWallet||request!==walletDetailRequest)return;
    walletDetail=next;
  }catch(e){if(token===generation&&wallet===selectedWallet&&request===walletDetailRequest)walletDetailError=true;}
  finally{if(token===generation&&request===walletDetailRequest){walletDetailLoading='';if(tab==='wallets')renderActivity();}}
}
function metric(label,value,detail,cls=''){return `<div class="metric"><span>${esc(label)}</span><strong class="${cls}">${value}</strong><small>${detail}</small></div>`;}
function key(label,value,cls=''){return `<div class="key-row"><span>${esc(label)}</span><b class="${cls}">${value}</b></div>`;}
function ack(q){if(!q?.available)return '<span class="warning">Unavailable</span>';const pending=Object.entries(q.counts).filter(([k])=>!['sent','suppressed','expired'].includes(k)).reduce((n,[,v])=>n+v,0);return `<span class="${pending?'warning':'positive'}">${q.counts.sent||0} acknowledged${pending?' · '+pending+' unresolved':''}</span>`;}
function render(){
  const s=state.snapshot,p=state.policy,sol=bot==='solana';
  $('account-title').textContent=sol?'RUG DESK · Solana':'Wallet follower';$('chain').textContent=state.chain;
  $('account-id').textContent='Account '+short(state.account_id);$('account-id').title=state.account_id;
  $('version').textContent='v'+state.version+' · LOCAL / READ ONLY';
  $('strategy-note').textContent=sol?'Rugcheck → pinned pool → Grok / X → refreshed market → paper entry':`${number(p.min_liquidity_usd,0)} USD liquidity · ${p.min_independent_wallets} buying recipients / ${Number(p.max_confirmation_age_seconds)/60} min · current GoPlus response required`;
  const unresolved=s.unresolved_positions||0;
  const halt=s.entry_halt||state.status.entry_halt||(state.daily?.halted_at?state.daily.halt_reason:'');
  html('metrics',sol?
    metric('Account','Setup blocked','No Solana trial clock','warning')+metric('Realized P&L',signed(s.realized_pnl),'Separate Solana account',tone(s.realized_pnl))+metric('Open positions',String(s.open_positions),'From persistent book')+metric('Paper fills',String(state.fill_count),'Acceptance tests excluded')+metric('Paid Grok / X','Disabled','No paid requests authorized','warning'):
    metric(unresolved?'Conservative equity':'Net equity',usd(s.equity),`${usd(s.starting_cash)} starting cash${unresolved?' · '+unresolved+' unresolved':''}`)+
    metric('Net P&L',signed(s.net_pnl),`${pct(s.return_pct)} since trial start`,tone(s.net_pnl))+
    metric('Realized P&L',signed(s.realized_pnl),`${s.closed_positions} completed position${s.closed_positions===1?'':'s'}`,tone(s.realized_pnl))+
    metric('Unrealized P&L',signed(s.unrealized_pnl),unresolved?'Conservative; unresolved marks':`${s.open_positions} open · after modeled exit costs`,tone(s.unrealized_pnl))+
    metric('Available cash',usd(s.cash),`${usd(s.position_basis)} remaining cost basis`));
  html('notice',sol?`<strong>${esc(state.blocker)}</strong>`:`${halt?'<strong>New entries halted: '+esc(pretty(halt))+'.</strong> ':''}Live market observations · simulated fills, with modeled fees, gas and slippage. <strong>Wallet skill and controller independence are unverified; safety evidence remains incomplete.</strong>${unresolved?' Unresolved holdings remain open; zero conservative value is not a sale.':''}`);
  html('asset-select',state.assets.length?state.assets.map(a=>`<option value="${esc(a.asset_id)}"${a.asset_id===selectedAsset?' selected':''}>${esc(a.symbol)} / USD</option>`).join(''):'<option value="">No traded coins</option>');
  $('asset-select').disabled=!state.assets.length;
  const a=state.assets.find(a=>a.asset_id===selectedAsset);$('instrument-state').textContent=a? a.state.toUpperCase()+' · PAPER':'NO POSITIONS';
  html('guardrails',sol?key('Liquidity floor','$30,000')+key('LP locked / burned','≥ 90%')+key('Top ten / creator','≤ 30% / ≤ 5%')+key('Ticket / max positions','$25 / 4')+key('Stop / trailing','−35% / −30%')+key('X gate','Disabled · required','warning'):
    key('Liquidity floor',usd(p.min_liquidity_usd))+key('Buying recipients',`${p.min_independent_wallets} / ${p.max_confirmation_age_seconds/60} min`)+key('Position / account risk',`${p.per_position_risk_pct}% / ${p.max_account_risk_pct}%`)+key('Positions / entries per day',`${p.max_open_positions} / ${p.max_entries_per_day}`)+key('New entries',halt?'Halted':Date.now()/1000>=state.trial.ends_at?'Trial entry window ended':'Gated by current evidence',halt?'warning':'muted')+key('Stop / TP / trailing',`−${p.stop_loss_pct}% / +${p.take_profit_pct}% / −${p.trailing_stop_pct}%`));
  $('trial-return').textContent=pct(s.return_pct);$('trial-return').className=tone(s.return_pct);
  html('trial-detail',sol?'The seven-day Solana trial has not started.':`${s.closed_positions} closed · ${s.open_positions} open · ${usd(s.fees_paid)} modeled costs<br>Max recorded drawdown ${number(s.max_drawdown_pct)}% · sample too small to establish an edge`);
  html('health-mini',key('Trade alerts',ack(state.notifications.trades))+(!sol?key('Market alerts',ack(state.notifications.market))+key('Recorded monitoring gaps',`${state.gaps.length}`,state.gaps.length?'warning':''):key('Trial readiness','Blocked','warning')));
  $('open-count').textContent=s.open_positions;$('fill-count').textContent=sol?state.fill_count:state.fills.length;
  $('footer-id').textContent=`Account ${state.account_id} · ${sol?'Solana setup':state.trial.policy_version}`;
  renderActivity();renderEvidence();renderReasons();drawEquity();freshness();
}
function freshness(){
  $('clock').textContent=new Date().toLocaleTimeString('en-GB',{timeZone:'Europe/Berlin',hour12:false})+' CET';
  $('connection').textContent=error?'Disconnected':state?'Console connected':'Connecting…';$('connection').className='connection'+(error?' negative':'');
  if(!state)return;
  const finished=state.status.finished_at,old=!finished||Date.now()/1000-finished>state.cadence_seconds*3;
  $('worker-freshness').textContent=`Worker completed ${age(finished)} ago${old?' · overdue':''}${state.error_count?' · '+state.error_count+' cycle errors':''}`;$('worker-freshness').className='context-status'+(old||state.error_count?' warning':'');
  if(state.trial.ends_at){const remaining=state.trial.ends_at-Date.now()/1000;
    $('trial-state').textContent=remaining>0?`Paper trial · ${Math.floor(remaining/86400)}d ${Math.floor(remaining%86400/3600)}h remaining`:'Entry period ended · exits continue';
    $('trial-deadline').textContent='Ends '+new Date(state.trial.ends_at*1000).toLocaleString('en-GB',{timeZone:'Europe/Berlin',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'})+' CET';
  }else{$('trial-state').textContent='Trial not started';$('trial-deadline').textContent='Operational setup only';}
  const point=chart?.points.at(-1);if(point){const stale=Date.now()/1000-point.at>state.cadence_seconds*3;$('quote-age').textContent='Quote '+age(point.at)+' ago'+(stale?' · stale':'');$('quote-age').className='quote-age'+(stale?' warning':'');}
}
function matches(row){return !query||Object.values(row).some(v=>typeof v!=='object'&&String(v).toLowerCase().includes(query));}
function table(headers,body,note=''){return `<div class="table-scroll"><table><thead><tr>${headers.map(x=>`<th scope="col">${x}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>${note?`<div class="activity-note">${note}</div>`:''}`;}
function coin(symbol,asset){return `<button class="coin-button" data-asset="${esc(asset)}">${esc(symbol||short(asset))}</button>`;}
function renderActivity(){
  if(!state)return;
  $('filter').disabled=tab==='health';
  $('filter').placeholder=tab==='wallets'?'Filter address or role':'Filter coin or reason';
  $('activity').setAttribute('aria-labelledby','tab-'+tab);
  const s=state.snapshot;
  if(tab==='positions'){
    const positions=(s.positions||[]).filter(matches);
    if(!positions.length){html('activity',`<div class="empty">${query?'No positions match this filter.':state.setup_blocked?esc(state.blocker):'No open positions. The bot continues checking observed signals against the active policy.'}</div>`);return;}
    html('activity',table(['Coin / pool','Opened · Berlin','Quantity','Cost basis','Mark / entry','Net liquidation','Unrealized P&L','Exit state'],positions.map(p=>`<tr class="${selectedAsset===p.asset_id?'chosen':''}"><td>${coin(p.symbol,p.asset_id)}<span class="sub">${esc(short(p.pool_id))}</span></td><td>${stamp(p.opened_at)}<span class="sub">${age(p.opened_at)} held</span></td><td class="num">${number(p.qty,3)}</td><td class="num">${usd(p.remaining_basis)}</td><td class="num">${p.mark_status==='ok'?price(p.mark_price):'<span class="warning">Unresolved</span>'}<span class="sub">entry ${price(p.entry_price)}</span></td><td class="num">${usd(p.liquidation_value)}<span class="sub">${esc(p.mark_status)} · ${age(p.mark_at)} ago</span></td><td class="num ${tone(p.unrealized_pnl)}">${signed(p.unrealized_pnl)}<span class="sub ${tone(p.unrealized_pnl)}">${pct(p.unrealized_pct)}</span></td><td>${p.tp_done?'Trailing armed':'TP pending'}<span class="sub">stop ${price(p.stop_price)}</span></td></tr>`).join(''),'Net liquidation includes modeled sale costs. Quotes are periodic; stops are conditions checked by the worker, not exchange stop orders.'));
  }else if(tab==='fills'){
    const fills=state.fills.filter(matches);
    html('activity',fills.length?table(['Coin','Time · Berlin','Paper action','Quantity','Fill price','Cash change','Costs','Realized P&L','Reason'],fills.map(f=>`<tr><td>${coin(f.symbol,f.asset_id)}</td><td>${stamp(f.observed_at,true)}</td><td><span class="action ${f.action==='OPEN'?'positive':f.action==='PARTIAL'?'warning':'negative'}">${esc(f.action)}</span></td><td class="num">${number(f.quantity,3)}</td><td class="num">${price(f.fill_price)}</td><td class="num">${signed(f.cash_delta)}</td><td class="num">${usd(Number(f.friction_usd)+Number(f.gas_usd))}</td><td class="num ${tone(f.pnl)}">${f.action==='OPEN'?'—':signed(f.pnl)}</td><td>${esc(pretty(f.reason))}</td></tr>`).join(''),'Only this strategy account is shown. Isolated acceptance fills are excluded. These are automatic paper ledger executions, not signed blockchain swaps.'):`<div class="empty">${query?'No fills match this filter.':'No strategy fills in this account. Isolated plumbing tests do not appear here.'}</div>`);
  }else if(tab==='decisions'){
    const decisions=state.decisions.filter(matches);
    html('activity',decisions.length?table(['Coin','Time · Berlin','Decision','Reason','Observed buyers','Pool liquidity','Policy / stage'],decisions.map(d=>`<tr><td>${coin(d.symbol,d.asset_id)}</td><td>${stamp(d.observed_at)}</td><td class="${['BUY','PAPER_BUY'].includes(d.verdict)?'positive':'warning'}">${esc(d.verdict)}</td><td>${esc(pretty(d.reason))}</td><td class="num">${d.buyers??'—'}</td><td class="num">${usd(d.liquidity_usd)}</td><td class="muted">${esc(d.stage||d.policy_version||'—')}</td></tr>`).join(''),'Latest 80 decisions. Buyer count reflects observed recipients; distinct addresses do not establish independent controllers or profitable wallets.'):'<div class="empty">No decisions match this view.</div>');
  }else if(tab==='wallets'){
    renderWallets();
  }else{
    const jobs=state.jobs.items.filter(j=>j.name==='RUG DESK Console'||(bot==='robinhood'?j.name.startsWith('SNIPER'):j.name.startsWith('RUG DESK')));
    const jobsHtml=state.jobs.available?jobs.map(j=>`<div class="job"><span>${esc(j.name)}<small>Last trigger ${esc(new Date(j.last_run).toLocaleTimeString('en-GB',{timeZone:'Europe/Berlin'}))}</small></span><span class="${j.state==='Running'?'muted':j.last_result===0?'positive':'warning'}">${esc(j.state)}<small>Result ${j.last_result===0?'0 · success':j.last_result===267009?'running':esc(j.last_result)}</small></span></div>`).join(''):'<p class="warning compact">Windows job status unavailable. Application completion evidence remains below.</p>';
    const cycle=state.status.finished_at&&state.status.started_at?state.status.finished_at-state.status.started_at:null;
    html('activity',`<div class="health-grid"><section><h3>Windows runner</h3>${jobsHtml}<p class="compact muted">Triggers may be skipped while a previous cycle runs. Application finish times show actual completion.</p></section><section><h3>Data & delivery</h3>${key('Last application completion',stamp(state.status.finished_at,true))}${cycle!==null?key('Last cycle duration',cycle+' seconds'):''}${bot==='robinhood'&&state.executor?key('Executor mode',state.executor.mode+' · '+(state.executor.health||'observation only'),state.executor.health==='ERROR'?'warning':'')+key('Executor completion',stamp(state.executor.finished_at,true)):''}${key('Trade / setup queue',ack(state.notifications.trades))}${key('Latest acknowledged message',state.notifications.trades?.last_ack?'#'+state.notifications.trades.last_ack.message_id:'—')}${state.notifications.market?key('Market queue',ack(state.notifications.market)):''}${state.collector?key('Collector lag',finite(state.collector.collector_lag_blocks)?number(state.collector.collector_lag_blocks,0)+' blocks':'Unknown'):''}${key('Monitoring gaps',String(state.gaps.length),state.gaps.length?'warning':'')}${state.research?key('Wallets / qualified',`${state.research.candidate_count??'—'} / ${state.research.approved_wallet_count??'—'}`):''}<p class="compact muted">Telegram acknowledgements mean the Telegram API accepted the message; they are not read receipts. ${state.gaps.length?'Historical gaps remain in the ledger; conservative equity dips during missing marks are not realized sales.':''}</p><p class="compact warning">${bot==='robinhood'&&state.executor?.mode==='SHADOW'?'Automatic executor installed · SHADOW. '+esc(state.executor.observed_candidates??0)+' candidate inputs observed; zero signing or broadcast authority. Initial wallet, capital, simulation and independent deployment review remain required.':'Automatic live execution is not enabled for this account.'} This console cannot enable real money.</p></section></div>${state.policy_revisions?.length?'<div class="activity-note">Policy history: '+state.policy_revisions.map(r=>stamp(r.at,true)+' · '+esc(r.before)+' → '+esc(r.after)).join('; ')+'. Earlier entries retain their original policy. Trial deadline and account are unchanged.</div>':''}${state.gaps.length?table(['Recorded gap · Berlin','Resumed · Berlin','Duration'],state.gaps.map(g=>`<tr><td>${stamp(g.from,true)}</td><td>${stamp(g.to,true)}</td><td>${number(g.seconds/60,1)} min</td></tr>`).join('')):''}`);
  }
}
function renderWallets(){
  if(bot!=='robinhood'){html('activity','<div class="empty">Wallet discovery belongs to the Robinhood wallet follower. Select that bot to inspect its observed recipients and research evidence. The Solana account remains separate.</div>');return;}
  if(!wallets){html('activity',`<div class="empty">${walletError?'Wallet evidence could not be read. Retrying every 5 seconds.':'Reading the saved wallet evidence index…'}</div>`);return;}
  if(!wallets.available){html('activity','<div class="empty">No wallet evidence export is available yet. The research worker must publish its first index; this is not evidence of zero candidates. Retrying every 5 seconds.</div>');return;}
  const stale=!wallets.generated_at||Date.now()/1000-wallets.generated_at>3600;
  const roleNames={funded_recipient:'Recipient',transaction_sender:'Tx sender',base_payer:'Payer'};
  const roles=w=>Object.entries(w.roles||{}).filter(([,v])=>v>0).map(([k,v])=>`${roleNames[k]||pretty(k)} ${v}`).join(' · ')||'Legacy reference only';
  const filtered=wallets.addresses.filter(w=>!query||(query==='qualified'?w.qualified===true:query==='unqualified'?w.qualified!==true:[w.wallet,roles(w),w.research_candidate?'candidate':'evidence only',w.history_complete?'history complete':'history incomplete'].join(' ').toLowerCase().includes(query))).sort((a,b)=>Number(b.research_candidate)-Number(a.research_candidate)||(b.last_observed_at||0)-(a.last_observed_at||0)||a.wallet.localeCompare(b.wallet));
  const shown=filtered.slice(0,200);
  const summary=`<div class="wallet-summary"><div><strong>${number(wallets.candidate_count,0)} research candidates</strong><span>${number(wallets.address_count,0)} indexed addresses · ${number(wallets.qualified_count,0)} qualified at export</span></div><a href="/api/wallet-export.csv" download="wallet-evidence.csv">Download evidence CSV</a></div><div class="activity-note ${walletError||stale?'warning':''}">${walletError?'Index refresh failed. Last saved values retained. ':''}Export ${stamp(wallets.generated_at,true)} · ${age(wallets.generated_at)} old${stale?' · over one hour old':''}. ${number(wallets.source_checked_observations,0)} source-checked observations · ${number(wallets.capture_only_observations,0)} checked at capture only · ${number(wallets.invalidated_observations,0)} invalidated.</div>`;
  const body=shown.map(w=>`<tr class="${selectedWallet===w.wallet?'chosen':''}"><td><button class="wallet-link num" data-wallet="${esc(w.wallet)}" title="Inspect ${esc(w.wallet)}" aria-expanded="${selectedWallet===w.wallet}">${esc(short(w.wallet))}</button><span class="sub">${w.research_candidate?'Research candidate':'Evidence-only address'}</span></td><td>${esc(roles(w))}</td><td class="num">${number(w.current_source_checked_observations,0)}<span class="sub">${number(w.capture_only_observations,0)} capture only${w.invalidated_observations?' · '+number(w.invalidated_observations,0)+' invalidated':''}</span></td><td class="${w.history_complete?'':'muted'}">${w.history_complete?'Complete':'Incomplete / pending'}<span class="sub">${w.performance_qualified?'Performance threshold met':'Performance unestablished'}</span></td><td class="${w.qualified?'positive':'warning'}">${w.qualified?'Qualified at export':'Unqualified'}</td><td>${stamp(w.last_observed_at,true)}<span class="sub">${w.last_observed_at?age(w.last_observed_at)+' ago':'No indexed observations'}</span></td></tr>`).join('');
  html('activity',summary+walletDetailHTML()+(body?table(['Public address','Observed roles','Source checked','Trading history','Qualification','Last observed · Berlin'],body,`Showing ${shown.length} of ${filtered.length} matching addresses${filtered.length>200?' · narrow the filter to find more':''}. ${wallets.address_count>wallets.displayed_address_count?'The console index is capped at '+number(wallets.row_limit,0)+' addresses; CSV contains the exported evidence. ':''}Roles do not establish ownership, profitable skill or independent control. Source-checked means the last reconciliation with the local canonical journal; capture-only evidence is not a current canonical check.`):'<div class="empty">No addresses match this filter.</div>'));
}
function walletDetailHTML(){
  if(!selectedWallet)return '';
  const head=`<div class="wallet-detail-heading"><h3>Address evidence</h3><button data-wallet-close aria-label="Close address evidence">Close</button></div><div class="wallet-address num">${esc(selectedWallet)}</div>`;
  if(!walletDetail)return `<section class="wallet-detail">${head}<p class="compact ${walletDetailError?'warning':'muted'}">${walletDetailError?'Address evidence could not be read. It will retry with the next index refresh.':'Reading address observations…'}</p></section>`;
  const d=walletDetail,h=d.history||{},observations=d.observations||[];
  const facts=key('History assessment',esc(pretty(h.assessment)))+key('History complete',h.history_complete===true?'Yes':'No')+key('Performance after follower stress',h.history_complete&&h.performance_estimate_available?signed(h.net_pnl_after_follower_stress_usd):(h.history_complete?'Unavailable · unverified performance or costs':'Unavailable · incomplete history'))+key('Recorded historical gas',h.history_complete?usd(h.gas_usd):'Unknown')+key('Closed historical positions',h.history_complete?number(h.closed_positions,0):'Unknown')+key('Controller status',esc(pretty(h.controller_status)))+key('Controller review current at export',h.controller_review_current===true?'Yes':'No')+key('Qualification at export',h.qualified===true?'Qualified':'Unqualified',h.qualified?'positive':'warning');
  const rows=observations.map(o=>{
    const checked=o.canonical_checked_at,old=!checked||Date.now()/1000-checked>3600;
    const scope=o.canonical===false?'Invalidated':o.canonical===true&&o.canonical_scope==='source_journal_current'?'Local journal check':o.canonical===true&&o.canonical_scope==='rpc_verified_at_capture_only'?'RPC at capture':'Unknown';
    const spendBasis=o.source_spend_basis==='verified direct wallet expenditure'?'Verified direct wallet spend':o.source_spend_basis==='pool base input, not proven personal wallet expenditure'?'Pool input · ownership unproven':'Spend basis unestablished';
    return `<tr><td><span title="${esc(o.tx_hash)}" class="num">${esc(short(o.tx_hash))}</span><span class="sub">log ${esc(o.log_index)}</span></td><td>${esc(pretty(o.role))}</td><td>${stamp(o.observed_at,true)}</td><td class="num">${number(o.block_number,0)}</td><td class="${o.canonical===false?'warning':''}">${scope}<span class="sub ${old?'warning':''}">${checked?'As of '+stamp(checked,true)+' · '+age(checked)+' old':'Check time unknown'}</span></td><td class="num">${finite(o.spent_usd)?usd(o.spent_usd):'Unknown'}<span class="sub">${spendBasis}</span></td><td title="${esc(o.pool)}" class="num">${esc(short(o.pool))}</td><td>${o.raw_evidence_available===true?'Indexed':'Unavailable'}</td></tr>`;
  }).join('');
  return `<section class="wallet-detail">${head}<div class="wallet-detail-facts">${facts}</div><p class="compact warning">${(h.qualification_blockers||[]).map(v=>esc(pretty(v))).join(' · ')||'No saved qualification blockers.'}</p><p class="compact muted">Assessment exported ${stamp(d.generated_at,true)}. Export time is not chain verification time: local journal checks reflect the collector's last reconciliation, which may lag; capture checks are historical. Check times over one hour old are highlighted. Pool input does not prove this address paid. Discovery evidence is not entry permission or a current safety check. ${number(d.shown_observations,0)} of ${number(d.evidence_count,0)} observations shown, newest first; detail is capped at ${number(d.observation_limit,0)}. <a href="/api/wallet-evidence?wallet=${encodeURIComponent(selectedWallet)}&amp;download=1" download>Download displayed address JSON</a></p>${rows?table(['Transaction','Role','Observed · Berlin','Block','Canonical scope · Berlin','Observed spend','Pool','Raw proof'],rows):'<p class="compact muted">Legacy reference only. No transaction proof is indexed for this address yet.</p>'}</section>`;
}
function renderEvidence(){
  const a=state.assets.find(a=>a.asset_id===selectedAsset),p=state.snapshot.positions?.find(p=>p.asset_id===selectedAsset),screen=state.screens[selectedAsset];
  $('evidence-state').textContent=screen?'RECORDED SCREEN':'NO CURRENT SCREEN';
  if(!a){html('evidence',`<p class="compact muted">${state.setup_blocked?'The Solana desk has no active strategy holdings. Rugcheck rejection reasons appear alongside.':'Choose a traded coin to inspect its pool and recorded safety evidence.'}</p>`);return;}
  const checks=screen?.checks||{};
  html('evidence',`<div class="evidence-address">Token ${esc(a.asset_id.split(':').at(-1))}<br>Pinned pool ${esc(a.pool_id||'Unavailable')}</div><div class="checks">${Object.entries(checks).map(([k,v])=>`<span class="check ${v===true?'positive':v===false?'negative':'warning'}">${esc(pretty(k))}: ${v===true?'observed pass':v===false?'failed':'unknown'}</span>`).join('')||'<span class="check warning">Safety evidence unavailable</span>'}</div><p class="compact muted">${screen?'Screen recorded '+stamp(screen.observed_at,true)+' · '+age(screen.observed_at)+' ago. ':''}Historical checks are not a current safety guarantee. Entries require a fresh provider response and size-bounded route.</p>${p?key('Current observed liquidity',usd(p.mark_liquidity))+key('Stop / take-profit thresholds',`${price(p.stop_price)} / ${price(p.take_price)}`)+key('Trailing status',p.tp_done?price(p.trailing_price):'Arms after first take profit')+key('Time exit eligibility',stamp(p.opened_at+Number(state.policy.time_stop_seconds),true)+' if gain < '+state.policy.time_stop_min_gain_pct+'%'): '<p class="compact muted">No open position in this coin.</p>'}`);
}
function renderReasons(){html('rejections',state.reasons.length?state.reasons.slice(0,7).map(r=>`<div class="reason-row"><span>${esc(pretty(r.reason))}<span class="muted"> · ${esc(r.verdict)}</span></span><strong>${r.count}</strong></div>`).join(''):'<p class="compact muted">No decisions recorded yet.</p>');}

function canvasContext(canvas){const rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);return {ctx,w:rect.width,h:rect.height};}
function clearCanvas(canvas){if(canvas.id==='price-chart'){plot=null;$('chart-tip').hidden=true;}return canvasContext(canvas);}
function sampled(points){const buckets=new Map();for(const p of points)buckets.set(Math.floor(p.at/300),{at:p.at,price:Number(p.price)});return [...buckets.values()];}
function technical(points){
  // Indicators reset after a >15-minute observation gap; never bridge an outage.
  let a=null,b=null,avgGain=null,avgLoss=null,previous=null,count=0,gains=0,losses=0;
  return sampled(points).map(p=>{
    if(previous&&p.at-previous.at>900){a=b=avgGain=avgLoss=null;count=gains=losses=0;previous=null;}
    a=a===null?p.price:p.price*2/10+a*8/10;b=b===null?p.price:p.price*2/22+b*20/22;
    if(previous){const delta=p.price-previous.price,g=Math.max(0,delta),l=Math.max(0,-delta);count++;
      if(count<=14){gains+=g;losses+=l;if(count===14){avgGain=gains/14;avgLoss=losses/14;}}
      else{avgGain=(avgGain*13+g)/14;avgLoss=(avgLoss*13+l)/14;}}
    const rsi=avgGain===null?null:avgGain===0&&avgLoss===0?50:avgLoss===0?100:100-100/(1+avgGain/avgLoss);
    previous=p;return {...p,ema9:count>=8?a:null,ema21:count>=20?b:null,rsi};
  });
}
function drawChart(){
  const {ctx,w,h}=clearCanvas($('price-chart'));plot=null;
  if(!chart?.points.length){$('chart-empty').hidden=false;$('chart-empty').textContent=state?.setup_blocked?'No market positions in this account. Screening continues; the paid X gate is disabled.':'No recorded prices for this selected pool yet.';$('quote-price').textContent='—';$('quote-age').textContent='No quote';$('quote-change').textContent='No recorded market series';html('ta','<span>RSI 14 <b>—</b></span><span>EMA 9 <b>—</b></span><span>EMA 21 <b>—</b></span><span>Samples <b>0</b></span>');return;}
  $('chart-empty').hidden=true;
  const all=chart.points.map(p=>({...p,price:Number(p.price)}));const end=Math.max(all.at(-1).at,...chart.fills.map(f=>f.observed_at)),start=range?end-range:Math.min(all[0].at,...chart.fills.map(f=>f.observed_at));
  const visibleFills=chart.fills.filter(f=>f.observed_at>=start&&f.observed_at<=end);
  const points=all.filter(p=>p.at>=start),ta=technical(all),lastTA=ta.at(-1),visibleTA=ta.filter(p=>p.at>=start);
  const last=all.at(-1),first=points[0],change=first.price?(last.price/first.price-1)*100:null;
  $('quote-price').textContent=price(last.price);$('quote-change').textContent=pct(change)+' in view';$('quote-change').className=tone(change);$('chart-source').textContent='Pinned pool · observed prices';
  html('ta',`<span>RSI 14 <b>${number(lastTA?.rsi,1)}</b></span><span>EMA 9 <b>${price(lastTA?.ema9)}</b></span><span>EMA 21 <b>${price(lastTA?.ema21)}</b></span><span>Samples <b>${points.length}</b></span>`);
  const left=8,right=89,top=17,bottom=27,cw=w-left-right,ch=h-top-bottom;
  if(cw<10||ch<10)return;
  const pos=state.snapshot.positions?.find(p=>p.asset_id===selectedAsset);
  const levels=$('levels-toggle').checked&&pos?[['Stop',Number(pos.stop_price),'#f39796'],['Take profit',Number(pos.take_price),'#edc38b'],...(pos.trailing_price?[['Trailing',Number(pos.trailing_price),'#a3bdf0']]:[])]:[];
  let low=Math.min(...points.map(p=>p.price),...levels.map(l=>l[1]),...visibleFills.map(f=>Number(f.fill_price))),high=Math.max(...points.map(p=>p.price),...levels.map(l=>l[1]),...visibleFills.map(f=>Number(f.fill_price)));
  const pad=Math.max((high-low)*.14,high*.002);low=Math.max(0,low-pad);high+=pad;
  const minT=Math.min(points[0].at,...visibleFills.map(f=>f.observed_at)),maxT=Math.max(end,minT+60),x=t=>left+(t-minT)/(maxT-minT)*cw,y=v=>top+(high-v)/(high-low)*ch;
  ctx.font='10px Consolas,monospace';ctx.lineWidth=1;
  for(let i=0;i<5;i++){const val=low+(high-low)*i/4,yy=y(val);ctx.strokeStyle='#293332';ctx.setLineDash([2,5]);ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(w-right,yy);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#9baaa6';ctx.fillText(price(val).replace('$',''),w-right+10,yy+3);}
  ctx.fillStyle='#9baaa6';ctx.textAlign='center';for(let i=0;i<5;i++){let t=minT+(maxT-minT)*i/4;ctx.fillText(stamp(t).slice(0,5),x(t),h-6);}ctx.textAlign='left';
  function line(series,field,color,width=1.5){ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();let previous=null;for(const p of series){const value=p[field];if(!finite(value)){previous=null;continue;}if(!previous||p.at-previous.at>900)ctx.moveTo(x(p.at),y(value));else ctx.lineTo(x(p.at),y(value));previous=p;}ctx.stroke();}
  line(points,'price','#8cdeb3',1.8);
  if(points.length===1){ctx.fillStyle='#8cdeb3';ctx.beginPath();ctx.arc(x(points[0].at),y(points[0].price),3,0,Math.PI*2);ctx.fill();}
  if($('ema-toggle').checked){line(visibleTA,'ema9','#edc38b',1);line(visibleTA,'ema21','#a3bdf0',1);}
  for(const [label,v,color] of levels){ctx.strokeStyle=color;ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(left,y(v));ctx.lineTo(w-right,y(v));ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=color;ctx.fillText(label,left+5,y(v)-5);}
  for(const f of visibleFills){ctx.fillStyle=f.action==='OPEN'?'#8cdeb3':f.action==='PARTIAL'?'#edc38b':'#f39796';ctx.beginPath();ctx.arc(x(f.observed_at),y(Number(f.fill_price)),4,0,Math.PI*2);ctx.fill();ctx.strokeStyle='#151a1b';ctx.lineWidth=2;ctx.stroke();}
  const yy=y(last.price);ctx.fillStyle='#8cdeb3';ctx.fillRect(w-right+5,yy-10,right-10,20);ctx.fillStyle='#101414';ctx.fillText(price(last.price).replace('$',''),w-right+9,yy+3);
  plot={points,fills:visibleFills,x,y,minT,maxT,left,cw};freshness();
}
function drawEquity(){
  const {ctx,w,h}=canvasContext($('equity-chart')),points=state.equity_history;if(points.length<2){ctx.fillStyle='#9baaa6';ctx.font='11px Segoe UI';ctx.fillText('No trial equity series',4,h/2);return;}
  let low=Math.min(...points.map(p=>Number(p.equity))),high=Math.max(...points.map(p=>Number(p.equity)));if(high===low)high=low+1;
  const first=points[0].observed_at,last=points.at(-1).observed_at;
  ctx.strokeStyle='#8cdeb3';ctx.lineWidth=1.5;ctx.beginPath();let previous=null;
  for(const p of points){const x=3+(p.observed_at-first)/Math.max(1,last-first)*(w-6),y=5+(high-Number(p.equity))/(high-low)*(h-10);if(!previous||p.observed_at-previous.observed_at>900)ctx.moveTo(x,y);else ctx.lineTo(x,y);previous=p;}ctx.stroke();
}

document.querySelectorAll('[data-bot]').forEach(button=>button.addEventListener('click',()=>{
  if(bot===button.dataset.bot)return;bot=button.dataset.bot;generation++;selectedAsset='';chart=null;state=null;loading=false;error=false;query='';wallets=null;walletError=false;walletLoading=false;selectedWallet='';walletDetail=null;walletDetailError=false;walletDetailLoading='';$('filter').value='';$('offline').hidden=true;
  document.querySelectorAll('[data-bot]').forEach(b=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-pressed',String(b===button));});
  $('account-title').textContent=bot==='solana'?'RUG DESK · Solana':'Wallet follower';$('chain').textContent=bot==='solana'?'Solana Mainnet':'Robinhood Chain · 4663';$('account-id').textContent='Reading selected account…';$('account-id').title='';$('strategy-note').textContent='Reading account policy…';$('worker-freshness').textContent='Waiting for worker status';$('trial-state').textContent='Reading trial status';$('trial-deadline').textContent='—';$('quote-price').textContent='—';$('quote-change').textContent='Loading selected account';$('quote-age').textContent='—';$('instrument-state').textContent='LOADING';html('asset-select','<option>Loading coins…</option>');$('asset-select').disabled=true;for(const id of ['guardrails','health-mini','evidence','rejections','ta'])html(id,'');for(const id of ['open-count','fill-count','trial-return'])$(id).textContent='—';$('trial-detail').textContent='Reading separate account';$('footer-id').textContent='Loading selected account';$('notice').textContent='Observation only · reading separate account';clearCanvas($('equity-chart'));html('metrics',metric('Account','Loading…','Reading separate persistent account'));html('activity','<div class="empty">Reading selected account…</div>');$('chart-empty').hidden=false;$('chart-empty').textContent='Loading selected account…';clearCanvas($('price-chart'));refresh();
}));
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>{tab=button.dataset.tab;document.querySelectorAll('[data-tab]').forEach(b=>b.setAttribute('aria-selected',String(b===button)));renderActivity();if(tab==='wallets')loadWallets();}));
document.querySelector('.tabs').addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;const buttons=[...document.querySelectorAll('[data-tab]')];let i=buttons.indexOf(document.activeElement);if(i<0)return;e.preventDefault();i=e.key==='Home'?0:e.key==='End'?buttons.length-1:(i+(e.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;buttons[i].focus();buttons[i].click();});
document.querySelectorAll('[data-range]').forEach(button=>button.addEventListener('click',()=>{range=Number(button.dataset.range);document.querySelectorAll('[data-range]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));});drawChart();}));
$('filter').addEventListener('input',()=>{query=$('filter').value.toLowerCase().trim();renderActivity();});
$('asset-select').addEventListener('change',()=>{selectedAsset=$('asset-select').value;chart=null;renderEvidence();renderActivity();loadChart();});
$('activity').addEventListener('click',e=>{const wallet=e.target.closest('[data-wallet]');if(wallet){loadWalletDetail(wallet.dataset.wallet);return;}if(e.target.closest('[data-wallet-close]')){selectedWallet='';walletDetail=null;renderActivity();return;}const button=e.target.closest('[data-asset]');if(button&&state.assets.some(a=>a.asset_id===button.dataset.asset)){selectedAsset=button.dataset.asset;chart=null;$('asset-select').value=selectedAsset;renderEvidence();renderActivity();loadChart();}});
for(const id of ['ema-toggle','levels-toggle'])$(id).addEventListener('change',drawChart);
$('price-chart').addEventListener('pointermove',e=>{if(!plot)return;const rect=e.currentTarget.getBoundingClientRect(),xx=e.clientX-rect.left,t=plot.minT+(xx-plot.left)/plot.cw*(plot.maxT-plot.minT),p=plot.points.reduce((a,b)=>Math.abs(a.at-t)<Math.abs(b.at-t)?a:b);const f=plot.fills.find(f=>Math.abs(plot.x(f.observed_at)-xx)<7);$('chart-tip').textContent=f?stamp(f.observed_at,true)+' CET\nPAPER '+f.action+' · '+price(f.fill_price)+'\nQuantity '+number(f.quantity,3):stamp(p.at,true)+' CET\n'+price(p.price)+' · observed';$('chart-tip').hidden=false;});
$('price-chart').addEventListener('pointerleave',()=>{$('chart-tip').hidden=true;});
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(state){drawChart();drawEquity();}},100);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{if(!document.hidden)refresh();},5000);setInterval(freshness,1000);refresh();
