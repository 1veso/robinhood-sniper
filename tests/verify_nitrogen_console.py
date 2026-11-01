"""Optional real Chromium acceptance; separate synthetic account, no provider calls."""
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
from sniper.nitrogen.demo import demo,purchase,evidence,address,SyntheticModel,response
from sniper.nitrogen.book import Book,bound_config


def main():
    from playwright.sync_api import sync_playwright
    output=ROOT/'runtime/nitrogen-ui';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);(root/'nitrogen_bot').mkdir();shutil.copy(ROOT/'nitrogen_bot/config.json',root/'nitrogen_bot/config.json')
        data=demo(root);app=Path(data['root']);shutil.copytree(ROOT/'console',app/'console');now=int(time.time())
        with closing(Book(app,bound_config(app),demo=True,clock=lambda:now)) as book:
            id=book.discover_purchase(purchase(now,address(30),address(31)),now);e=evidence(book,book.lead(id),now)
            book.assess(id,e,SyntheticModel(book),now)
            wid=book.discover_purchase(purchase(now,address(40),address(41)),now);we=evidence(book,book.lead(wid),now)
            book.assess(wid,we,SyntheticModel(book,[response('watch')]),now)
            from sniper.nitrogen.service import head
            head(book,providers=object())
        with patch('sniper.console.ConsoleData.jobs',return_value={'available':False,'items':[]}):
            server=make_server(app,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    url=f'http://127.0.0.1:{server.server_port}';page.goto(url+'/?bot=nitrogen')
                    page.locator('#nitrogen-panel h2',has_text='Four Jev answers').wait_for()
                    assert page.locator('#account-title').inner_text()=='NITROGEN'
                    assert page.get_by_role('button',name='NITROGEN, slot 7',exact=True).is_enabled()
                    assert page.get_by_role('button',name='Radon, slot 8',exact=True).is_enabled()
                    assert page.locator('#nitrogen-panel').get_by_text('APPROVE #',exact=False).count()>0
                    assert 'SYNTHETIC' in page.locator('#trial-state').inner_text()
                    assert not page.locator('.market-grid').is_visible()
                    page.screenshot(path=str(output/'desktop.png'),full_page=True)
                    page.get_by_role('button',name='Radon, slot 8',exact=True).click()
                    page.wait_for_function("document.getElementById('account-title').textContent === 'Radon'")
                    assert not page.locator('#nitrogen-panel').is_visible()
                    page.get_by_role('button',name='NITROGEN, slot 7',exact=True).click()
                    page.locator('#nitrogen-panel h2',has_text='Four Jev answers').wait_for()
                    page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(output/'mobile.png'),full_page=True)
                    assert not errors,errors
                    health=page.request.get(url+'/api/health').json();assert health['accounts']['nitrogen']['slot']==7 and health['accounts']['radon']['slot']==8
                    assert page.request.post(url+'/api/state?bot=nitrogen').status==403
                    browser.close()
                (output/'result.json').write_text(json.dumps({'result':'passed','evidence':'synthetic','page_errors':errors,'screenshots':['desktop.png','mobile.png']},indent=2))
            finally:server.shutdown();server.server_close();thread.join()
    print(output/'result.json')


if __name__=='__main__':main()
