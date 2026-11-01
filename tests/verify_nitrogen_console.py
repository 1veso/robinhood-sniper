"""Deterministic, isolated cockpit preview and Chromium acceptance. No provider calls."""
from contextlib import closing
from pathlib import Path
import json
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from sniper.console import make_server
from sniper.nitrogen.demo import purchase,evidence,address,SyntheticModel,response
from sniper.nitrogen.book import Book
from sniper.nitrogen.config import load,encode
from sniper.nitrogen.view import view


def fixture(root,now):
    """Actual synthetic ledger operations; never chart-only invented balances."""
    (root/'nitrogen_bot').mkdir();cfg=load(ROOT);cfg['tracked_wallets']=[address(1)]
    (root/'nitrogen_bot/config.json').write_text(encode(cfg));clock=[now-1800]
    book=Book(root,cfg,initialize=True,demo=True,clock=lambda:clock[0])
    def lead(n,symbol,answer=None,fail=None):
        at=clock[0];id=book.discover_purchase(purchase(at,address(n),address(n+1)),at)
        e=evidence(book,book.lead(id),at);e['quote']['symbol']=symbol
        if fail=='liquidity':e['quote']['liquidity']='1000'
        if fail=='mint_disabled':e['facts']['mint_disabled']=False
        book.assess(id,e,SyntheticModel(book,[answer or response()]),at)
        return id,e
    book.publish();id,e=lead(20,'MOSS');book.approve(book.pending()[0]['id'],e,clock[0]);book.publish()
    # Recorded quotes contain both advances and drawdowns. Costs are the real baseline.
    prices=['1.04','1.15','1.10','1.29','1.24','1.45','1.61','1.48','1.72','1.84','2.12','2.30','2.21','2.51','2.30','1.65']
    for price in prices:
        clock[0]+=60;q=dict(e['quote'],observed_at=clock[0],sol_observed_at=clock[0],price=price);book.watch(q,clock[0]);book.publish()
    clock[0]+=60;id,e=lead(30,'FERN');book.approve(book.pending()[0]['id'],e,clock[0]);book.publish()
    for price in ['1.05','1.15','1.10','1.25','1.30','1.22','1.42','1.51','1.40','1.60']:
        clock[0]+=60;book.watch(dict(e['quote'],observed_at=clock[0],sol_observed_at=clock[0],price=price),clock[0]);book.publish()
    clock[0]=now
    book.watch(dict(e['quote'],observed_at=now,sol_observed_at=now,price='1.60'),now);book.publish()
    lead(40,'WATCH',response('watch'))
    lead(50,'THIN',fail='liquidity');lead(52,'MINT',fail='mint_disabled')
    lead(60,'LATE',response(late=.6));lead(62,'CROWD',response(organic=.5));lead(64,'NOEXIT',response(score=3));lead(66,'WEAK',response(probability=.69))
    lead(70,'PENDING')
    for kind in ('scan','watch','head'):
        rid=book.run_start(kind,now-2);book.run_finish(rid,now,[])
    book.publish()
    return book,clock,lead


def main():
    from playwright.sync_api import sync_playwright
    output=ROOT/'runtime/nitrogen-cockpit-preview';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        app=Path(tmp);now=int(time.time());book,clock,lead=fixture(app,now)
        snapshot=view(app,now);assert not snapshot['cockpit'].get('unavailable'),snapshot
        with patch('sniper.console.ConsoleData.jobs',return_value={'available':False,'items':[]}):
            server=make_server(app,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                    page=browser.new_page(viewport={'width':600,'height':1068},device_scale_factor=1);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    url=f'http://127.0.0.1:{server.server_port}';page.goto(url+'/?bot=nitrogen')
                    page.locator('#ni-evidence',has_text='SYNTHETIC PREVIEW').wait_for()
                    assert page.locator('.ni-role').count()==6
                    assert page.locator('.ni-role').evaluate_all('(els)=>els.every(e=>e.getBoundingClientRect().right<=e.closest(".ni-card").getBoundingClientRect().right)')
                    assert page.locator('.ni-kill i').first.evaluate('(el)=>getComputedStyle(el).backgroundImage')!='none'
                    assert page.locator('.ni-role').evaluate_all('(els)=>els.map(e=>e.dataset.role)')==['SEARCH','RISK','WHALE','SNIPER','EXIT','JUDGE']
                    assert page.locator('.ni-toast').count()==0,'Initial history must not toast'
                    assert page.locator('#ni-mode').inner_text()=='PAPER'
                    assert page.locator('#ni-badge-SNIPER').inner_text()=='1 pending'
                    assert page.locator('#ni-vetoes').inner_text()=='4 vetoes'
                    assert not page.locator('.market-grid').is_visible()
                    page.screenshot(path=str(output/'reference-600.png'),full_page=True,animations='disabled')
                    page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(output/'mobile-390.png'),full_page=True,animations='disabled')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert not page.locator('.rail').is_visible()
                    page.locator('[data-detail="SNIPER"]').first.click();page.locator('#ni-drawer[open]').wait_for()
                    assert 'APPROVE #' in page.locator('#ni-drawer-body').inner_text()
                    page.screenshot(path=str(output/'mobile-pending.png'),full_page=True,animations='disabled');page.locator('#ni-close').click()
                    page.set_viewport_size({'width':1440,'height':900});page.screenshot(path=str(output/'desktop-1440.png'),animations='disabled')
                    page.screenshot(path=str(output/'desktop-full.png'),full_page=True,animations='disabled')
                    page.locator('[data-detail="JUDGE"]').first.click();assert 'Separate action confidence' in page.locator('#ni-drawer-body').inner_text()
                    page.screenshot(path=str(output/'desktop-judge.png'),animations='disabled');page.locator('#ni-close').click()
                    # Only a new durable judgment after the initial snapshot produces a toast.
                    clock[0]+=1;lead(80,'LONGSYMBOLWITHMANYLETTERS',response('pass'));book.publish();new=view(app,clock[0]);new['version']='preview'
                    page.evaluate('(s)=>window.NitrogenView.render(s)',new)
                    assert page.locator('.ni-toast').count()==1
                    event_id=page.locator('.ni-toast').get_attribute('data-event-id')
                    page.set_viewport_size({'width':600,'height':1068});page.screenshot(path=str(output/'notification-600.png'),full_page=True,animations='disabled')
                    page.evaluate('(s)=>window.NitrogenView.render(s)',new)
                    assert page.locator('.ni-toast').get_attribute('data-event-id')==event_id
                    # Large/negative numerical widths and long token metadata do not escape the hero/rows.
                    from copy import deepcopy
                    wide=deepcopy(new);wide['summary']['equity']='-123456789.12';wide['cockpit']['tokens'][0]['symbol']='EXTREMELYLONGRECORDEDSYMBOL'
                    page.evaluate('(s)=>window.NitrogenView.render(s)',wide)
                    assert page.locator('.ni-equity').evaluate('(el)=>el.getBoundingClientRect().right<=el.closest(".ni-hero").getBoundingClientRect().right')
                    page.evaluate('(s)=>window.NitrogenView.render(s)',new)
                    # Reconnect snapshots consume history without emitting it again.
                    page.evaluate('window.NitrogenView.disconnected()');clock[0]+=1;lead(90,'RECONNECT',response('pass'));book.publish();reconnected=view(app,clock[0])
                    page.evaluate('(s)=>window.NitrogenView.render(s)',reconnected)
                    assert page.locator('.ni-toast').get_attribute('data-event-id')==event_id
                    # A snapshot page gap is recovered from the durable account-bound cursor.
                    for i in range(105):book.decision(address(90),'EVIDENCE_BLOCKED',{'lead':'fixture-gap','reason':'synthetic evidence unavailable'},clock[0])
                    book.publish();gap=view(app,clock[0])
                    with page.expect_response(lambda r:'/api/nitrogen-journal?' in r.url and 'after=' in r.url) as recovered:
                        page.evaluate('(s)=>window.NitrogenView.render(s)',gap)
                    assert recovered.value.status==200
                    # A response bound to another account cannot populate this workspace.
                    foreign=deepcopy(gap);foreign['cockpit']['account_id']='radon:foreign';foreign['summary']['equity']='9999999'
                    before=page.locator('#ni-whole').inner_text();page.evaluate('(s)=>window.NitrogenView.render(s)',foreign)
                    assert page.locator('#ni-whole').inner_text()==before
                    # Unknown valuations and stale workers retain the composition.
                    clock[0]+=600;book.publish();stale=view(app,clock[0]);page.evaluate('(s)=>window.NitrogenView.render(s)',stale)
                    assert page.locator('#ni-whole').inner_text()=='—';page.evaluate('document.querySelector(".ni-toast-host").innerHTML=""')
                    page.screenshot(path=str(output/'stale-unknown-600.png'),full_page=True,animations='disabled')
                    # Actual losing exit plus earlier winner; negative state separately tested with a fresh account below.
                    with tempfile.TemporaryDirectory() as empty:
                        empty_state=view(Path(empty),clock[0]);page.evaluate('(s)=>window.NitrogenView.render(s)',empty_state)
                        assert page.locator('#ni-evidence').inner_text()=='NOT INITIALIZED'
                        page.screenshot(path=str(output/'empty-600.png'),full_page=True,animations='disabled')
                    with tempfile.TemporaryDirectory() as negative:
                        nr=Path(negative);(nr/'nitrogen_bot').mkdir();(nr/'nitrogen_bot/config.json').write_text(encode(book.cfg))
                        with closing(Book(nr,book.cfg,initialize=True,demo=True,clock=lambda:clock[0])) as loss:
                            lid=loss.discover_purchase(purchase(clock[0]),clock[0]);ev=evidence(loss,loss.lead(lid),clock[0]);loss.assess(lid,ev,SyntheticModel(loss),clock[0]);loss.approve(loss.pending()[0]['id'],ev,clock[0]);loss.publish()
                            clock[0]+=1;loss.watch(dict(ev['quote'],price='.5',observed_at=clock[0],sol_observed_at=clock[0]),clock[0]);loss.publish();neg=view(nr,clock[0]);page.evaluate('(s)=>window.NitrogenView.render(s)',neg)
                            assert page.locator('#ni-pnl').inner_text().startswith('−$')
                            page.screenshot(path=str(output/'negative-600.png'),full_page=True,animations='disabled')
                    page.set_viewport_size({'width':1440,'height':900})
                    page.get_by_role('button',name='Radon, slot 8',exact=True).click()
                    page.wait_for_function("document.getElementById('account-title').textContent === 'Radon'")
                    assert not page.locator('#nitrogen-panel').is_visible()
                    assert not page.locator('body').evaluate('(el)=>el.classList.contains("nitrogen-active")')
                    page.get_by_role('button',name='NITROGEN, slot 7',exact=True).click()
                    page.locator('#ni-evidence',has_text='SYNTHETIC PREVIEW').wait_for();assert page.locator('.ni-toast').count()==0
                    page.emulate_media(reduced_motion='reduce');assert page.locator('#ni-ticker-track').evaluate('(e)=>getComputedStyle(e).animationName')=='none'
                    assert not errors,errors
                    health=page.request.get(url+'/api/health').json();assert health['accounts']['nitrogen']['slot']==7 and health['accounts']['radon']['slot']==8
                    assert page.request.post(url+'/api/state?bot=nitrogen').status==403
                    browser.close()
                (output/'result.json').write_text(json.dumps({'result':'passed','evidence':'synthetic isolated ledger operations; no model or market calls','page_errors':errors,'screenshots':sorted(x.name for x in output.glob('*.png'))},indent=2))
            finally:server.shutdown();server.server_close();thread.join();book.close()
    print(output/'result.json')


if __name__=='__main__':main()
