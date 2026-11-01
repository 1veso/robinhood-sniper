"""Optional real-browser smoke test; synthetic account, loopback only, no providers."""
from pathlib import Path
import json
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from sniper.console import make_server
from sniper.radon.demo import demo


def main():
    from playwright.sync_api import sync_playwright
    output=ROOT/'runtime/radon-ui';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);(root/'radon_bot').mkdir()
        shutil.copy(ROOT/'radon_bot/config.json',root/'radon_bot/config.json')
        data=demo(root);app=Path(data['root'])
        shutil.copytree(ROOT/'console',app/'console')
        with patch('sniper.console.ConsoleData.jobs',return_value={'available':False,'items':[]}):
            server=make_server(app,port=0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    url=f'http://127.0.0.1:{server.server_port}/?bot=radon'
                    page.goto(url)
                    page.locator('#radon-panel h2',has_text='Copied positions').wait_for()
                    assert page.locator('#account-title').inner_text()=='Radon'
                    assert page.get_by_role('button',name='NITROGEN, slot 7').is_enabled()
                    assert 'SYNTHETIC' in page.locator('#trial-state').inner_text()
                    assert page.locator('#radon-panel').get_by_text('Historical conditional research',exact=True).count()==1
                    assert not page.locator('.market-grid').is_visible()
                    page.screenshot(path=str(output/'desktop.png'),full_page=True)
                    page.get_by_role('button',name='Helium',exact=True).click()
                    page.wait_for_function("document.getElementById('account-title').textContent === 'Helium'")
                    page.get_by_role('button',name='Radon, slot 8',exact=True).click()
                    page.locator('#radon-panel h2',has_text='Copied positions').wait_for()
                    page.set_viewport_size({'width':390,'height':844})
                    page.screenshot(path=str(output/'mobile.png'),full_page=True)
                    assert not errors,errors
                    response=page.request.post(url.split('/?')[0]+'/api/state?bot=radon')
                    assert response.status==403
                    browser.close()
                (output/'result.json').write_text(json.dumps({'result':'passed','evidence':'synthetic','page_errors':errors,'screenshots':['desktop.png','mobile.png']},indent=2))
            finally:
                server.shutdown();server.server_close();thread.join()
    print(output/'result.json')


if __name__=='__main__':main()
