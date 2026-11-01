'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);};
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const context={window:{},$:node,esc,renderBotIdentity(){},short:v=>v,pretty:v=>v,price:v=>'$'+v,
  number:v=>v,usd:v=>'$'+v,signed:v=>v,tone:()=>'',metric:(k,v)=>esc(k)+esc(v),
  key:(k,v)=>esc(k)+esc(v),table:(headers,rows)=>'<table>'+rows+'</table>',html:(id,s)=>{node(id).innerHTML=s;}};
vm.createContext(context);vm.runInContext(fs.readFileSync('console/devwatch.js','utf8'),context);
context.window.DevWatchView.render({initialized:false,version:'test'});
assert.match(node('devwatch-panel').innerHTML,/Not installed/);
const sample={initialized:true,version:'test',mode:'MANUAL LIVE',
  summary:{realized_pnl:0,api_spend:1,x_posts:87,checked:5,buy_verdicts:1},
  policy:{TICKET_USD:25,MAX_OPEN:4,DAILY_LOSS_HALT_USD:50,ENTRY:{min_liquidity_usd:30000,max_dev_pct:5,max_top10_pct:30},EXIT:{stop_pct:-30,take_pct:80}},
  positions:[{symbol:'<img onerror=alert(1)>',mint:'mint',qty:10,entry:1,pulse:'fading',pending_exit:{reason:'x_fading',id:'pending-id',qty:5}}],
  pending_buys:[],checks:[],trades:[],status:{sources_verified:true},signal_readiness:{bots:[{name:'Test',state:'RESTORE_REQUIRED',signal_scope:'Grok',blockers:['backup missing']}]}};
context.window.DevWatchView.render(sample);
const rendered=node('devwatch-panel').innerHTML;
assert.match(rendered,/&lt;img/);assert.doesNotMatch(rendered,/<img/);
assert.match(rendered,/pending-id/);assert.match(rendered,/Awaiting actual fill confirmation/);
assert.match(rendered,/No fills recorded/);assert.match(rendered,/backup missing/);
assert.doesNotMatch(rendered,/<button|onclick|fetch\(/);
console.log('DEV WATCH UI: empty state, manual confirmations, escaping and backup readiness PASS');

context.window.DevWatchView.render({...sample,mode:'PAPER',positions:[]});
assert.equal(node('mode-badge').textContent,'PAPER');
assert.match(node('notice').textContent,/simulated entries and exits/);
assert.match(node('devwatch-panel').innerHTML,/No simulated fills recorded/);
console.log('Oxygen UI: paper mode and ninth-desk language PASS');
