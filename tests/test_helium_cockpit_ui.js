/* Node-only behavior checks for the Helium renderer; this is not a browser layout test. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../console/helium.js'),'utf8');
const ids=new Map();
function decode(v){return v.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');}
class Element {
  constructor(tag='div',attrs={}){this.tagName=tag.toUpperCase();this.attrs={};this.dataset={};this.children=[];this.listeners={};this._html='';this._text='';this.value='';this.scrollTop=0;this.open=false;this.hidden=false;this.classes=new Set();this.classList={add:(...v)=>v.forEach(x=>this.classes.add(x)),remove:(...v)=>v.forEach(x=>this.classes.delete(x)),contains:x=>this.classes.has(x),toggle:(x,v)=>{const next=v===undefined?!this.classes.has(x):v;next?this.classes.add(x):this.classes.delete(x);return next;}};Object.entries(attrs).forEach(([k,v])=>this.setAttribute(k,v));}
  setAttribute(k,v){this.attrs[k]=String(v);if(k==='id'){this.id=String(v);ids.set(this.id,this);}if(k==='class'){this.className=String(v);String(v).split(/\s+/).forEach(x=>this.classes.add(x));}if(k==='value')this.value=String(v);if(k==='hidden')this.hidden=true;if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(v);}
  hasAttribute(k){return Object.hasOwn(this.attrs,k);}
  getAttribute(k){return this.attrs[k]??null;}
  get isConnected(){return !!this.parentElement||this.id==='helium-panel';}
  set textContent(v){this._text=String(v);this._html=String(v);}
  get textContent(){return this._text;}
  set innerHTML(v){this._html=String(v);this._text=decode(this._html.replace(/<[^>]*>/g,''));this.replaceChildren();const stack=[this];for(const token of this._html.matchAll(/<\/?[^>]+>|[^<]+/g)){const s=token[0];if(s.startsWith('</')){if(stack.length>1)stack.pop();continue;}if(!s.startsWith('<'))continue;const match=s.match(/^<([\w-]+)\b([^>]*)>/);if(!match)continue;const attrs={};for(const a of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g))attrs[a[1]]=decode(a[2]??'');const el=new Element(match[1],attrs);stack.at(-1).append(el);if(!['input','br','hr','meta','link'].includes(match[1])&&!s.endsWith('/>'))stack.push(el);}}
  get innerHTML(){return this._html;}
  append(el){el.parentElement=this;this.children.push(el);}
  replaceChildren(){const drop=e=>{for(const child of e.children)drop(child);if(e.id&&ids.get(e.id)===e)ids.delete(e.id);};for(const child of this.children)drop(child);this.children=[];}
  remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(x=>x!==this);this.parentElement=null;}
  insertAdjacentHTML(_,html){this.innerHTML=this.innerHTML+html;}
  get firstElementChild(){return this.children[0]||null;}
  addEventListener(name,fn){this.listeners[name]=fn;}
  matches(selector){if(selector.startsWith('['))return this.hasAttribute(selector.slice(1,-1));if(selector.startsWith('.'))return this.classes.has(selector.slice(1));return this.tagName===selector.toUpperCase();}
  querySelectorAll(selector){const found=[];const visit=e=>{for(const child of e.children){if(child.matches(selector))found.push(child);visit(child);}};visit(this);return found;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  contains(el){for(let p=el;p;p=p.parentElement)if(p===this)return true;return false;}
  closest(selector){for(let p=this;p;p=p.parentElement)if(p.matches(selector))return p;return null;}
  focus(){}
  showModal(){this.open=true;}
  close(){this.open=false;this.listeners.close?.();}
  getBoundingClientRect(){return {width:600,height:110};}
  getContext(){return ctx;}
}
const canvasCalls=[];
const ctx={scale(){},setLineDash(){},beginPath(){canvasCalls.push(['begin']);},moveTo(...v){canvasCalls.push(['move',...v]);},lineTo(...v){canvasCalls.push(['line',...v]);},stroke(){},arc(...v){canvasCalls.push(['point',...v]);},fill(){}};
const root=new Element('section',{id:'helium-panel'}),body=new Element('body');body.append(root);
let now=1793350800000,requests=[],responseFor=()=>({ok:false,status:503,json:async()=>({})});
class Clock extends Date {constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
const sandbox={window:{devicePixelRatio:1},document:{body,activeElement:null,getElementById:id=>ids.get(id)||null,createElement:t=>new Element(t)},Date:Clock,Number,String,Math,JSON,Map,Set,URLSearchParams,AbortSignal:{timeout:()=>undefined},setTimeout:()=>1,clearTimeout(){},ResizeObserver:class{observe(){}},fetch:async url=>{requests.push(String(url));return responseFor(String(url));}};
vm.createContext(sandbox);vm.runInContext(source,sandbox);
const ui=sandbox.window.HeliumView;
const el=id=>ids.get(id);
function click(node){root.listeners.click({target:node});}
function flush(){return new Promise(resolve=>setImmediate(resolve));}
function event(seq,values={}){return {id:'A:'+seq,seq,account_id:'A',at:Math.floor(now/1000),stage:'filter',kind:'decision',outcome:'skip',mint:'mint-one',reason:'Insufficient liquidity',...values};}
function snapshot(seq,events=[],values={}){return {bot:'helium',initialized:true,account_id:'A',demo:false,config:{version:'krypton_new_token_v1',quote_max_age_seconds:60,max_position_pct:'.02',stop_loss_pct:'.25',take_profit_pct:'.5'},summary:{equity:'1000',cash:'1000',net_pnl:'0',starting_cash:'1000',realized_pnl:'0',completed_trades:0,unknown_api_bills:0,unresolved_positions:0},positions:[],fills:[],decisions:[],equity_history:[],market:[],status:{team_gate:{blockers:[]}},helium_workspace:{cursor:'A:'+seq,events,total_events:seq,counts:{candidates:2,rejected:1,open_positions:0,closed_positions:0},data:{state:'live',observed_at:Math.floor(now/1000),max_age_seconds:60},worker:{state:'idle',last_scan_at:Math.floor(now/1000),last_watch_at:Math.floor(now/1000),blockers:[]},pipeline:[],rejections:[],execution:{mode:'paper',live_ready:false}},...values};}
function withWorkspace(s,patch){s.helium_workspace={...s.helium_workspace,...patch};return s;}
function fill(id,side='BUY',mint='mint-one'){return {id,side,mint,payload:{fill_price:'1',quantity:'2'}};}
function fillEvent(seq,id,side='BUY',values={}){return event(seq,{kind:'fill',outcome:'filled',stage:side==='BUY'?'execution':'guard',fill_id:id,side,price:'1',quantity:'2',...values});}
(async()=>{
  ui.render(snapshot(2,[event(1),fillEvent(2,1)],{fills:[fill(1)]}));
  assert.equal(el('h-toasts').children.length,0,'initial snapshot must not alert old fills');
  assert.equal(el('h-data').textContent,'LIVE DATA');
  assert.equal(body.classList.contains('helium-active'),true);
  ui.render(snapshot(2,[event(1),fillEvent(2,1)],{fills:[fill(1)]}));
  assert.equal(el('h-toasts').children.length,0,'unchanged cursor is quiet');
  ui.render(snapshot(3,[event(1),fillEvent(2,1),fillEvent(3,2)],{fills:[fill(2),fill(1)]}));
  assert.equal(el('h-toasts').children.length,1,'new committed fill produces one notification');
  ui.render(snapshot(3,[event(1),fillEvent(2,1),fillEvent(3,2)],{fills:[fill(2),fill(1)]}));
  assert.equal(el('h-toasts').children.length,1,'repeated snapshot does not duplicate fill notification');
  ui.render(snapshot(2,[event(1)],{summary:{equity:'99'}}));
  assert.match(el('h-main-value').innerHTML,/1,000/,'older snapshot cannot roll account backwards');
  ui.render(snapshot(4,[event(4,{outcome:'buy',stage:'execution'})],{fills:[fill(2),fill(1)]}));
  assert.equal(el('h-toasts').children.length,1,'BUY decision alone is not a committed fill');
  ui.render(snapshot(5,[fillEvent(5,99)],{fills:[]}));await flush();
  assert.equal(el('h-toasts').children.length,1,'unmatched fill requires authoritative journal confirmation');
  assert.match(requests.at(-1),/after=A%3A4/);
  responseFor=url=>({ok:true,json:async()=>({account_id:'A',prev_cursor:'A:4',cursor:'A:6',events:[fillEvent(5,99),event(6)],has_more:false})});
  ui.render(snapshot(6,[event(6)],{fills:[]}));await flush();
  assert.equal(el('h-toasts').children.length,2,'gap catch-up recovers omitted committed fill from durable journal');
  ui.render(snapshot(6,[event(6)]));await flush();
  assert.equal(el('h-toasts').children.length,2,'catch-up and snapshot share one processed cursor');
  responseFor=()=>({ok:true,json:async()=>({account_id:'OTHER',prev_cursor:'A:6',cursor:'OTHER:7',events:[fillEvent(7,100,{})]})});
  ui.render(snapshot(7,[fillEvent(7,100,'BUY',{account_id:'OTHER'})],{fills:[fill(100)]}));await flush();
  assert.equal(el('h-toasts').children.length,2,'mismatched event/account never creates an alert');

  const b=snapshot(1,[event(1,{id:'B:1',account_id:'B'})]);b.account_id='B';b.helium_workspace.cursor='B:1';
  ui.render(b);assert.equal(el('h-toasts').children.length,0,'account switch clears notices and seeds a fresh cursor');
  ui.setActive(false);assert.equal(body.classList.contains('helium-active'),false);assert.equal(root.hidden,true);
  ui.setActive(true);ui.render(b);assert.equal(el('h-toasts').children.length,0,'reactivation never replays old notifications');
  ui.disconnected();assert.equal(el('h-data').textContent,'DATA DISCONNECTED');assert.equal(el('h-worker').textContent,'DISCONNECTED');
  ui.render(b);now+=61000;ui.tick();assert.equal(el('h-data').textContent,'DATA DISCONNECTED','transport expires independently of market quote');
  b.helium_workspace.data.observed_at=Math.floor(now/1000)-61;ui.render(b);assert.equal(el('h-data').textContent,'STALE DATA','fresh HTTP snapshot cannot turn stale market data green');
  const unknown=snapshot(1,[event(1)],{summary:{equity:'999',cash:'979',net_pnl:null,paper_estimated_net_pnl:'-1',starting_cash:'1000',unknown_api_bills:1,unresolved_positions:1}});
  ui.render(unknown);assert.match(el('h-net').innerHTML,/estimated paper P&amp;L/);assert.match(el('h-cost-treatment').textContent,/awaits model bills/);assert.match(el('h-cost-treatment').textContent,/1 positions have unresolved/);assert.equal(el('h-main-label').textContent,'CONSERVATIVE PAPER EQUITY');
  const sourceStale=structuredClone(unknown);sourceStale.summary={...sourceStale.summary,accounting_unresolved_positions:0,source_stale_positions:1,valuation_current:false};ui.render(sourceStale);assert.match(el('h-net').innerHTML,/Current P&amp;L unavailable/);assert.doesNotMatch(el('h-net').innerHTML,/% return/);assert.match(el('h-cost-treatment').textContent,/retains the recorded accounting marks/);assert.doesNotMatch(el('h-cost-treatment').textContent,/zero value/);

  const malicious=event(1,{reason:'<img src=x onerror="alert(1)">',symbol:'<script>alert(1)</script>'});
  const unsafe=snapshot(1,[malicious],{positions:[{mint:'mint-one',symbol:'<img onerror=x>',entry_price:'1',quantity:'2',basis:'2',unrealized_pnl:'0',mark_current:true,observed_at:Math.floor(now/1000),observed_price:'1',stop_price:'.75',take_price:'1.5',rent_reserved:'0'}],market:[{mint:'mint-one',symbol:'<script>x</script>',price:'1',observed_at:Math.floor(now/1000),state:'fresh'}]});
  ui.render(unsafe);assert.match(el('h-log').innerHTML,/&lt;img/);assert.doesNotMatch(el('h-log').innerHTML,/<img/);assert.doesNotMatch(el('h-positions').innerHTML,/<img/);assert.doesNotMatch(el('h-ticker').innerHTML,/<script>/);
  el('h-outcome-filter').value='rejected';el('h-outcome-filter').listeners.change();assert.match(el('h-log').innerHTML,/Insufficient|&lt;img/,'SKIP maps to rejected filter');
  el('h-outcome-filter').value='';el('h-outcome-filter').listeners.change();
  now+=61000;ui.tick();assert.match(root.querySelector('[data-h-market-at]').textContent,/stale/);assert.equal(root.querySelector('[data-h-pnl-at]').textContent,'P&L unresolved');

  canvasCalls.length=0;
  const history=snapshot(1,[event(1)],{equity_history:[{observed_at:1,chart_equity:'1000',net_pnl:'0',unresolved_positions:0},{observed_at:2,chart_equity:null,net_pnl:null,unresolved_positions:1},{observed_at:3,chart_equity:'1001',net_pnl:'1',unresolved_positions:0}]});
  ui.render(history);assert.equal(canvasCalls.filter(c=>c[0]==='point').length,2,'only real resolved valuations produce points');
  assert.equal(canvasCalls.filter(c=>c[0]==='line').length,3,'only grid lines are drawn; unresolved point prevents a connecting equity segment');
  assert.match(el('h-observations').innerHTML,/Unresolved/);assert.match(el('h-chart-label').textContent,/latest 500 retained/);

  responseFor=url=>({ok:true,json:async()=>url.includes('before=')?{account_id:'A',decisions:[{id:1,at:Math.floor(now/1000),mint:'mint-one',action:'SKIP',reason:'Insufficient liquidity',checks:[]}],total_matching_decisions:51,has_more_decisions:false}:{account_id:'A',decisions:[{id:2,at:Math.floor(now/1000),mint:'mint-one',action:'SKIP',reason:'Insufficient liquidity',checks:[{name:'liquidity',state:'FAIL',reason:'Below floor'}]}],total_matching_decisions:51,has_more_decisions:true,next_before:2}});
  const rejection=snapshot(1,[event(1)]);rejection.helium_workspace.rejections=[{reason:'Insufficient liquidity',count:51}];ui.render(rejection);
  click(el('h-reasons').querySelector('button'));await flush();assert.equal(el('h-drawer').open,true);assert.match(el('h-detail-decisions').innerHTML,/Below floor/);assert.equal(el('h-detail-older').hidden,false);
  click(el('h-detail-older'));await flush();assert.match(requests.at(-1),/before=2/);assert.match(el('h-detail-decisions').innerHTML,/Decision 1/);assert.match(el('h-detail-decisions').innerHTML,/Decision 2/);assert.equal(el('h-detail-older').hidden,true);

  ui.render({bot:'helium',account_id:null,initialized:false,status:{health:'NOT_INITIALIZED'}});
  assert.equal(el('h-uninitialized').hidden,false);assert.match(el('h-main-value').innerHTML,/Unavailable/);assert.equal(el('h-toasts').children.length,0);
  const index=fs.readFileSync(path.join(__dirname,'../console/index.html'),'utf8');
  assert.match(index,/id="neon-workspace"/);assert.match(index,/id="helium-panel"/);assert.match(index,/href="\/helium.css"/);
  const app=fs.readFileSync(path.join(__dirname,'../console/app.js'),'utf8');assert.match(app,/HeliumView\.setActive\(bot==='helium'\)/);assert.match(app,/HeliumView\.disconnected\(\)/);
  console.log('Helium UI behavior checks passed: identity, snapshots, fills, catch-up, stale data, costs, escaping, evidence paging, and preserved NEON surface.');
})().catch(error=>{console.error(error);process.exitCode=1;});
