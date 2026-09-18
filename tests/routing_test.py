#!/usr/bin/env python3
import json,os,subprocess,tempfile,unittest
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
class RoutingTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.install=self.root/'install';self.install.mkdir();self.state=self.root/'runtime';self.net=self.root/'net.json';self.bin=self.root/'bin';self.bin.mkdir()
  self.net.write_text(json.dumps({'tables':{'filter':{'INPUT':[['-j','UNIFI']], 'UNIFI':[]},'nat':{'PREROUTING':[['-j','UNIFI']], 'UNIFI':[]},'mangle':{'PREROUTING':[['-j','UNIFI']], 'UNIFI':[]}},'rules':['32000: from all lookup main'],'route':'','active':False,'calls':[]}))
  fake=self.bin/'fake';fake.write_text((REPO/'tests/fake_net.py').read_text());fake.chmod(0o755)
  for name in ['iptables','iptables-save','ip','ss','curl','systemctl','modprobe','conntrack','flock']:(self.bin/name).symlink_to(fake)
  (self.install/'mihomo').write_text('#!/bin/sh\nexit 0\n');(self.install/'mihomo').chmod(0o755)
  self.scope="SCOPE=devices\nLAN_INTERFACES=(br0)\nDEVICE_IPV4=192.168.1.100\nDEVICE_MAC=aa:bb:cc:dd:ee:ff\nSOURCE_CIDRS=()\nIPV6_POLICY=require-no-default\nDNS_LISTEN_IPV4=192.168.1.1\n"
  (self.install/'routing.env').write_text(self.scope)
  (self.install/'config.yaml').write_text('dns:\n  listen: 192.168.1.1:1053\n')
  self.env=dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ['PATH'],MIHOMO_DIR=str(self.install),MIHOMO_STATE_DIR=str(self.state),FAKE_NET=str(self.net))
 def data(self):return json.loads(self.net.read_text())
 def change(self,**kw):
  d=self.data();d.update(kw);self.net.write_text(json.dumps(d))
 def run_cmd(self,cmd,ok=True):
  p=subprocess.run(['bash',str(REPO/'mihomo-routing.sh'),cmd],env=self.env,text=True,capture_output=True)
  if ok:self.assertEqual(p.returncode,0,p.stderr)
  else:self.assertNotEqual(p.returncode,0,p.stdout)
  return p
 def start(self):
  self.run_cmd('guard');self.change(active=True);(self.state/'wanted').touch();self.run_cmd('attach')
 def assert_original(self):
  d=self.data()
  for table,chain in [('filter','INPUT'),('nat','PREROUTING'),('mangle','PREROUTING')]:self.assertEqual(d['tables'][table],{chain:[['-j','UNIFI']],'UNIFI':[]})
  self.assertEqual(d['rules'],['32000: from all lookup main']);self.assertEqual(d['route'],'')
 def test_device_scope_and_cleanup(self):
  self.start();self.run_cmd('check');d=self.data()
  for table,chain in [('nat','PREROUTING'),('mangle','PREROUTING')]:
   hook=d['tables'][table][chain][0]
   for token in ['br0','192.168.1.100/32','aa:bb:cc:dd:ee:ff']:self.assertIn(token,hook)
  self.run_cmd('cleanup');self.assert_original()
  calls=self.data()['calls'];self.assertTrue(any(c[0]=='conntrack' and '--reply-port-src' in c and '1053' in c for c in calls))
 def test_empty_scope_refused_without_rules(self):
  (self.install/'routing.env').write_text('SCOPE=networks\nLAN_INTERFACES=(br0)\nSOURCE_CIDRS=()\n')
  self.run_cmd('guard',False);self.assert_original()
 def test_network_scope_keeps_interface_and_source(self):
  (self.install/'routing.env').write_text('SCOPE=networks\nLAN_INTERFACES=(br2)\nSOURCE_CIDRS=(192.168.2.0/24)\nDNS_LISTEN_IPV4=192.168.1.1\n')
  self.start();hook=self.data()['tables']['mangle']['PREROUTING'][0]
  self.assertIn('br2',hook);self.assertIn('192.168.2.0/24',hook);self.assertNotIn('--mac-source',hook)
  self.run_cmd('cleanup');self.assert_original()
 def test_ipv6_default_refuses_activation(self):
  self.change(ipv6=True);self.run_cmd('guard',False);self.assert_original()
 def test_collision_is_not_deleted(self):
  d=self.data();d['tables']['nat']['MHM_DNS']=[['-j','ACCEPT']];self.net.write_text(json.dumps(d))
  self.run_cmd('guard',False);self.run_cmd('cleanup');self.assertEqual(self.data()['tables']['nat']['MHM_DNS'],[['-j','ACCEPT']])
 def test_mark_overlap_rejected(self):
  self.change(rules=['32000: from all lookup main','100: from all fwmark 0x0/0x400 lookup 9'])
  self.run_cmd('guard',False);self.assertFalse((self.state/'owned').exists())
 def test_unifi_high_bit_masks_do_not_collide(self):
  d=self.data();d['tables']['mangle']['UNIFI']=[['-j','CONNMARK','--restore-mark','--nfmask','0x7e0000','--ctmask','0x7e0000']];self.net.write_text(json.dumps(d))
  self.run_cmd('guard')
  self.assertTrue((self.state/'owned').exists())
 def test_mid_attach_failure_removes_interception(self):
  self.run_cmd('guard');self.change(active=True,fail=['nat','-I','PREROUTING']);(self.state/'wanted').touch()
  self.run_cmd('attach',False);self.run_cmd('cleanup');self.assert_original()
 def test_scope_edit_does_not_break_cleanup(self):
  self.start();(self.install/'routing.env').write_text(self.scope.replace('192.168.1.100','192.168.1.200'))
  self.run_cmd('preflight',False);self.change(missing_bridge=True);self.run_cmd('cleanup');self.assert_original()
 def test_guard_reordering_detected(self):
  self.start();d=self.data();d['tables']['filter']['INPUT'].insert(0,['-j','ACCEPT']);self.net.write_text(json.dumps(d));self.run_cmd('check-guard',False)
 def test_missing_internal_rule_detected(self):
  self.start();d=self.data();d['tables']['mangle']['MHM_TPROXY'].pop(0);self.net.write_text(json.dumps(d));self.run_cmd('check',False)
if __name__=='__main__':unittest.main()
