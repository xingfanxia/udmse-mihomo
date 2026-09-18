"""Browser checks against the real HTTP/auth handler and a temporary controller.
Run with: uv run --with playwright python tests/admin_browser.py
"""
from pathlib import Path
import importlib.util,json,threading,time,math
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('admin_server',ROOT/'admin-server.py');admin=importlib.util.module_from_spec(spec);spec.loader.exec_module(admin)
TOKEN='ab'*32
class Fixture:
 def __init__(self):self.enabled=False;self.mode='rule';self.calls=[];self.fail=False;self.degraded=False;self.proxy='CL · TUIC'
 def status(self):
  return {'enabled':self.enabled,'routing_active':self.enabled and not self.degraded,'engine_active':self.enabled,'mode':self.mode,'state':'degraded' if self.degraded else 'active' if self.enabled else 'direct','remaining_seconds':3527 if self.enabled else None,'scope':{'type':'devices','interfaces':['br0'],'device_ipv4':'192.168.1.230','device_mac':'aa:bb:cc:dd:ee:ff','networks':[]},'selected_proxy':self.proxy if self.enabled else None,'message':None}
 def control(self,body):
  self.calls.append(body);time.sleep(.15)
  if self.fail:self.fail=False;raise admin.AdminError(502,'测试操作未完成，请重试。')
  self.enabled=body['enabled'];self.mode=body['mode'];self.degraded=False;return self.status()
f=Fixture()
class Metrics:
 def __init__(self):self.stale=False;self.reset=False
 def snapshot(self):
  now=int(time.time()*1000)
  history=[]
  if f.enabled:
   for i in range(151):
    gap=self.reset and i==75
    history.append({'timestamp_ms':now-(150-i)*2000,'download_bytes_per_second':None if gap else 950000+550000*math.sin(i/9)+200000*math.sin(i/3),'upload_bytes_per_second':None if gap else 65000+25000*math.sin(i/8),'cpu_percent':3.7,'memory_bytes':48000000,'connections':12,'session_id':'new' if self.reset and i>75 else 'old'})
  return {'state':'running' if f.enabled else 'stopped','sampled_at_ms':now,'age_seconds':9 if self.stale else .3,'stale':self.stale,'interval_seconds':2,'window_seconds':300,'metric_scope':'mihomo','upload_bytes_per_second':72000 if f.enabled else None,'download_bytes_per_second':1350000 if f.enabled else None,'upload_total_bytes':7200000 if f.enabled else None,'download_total_bytes':135000000 if f.enabled else None,'connections':12 if f.enabled else None,'cpu_percent':3.7 if f.enabled else None,'memory_bytes':48000000 if f.enabled else None,'uptime_seconds':182 if f.enabled else None,'history':history,'message':None}
metrics=Metrics();server=admin.make_server(f,TOKEN,ROOT/'admin',0,telemetry=metrics);threading.Thread(target=server.serve_forever,daemon=True).start()
url=f'http://127.0.0.1:{server.server_port}/'
shots=ROOT/'scratch/media/admin-page';shots.mkdir(parents=True,exist_ok=True)
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,channel='chrome')
  page=browser.new_page(viewport={'width':1280,'height':1040},device_scale_factor=1)
  errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto(url);page.wait_for_load_state('networkidle')
  page.get_by_label('管理口令',exact=True).fill('wrong-token');page.get_by_role('button',name='连接控制面板').click();expect(page.get_by_role('alert')).to_contain_text('口令')
  page.get_by_label('管理口令',exact=True).fill(TOKEN);page.get_by_role('button',name='连接控制面板').click();expect(page.get_by_role('switch',name='代理总开关')).to_have_attribute('aria-checked','false')
  page.get_by_role('switch',name='代理总开关').click();expect(page.get_by_role('switch')).to_have_attribute('aria-checked','true');expect(page.locator('#route-overseas')).to_have_text('代理');expect(page.locator('#route-china')).to_have_text('直连')
  assert f.calls[-1]=={'enabled':True,'mode':'rule','minutes':60}
  expect(page.locator('#metric-down')).to_contain_text('1.35');expect(page.locator('#metric-connections')).to_have_text('12')
  expect(page.locator('#chart-download')).not_to_have_attribute('d','')
  page.screenshot(path=str(shots/'desktop.png'),full_page=True)
  page.get_by_role('button',name='全部代理',exact=False).click();expect(page.locator('#route-china')).to_have_text('代理');assert f.mode=='global'
  page.get_by_role('button',name='全部直连',exact=False).click();expect(page.get_by_role('switch')).to_have_attribute('aria-checked','false');expect(page.locator('#route-overseas')).to_have_text('直连')
  page.get_by_role('button',name='智能分流',exact=False).click();expect(page.get_by_role('switch')).to_have_attribute('aria-checked','true')
  f.fail=True;page.get_by_role('button',name='全部代理',exact=False).click();expect(page.locator('#notice')).to_contain_text('测试操作未完成');assert f.mode=='rule'
  f.proxy='<img src=x onerror=alert(1)>';page.get_by_role('button',name='智能分流',exact=False).click();expect(page.locator('#proxy-name')).to_have_text(f.proxy);assert page.locator('#proxy-name img').count()==0
  f.proxy='CL · TUIC';page.get_by_role('button',name='智能分流',exact=False).click();expect(page.locator('#proxy-name')).to_have_text('CL · TUIC')
  page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(shots/'mobile.png'),full_page=True)
  assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
  metrics.stale=True;expect(page.locator('#telemetry-state')).to_have_text('采样延迟',timeout=5000);expect(page.locator('#metric-down')).to_have_text('—')
  metrics.stale=False;metrics.reset=True;expect(page.locator('#telemetry-state')).to_have_text('每 2 秒更新',timeout=5000)
  assert page.locator('#chart-download').get_attribute('d').count('M') >= 2
  expect(page.get_by_role('switch')).to_be_enabled();f.degraded=True
  expect(page.locator('#state-badge')).to_have_text('状态待确认',timeout=8000)
  expect(page.locator('#route-overseas')).to_have_text('待确认')
  page.get_by_role('button',name='退出',exact=True).click();expect(page.get_by_label('管理口令',exact=True)).to_be_visible()
  assert page.evaluate('localStorage.length + sessionStorage.length')==0
  assert not errors,errors
  browser.close();print('PASS browser controls, aggregate metrics, chart reset gaps, stale data, desktop/mobile layout and logout')
finally:server.shutdown();server.server_close()
