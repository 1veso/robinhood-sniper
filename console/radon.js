'use strict';
window.RadonView={
  freshness(s){
    const completed=kind=>s.runs?.find(r=>r.kind===kind&&r.state==='COMPLETE')?.finished_at;
    const source=s.status?.source||{},now=Date.now()/1000;
    const fresh=source.state==='OK'&&source.through&&now-source.through>=0&&now-source.through<=(s.config?.source_max_age_seconds||30);
    $('worker-freshness').textContent=`Source ${fresh?'current':'unavailable / stale'} · entries ${age(completed('entries'))} ago · exits ${age(completed('watch'))} ago`;
    $('worker-freshness').className='context-status'+(!fresh||s.error_count?' warning':'');
  },
  render(s){
    renderBotIdentity();
    $('version').textContent='v'+s.version+' · LOCAL / READ ONLY';
    $('account-id').textContent=s.account_id?'Independent paper account · '+short(s.account_id):'Not initialized';
    $('account-id').title=s.account_id||'';
    $('trial-state').textContent=s.demo?'SYNTHETIC DEMO':'PROSPECTIVE PAPER';
    $('trial-deadline').textContent='Slot 8 · slot 7 reserved';
    $('strategy-note').textContent='Copier replay → frozen shortlist → checks + X evidence → fixed-ticket paper fills';
    $('notice').textContent='Paper only. Historical replay is conditional research; forward fills are reported separately. No executable sell route is claimed.';
    if(!s.initialized){html('metrics','');html('radon-panel','<div class="panel empty">Radon has not been initialized. This view cannot create accounts, authorize paid research, or start trading.</div>');return;}
    const m=s.summary,sc=s.shortlist,rank=s.research?.ranking||[];
    const when=t=>t?new Date(t*1000).toISOString().replace('T',' ').replace('.000Z',' UTC'):'—';
    const section=(title,body)=>`<section class="panel radon-section"><div class="panel-heading"><h2>${esc(title)}</h2></div>${body}</section>`;
    html('metrics',metric('Paper cash',usd(m.cash),'After recorded model costs and reserves')+metric('Paper equity',usd(m.equity),m.unpriced_positions.length?'Unknown marks — see positions':'Cost-aware open marks + reserved capital')+metric('Realized P&L',signed(m.realized_pnl),'Recorded sales after trading costs')+metric('Net account return',m.net_account_return===null?'Unknown':number(Number(m.net_account_return)*100)+'%','Relative to starting paper capital')+metric('Buying turnover',usd(m.buying_turnover),'Not starting capital'));
    const current=sc?`<p class="radon-note">Effective ${esc(when(sc.effective_at))} · expires ${esc(when(sc.expires_at))}. Only subsequent signals qualify.</p><p class="radon-note">${sc.payload.leaders.map(l=>esc(short(l))).join(' · ')||'No eligible leaders'} · Joined ${sc.payload.joined.length} / dropped ${sc.payload.dropped.length}. Dropped leaders retain exit monitoring.</p>`:'<p class="empty">No frozen shortlist yet. Source availability and complete history are required.</p>';
    const ranking=table(['Leader','Conditional copy multiple','Buy fills / history buys','Median hold','Largest winning coin','Coverage'],rank.map(r=>`<tr><td title="${esc(r.leader)}">${esc(short(r.leader))}${r.eligible?' · eligible':''}</td><td>${r.replay?.copy_multiple?number(r.replay.copy_multiple)+'×':'Unknown'}</td><td>${r.replay?.buy_fills??'—'} / ${r.history_buys??'—'}</td><td>${r.median_hold_seconds??'—'}s</td><td>${r.replay?number(Number(r.replay.largest_winner_share)*100)+'%':'—'}${r.replay?.one_coin_flag?' · concentrated':''}</td><td>${esc(r.incomplete_reason||r.kol_identity||'Archived numerical evidence only')}</td></tr>`).join('')||'<tr><td colspan="6">Historical research unavailable.</td></tr>');
    const combined=s.research?.combined_portfolio;
    const historical=`${ranking}<p class="radon-note">Combined chronological replay: ${combined?`${usd(combined.summary.starting_cash)} starting cash → ${usd(combined.summary.equity)} modeled equity; ${usd(combined.summary.buying_turnover)} turnover.`:'Unavailable.'} Historical X and safety decisions are not reconstructed from current data. In-sample selection is not forward performance.</p>`;
    const positions=table(['Mint / originating leader','Quantity','Remaining basis','Episode cap left','Attributed buys','Open value'],s.positions.map(p=>`<tr><td title="${esc(p.mint)}">${esc(short(p.mint))}<small class="sub">${esc(short(p.leader))}</small></td><td>${number(p.quantity,6)}</td><td>${usd(p.basis)}</td><td>${usd(p.cap_remaining)}<small class="sub">Episode ${p.episode} · shared across leaders</small></td><td>${p.buy_count}</td><td>${usd(p.liquidation_value)}${p.liquidation_value===null?' · unpriced/stale':''}</td></tr>`).join('')||'<tr><td colspan="6">No copied positions.</td></tr>');
    const accounting=`<div class="krypton-rules">${key('Starting paper capital',usd(m.starting_cash))}${key('Peak deployed capital',usd(m.peak_deployed))}${key('Unrealized P&L',signed(m.unrealized_pnl))}${key('Network costs',usd(m.network_costs))}${key('Embedded modeled friction',usd(m.embedded_friction))}${key('Rent reserved',usd(m.rent_reserved))}${key('Model billed / unsettled',usd(m.model_billed)+' / '+usd(m.model_unsettled))}${key('Pending exits',s.pending_exits)}</div>`;
    const decisions=table(['Time UTC','Decision','Reason'],s.decisions.map(d=>`<tr><td>${esc(when(d.at))}</td><td>${esc(d.action)}</td><td>${esc(d.reason)}<details id="radon-d-${d.id}"><summary>Evidence and timing</summary><pre>${esc(JSON.stringify(d.payload,null,2))}</pre></details></td></tr>`).join('')||'<tr><td colspan="3">No decisions recorded.</td></tr>');
    const workers=table(['Worker','Started UTC','Completed UTC','Outcome'],s.runs.map(r=>`<tr><td>${esc(r.kind)}</td><td>${esc(when(r.started_at))}</td><td>${esc(when(r.finished_at))}</td><td>${esc(r.state)} ${esc((r.payload.errors||[]).join('; '))}</td></tr>`).join(''));
    const c=s.status.chain_collector;
    const collection=c?`<p class="radon-note">Independent chain collector: ${esc(c.state)} · updated ${esc(when(c.observed_at))} · ${number(c.candidate_count)} research candidates · ${number(c.signature_count)} signatures archived.</p><p class="radon-note">${esc(c.error||'Raw history is still being reconciled. No qualified trading feed or entry approval is implied.')}</p>`:'';
    const operations=`${collection}<p class="radon-note">Trading feed: ${esc(s.status.source.state)} · ${esc(s.status.source.coverage||s.status.source.error||'Complete event coverage unavailable')}. Model: ${s.status.model_gate.enabled?'last check authorized':'unavailable / disabled'}.</p>${workers}`;
    const opened=[...document.querySelectorAll('#radon-panel details[open]')].map(e=>e.id);
    html('radon-panel',section('Current copier shortlist',current)+section('Historical conditional research',historical)+section('Prospective paper account',accounting)+section('Copied positions · leader attribution',positions)+section('Decision stream',decisions)+section('Operational status',operations)+section('Evidence limits',`<ul>${s.limits.map(l=>'<li>'+esc(l)+'</li>').join('')}</ul>`));
    for(const id of opened){const el=document.getElementById(id);if(el)el.open=true;}
    $('footer-id').textContent='Radon · slot 8 · '+s.account_id+' · read-only paper desk';
    this.freshness(s);
  }
};
