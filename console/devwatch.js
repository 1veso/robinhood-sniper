'use strict';
window.DevWatchView={render(s){
  renderBotIdentity();
  $('version').textContent='v'+s.version+' · LOCAL / READ ONLY';
  $('account-id').textContent='Separate article ledger';$('account-id').title='DEV WATCH';
  $('trial-state').textContent=s.mode||'NOT INSTALLED';
  $('mode-badge').textContent=s.mode==='PAPER'?'PAPER':'MANUAL SIGNALS';
  $('trial-deadline').textContent='Wallet trades require your action';
  $('strategy-note').textContent='Grok / X leads → article checks → wallet snapshots → confirmed exits';
  $('notice').textContent='Signals are not orders. Execute in your own wallet and confirm actual fills. Pending exits leave holdings and P&L unchanged.';
  $('footer-id').textContent='DEV WATCH · isolated ledger · manual execution';
  const section=(title,content)=>`<section class="panel krypton-section"><div class="panel-heading"><h2>${esc(title)}</h2></div>${content}</section>`;
  if(!s.initialized){html('metrics','');html('devwatch-panel',section('DEV WATCH','Not installed in this data root. No account was created.'));return;}
  const m=s.summary,p=s.policy;
  html('metrics',metric('Open holdings',s.positions.length,'Recorded quantities; no wallet balance assumed')+metric('Pending approvals',s.pending_buys.length,'BUY proposals are not fills')+metric('Recorded realized P&L',signed(m.realized_pnl),'From ledger fills; not independently verified',tone(m.realized_pnl))+metric('Grok / X spend',usd(m.api_spend),number(m.x_posts,0)+' posts · excludes setup proof')+metric('Candidates checked',number(m.checked,0),m.buy_verdicts+' BUY verdicts · all recorded history'));
  const holdings=table(['Token / mint','Quantity / entry','X pulse','Pending exit'],s.positions.map(x=>`<tr><td>${esc(x.symbol)}<small class="sub">${esc(x.mint)}</small></td><td>${number(x.qty,6)} / ${price(x.entry)}</td><td>${esc(x.pulse)}</td><td>${x.pending_exit?`${esc(pretty(x.pending_exit.reason))} · ${esc(x.pending_exit.qty)} tokens<small class="sub">Confirmation ID: ${esc(x.pending_exit.id)}</small><span class="warning">Awaiting actual fill confirmation</span>`:'No exit requested'}</td></tr>`).join('')||'<tr><td colspan="4">No open holdings. Qualifying buys require your manual execution and fill details.</td></tr>');
  const proposals=table(['Token','Mint','Action'],s.pending_buys.map(x=>`<tr><td>${esc(x.symbol)}</td><td>${esc(x.mint)}</td><td>Awaiting manual buy and actual fill details</td></tr>`).join('')||'<tr><td colspan="3">No pending buy approvals.</td></tr>');
  const checks=table(['Time · Berlin','Token','Verdict','Reason'],s.checks.map(x=>`<tr><td>${esc(x.time)}</td><td title="${esc(x.mint)}">${esc(x.symbol||short(x.mint))}</td><td>${esc(x.verdict)}</td><td>${esc(x.reason)}</td></tr>`).join('')||'<tr><td colspan="4">No checks recorded.</td></tr>');
  const trades=table(['Time · Berlin','Token','Action','Proceeds / notional','Realized P&L','Reason'],s.trades.map(x=>`<tr><td>${esc(x.time)}</td><td>${esc(x.symbol)}</td><td>${esc(x.action)}</td><td>${usd(x.usd)}</td><td>${signed(x.pnl)}</td><td>${esc(x.reason)}</td></tr>`).join('')||'<tr><td colspan="6">No fills recorded. Alerts do not create trade records.</td></tr>');
  const rules=`<div class="krypton-rules">${key('Ticket / max open',usd(p.TICKET_USD)+' / '+p.MAX_OPEN)}${key('Liquidity floor',usd(p.ENTRY.min_liquidity_usd))}${key('Dev / top ten maximum',p.ENTRY.max_dev_pct+'% / '+p.ENTRY.max_top10_pct+'%')}${key('Stop / take profit',p.EXIT.stop_pct+'% / +'+p.EXIT.take_pct+'%')}${key('Daily loss halt',usd(p.DAILY_LOSS_HALT_USD))}${key('Approved sources',s.status.sources_verified?'Verified':'CHANGED',s.status.sources_verified?'positive':'warning')}</div>`;
  const readiness=table(['Bot','State','Signal path / remaining work'],s.signal_readiness.bots.map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(pretty(x.state))}</td><td>${esc(x.signal_scope)}${x.blockers.length?'<small class="sub">'+x.blockers.map(esc).join(' · ')+'</small>':''}</td></tr>`).join(''));
  html('devwatch-panel',section('Open holdings and pending exits',holdings)+section('Pending buy approvals',proposals)+section('Article rules',rules)+section('Latest 100 checks',checks)+section('Latest 100 ledger fills',trades)+section('Seven-bot signal readiness',readiness)+section('Observation scope','No synthetic bankroll, equity, or current price is inferred. Historical ledger rows may include paper activity. Jobs run through the existing Codex routines; this console does not start duplicate workers or confirm fills.'));
}};
