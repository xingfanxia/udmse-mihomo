#!/usr/bin/env python3
"""Real pinned-engine DNS/provider smoke test. Requires an isolated Linux netns.

sudo unshare -n sh -c 'ip link set lo up; python3 tests/bootstrap_smoke.py BIN CONFIG_JSON'
Never run this against the gateway's main network namespace.
"""
import copy,http.server,json,os,socket,struct,subprocess,sys,tempfile,threading,time,urllib.request
from pathlib import Path
if Path('/proc/self/ns/net').stat().st_ino==Path('/proc/1/ns/net').stat().st_ino:
 raise SystemExit('Refusing to start test listeners in the host network namespace')
binary,template=sys.argv[1:3]
admin_module=Path(sys.argv[3]) if len(sys.argv)>3 else None
base=json.loads(Path(template).read_text())
dns_ip=base['dns']['listen'].rsplit(':',1)[0]
subprocess.run(['ip','addr','add',dns_ip+'/32','dev','lo'],check=True)
counts={'dns':0,'http':0}
class Provider(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  counts['http']+=1
  body=b'proxies:\n  - name: fixture-proxy\n    type: http\n    server: 127.0.0.1\n    port: 18081\n'
  self.send_response(200);self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
httpd=http.server.ThreadingHTTPServer(('127.0.0.1',18080),Provider)
threading.Thread(target=httpd.serve_forever,daemon=True).start()
udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);udp.bind(('127.0.0.1',15353))
def dns():
 while True:
  data,peer=udp.recvfrom(2048);counts['dns']+=1
  pos=12
  while data[pos]:pos+=1+data[pos]
  pos+=1;kind=struct.unpack('!H',data[pos:pos+2])[0];end=pos+4
  answer=b'\xc0\x0c'+struct.pack('!HHIH',1,1,1,4)+socket.inet_aton('127.0.0.1') if kind==1 else b''
  packet=data[:2]+struct.pack('!HHHHH',0x8180,1,1 if answer else 0,0,0)+data[12:end]+answer
  udp.sendto(packet,peer)
threading.Thread(target=dns,daemon=True).start()
for enable_direct in (True,False):
 before=counts.copy()
 with tempfile.TemporaryDirectory() as tmp:
  cfg=copy.deepcopy(base)
  cfg['dns']['default-nameserver']=['192.0.2.1']
  cfg['dns']['nameserver']=['https://192.0.2.2/dns-query#PROXY']
  cfg['dns']['nameserver-policy']={}
  cfg['dns']['proxy-server-nameserver']=['192.0.2.1']
  cfg['dns']['direct-nameserver']=['udp://127.0.0.1:15353'] if enable_direct else []
  cfg['rule-providers']={};cfg['rules']=['MATCH,PROXY']
  cfg['proxy-providers']['my-sub']['url']='http://subscription.test:18080/nodes.yaml'
  cfg['proxy-providers']['my-sub']['health-check']['enable']=False
  conf=Path(tmp)/'config.json';conf.write_text(json.dumps(cfg))
  with (Path(tmp)/'engine.log').open('w') as log:
   process=subprocess.Popen([binary,'-d',tmp,'-f',str(conf)],stdout=log,stderr=log)
   try:
    group=None
    for _ in range(50):
     try:
      req=urllib.request.Request('http://127.0.0.1:9090/proxies/PROXY',headers={'Authorization':'Bearer '+cfg['secret']})
      with urllib.request.urlopen(req,timeout=.2) as r:group=json.load(r)
      if enable_direct and group.get('now')=='fixture-proxy':break
     except (OSError,ValueError):pass
     if process.poll() is not None:raise AssertionError('Engine exited unexpectedly')
     time.sleep(.1)
    assert group is not None,'API never became ready'
    if enable_direct:
     assert group['now']=='fixture-proxy',group
     assert counts['http']>before['http'] and counts['dns']>before['dns']
     tcp=subprocess.check_output(['ss','-H','-lnt'],text=True)
     udp_ports=subprocess.check_output(['ss','-H','-lnu'],text=True)
     for addr in ['127.0.0.1:7890','127.0.0.1:7893','127.0.0.1:9090',dns_ip+':1053']:
      assert addr in tcp,addr+' TCP listener missing'
     assert '127.0.0.1:7893' in udp_ports and dns_ip+':1053' in udp_ports
     print('PASS actual TCP/UDP listener addresses match the guarded runtime contract')
     print('PASS empty-cache subscription bootstrapped through dedicated direct DNS')
     if admin_module:
      import importlib.util
      spec=importlib.util.spec_from_file_location('admin_server',admin_module);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
      (Path(tmp)/'config.yaml').write_text('mode: rule\nsecret: '+json.dumps(cfg['secret'])+'\n')
      controller=module.Controller(root=Path(tmp),state=Path(tmp)/'state',lifecycle_lock=Path(tmp)/'lock')
      controller.apply_mode('global')
      assert controller.api('GET','/configs')['mode']=='global'
      assert controller.api('GET','/proxies/GLOBAL')['now']=='PROXY'
      controller.apply_mode('rule')
      assert controller.api('GET','/configs')['mode']=='rule'
      print('PASS admin global mode selects PROXY and returns to smart mode on the real engine')
    else:
     assert group['now']=='REJECT',group
     assert counts['http']==before['http']
     print('PASS missing bootstrap resolver leaves empty group REJECT, with no direct fallback')
   finally:
    process.terminate()
    try:process.wait(timeout=4)
    except subprocess.TimeoutExpired:process.kill();process.wait()
httpd.shutdown();udp.close()
