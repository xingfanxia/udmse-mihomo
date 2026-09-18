"""Real browser checks for Mac auto-login and persistent theme; no live routing."""
import importlib.util
from pathlib import Path
import threading
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
admin = module('admin', 'admin-server.py')
bridge = module('bridge', 'local-dashboard.py')

class Controller:
    def __init__(self):
        self.enabled = False
        self.mode = 'rule'
    def status(self):
        return {'enabled': self.enabled, 'routing_active': self.enabled,
                'engine_active': self.enabled, 'mode': self.mode,
                'state': 'active' if self.enabled else 'direct',
                'remaining_seconds': 300 if self.enabled else None,
                'scope': {'type': 'devices', 'device_ipv4': '192.168.1.230', 'interfaces': ['br0']},
                'selected_proxy': None, 'message': None}
    def control(self, body):
        self.enabled, self.mode = body['enabled'], body['mode']
        return self.status()

credential = 'example-pass-42'
controller = Controller()
upstream = admin.make_server(controller, credential, ROOT / 'admin', 0)
local = bridge.make_server(credential, port=0, upstream_port=upstream.server_port,
                          upstream_host_port=upstream.server_port)
for server in (upstream, local):
    threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel='chrome')
        page = browser.new_page(viewport={'width': 1280, 'height': 1050}, color_scheme='light')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        url = f'http://127.0.0.1:{local.server_port}'
        page.goto(url)
        expect(page.locator('#dashboard')).to_be_visible()
        expect(page.locator('#login')).to_be_hidden()
        expect(page.locator('#logout')).to_be_hidden()
        expect(page.locator('.connection')).to_contain_text('此 Mac 已自动连接')
        page.get_by_role('switch').click()
        expect(page.get_by_role('switch')).to_have_attribute('aria-checked', 'true')
        page.get_by_role('switch').click()
        expect(page.get_by_role('switch')).to_have_attribute('aria-checked', 'false')
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        page.get_by_role('button', name='深色模式').click()
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        page.reload()
        expect(page.locator('#dashboard')).to_be_visible()
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        assert page.evaluate('JSON.stringify(localStorage)') == '{"udm-theme":"dark"}'
        assert page.evaluate('sessionStorage.length') == 0
        shots = ROOT / 'scratch/media/local-dashboard'
        shots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shots / 'dark-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(shots / 'dark-mobile.png'), full_page=True)
        session = page.request.get(url + '/local-session').json()
        assert session['token'] != credential
        assert not errors, errors
        browser.close()
        print('PASS auto-login, authenticated controls, theme persistence, desktop/mobile and no browser credential storage')
finally:
    for server in (local, upstream):
        server.shutdown()
        server.server_close()
