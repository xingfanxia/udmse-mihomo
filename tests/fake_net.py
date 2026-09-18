#!/usr/bin/env python3
"""Stateful iptables/ip/systemd doubles: never touch the host network."""
import json,os,shlex,sys
from pathlib import Path
p=Path(os.environ['FAKE_NET']);d=json.loads(p.read_text());tool=Path(sys.argv[0]).name;a=sys.argv[1:]
d['calls'].append([tool]+a)
def end(code=0,text=''):
 p.write_text(json.dumps(d))
 if text: print(text)
 sys.exit(code)
if tool=='iptables':
 if a[:2]==['-w','5']:a=a[2:]
 table=a[1];op=a[2];chain=a[3] if len(a)>3 else None;args=a[4:];chains=d['tables'][table]
 if d.get('fail')==[table,op,chain]:d['fail']=None;end(2)
 if op=='-S':
  if chain not in chains:end(1)
  head=('-P '+chain+' ACCEPT') if chain in ('INPUT','PREROUTING') else '-N '+chain
  end(text='\n'.join([head]+['-A '+chain+' '+shlex.join(x) for x in chains[chain]]))
 if op=='-N':
  if chain in chains:end(1)
  chains[chain]=[];end()
 if chain not in chains:end(1)
 if op=='-C':end(0 if args in chains[chain] else 1)
 if op=='-A':chains[chain].append(args);end()
 if op=='-I':
  pos=int(args.pop(0))-1 if args and args[0].isdigit() else 0
  chains[chain].insert(pos,args);end()
 if op=='-D':
  if args not in chains[chain]:end(1)
  chains[chain].remove(args);end()
 if op=='-F':chains[chain]=[];end()
 if op=='-X':
  if any('-j' in row and row[row.index('-j')+1]==chain for rows in chains.values() for row in rows):end(1)
  del chains[chain];end()
if tool=='iptables-save':
 end(text='\n'.join('-A '+c+' '+shlex.join(r) for cs in d['tables'].values() for c,rs in cs.items() for r in rs))
if tool=='ip':
 if a[:4]==['-4','-j','addr','show']:end(text=json.dumps([{'addr_info':[{'local':'192.168.1.1'}]}]))
 if a[:3]==['link','show','dev']:end(1 if d.get('missing_bridge') else 0)
 if a[:4]==['-6','-j','route','show']:end(text=json.dumps([{'dst':'default'}] if d.get('ipv6') else []))
 if a[:2]==['rule','show'] or a==['rule']:
  end(text='\n'.join(d['rules']))
 if a[:2]==['rule','add']:
  d['rules'].append('10010: from all fwmark 0x400/0x400 lookup 104');end()
 if a[:2]==['rule','del']:
  rule='10010: from all fwmark 0x400/0x400 lookup 104'
  if rule not in d['rules']:end(2)
  d['rules'].remove(rule);end()
 if a[:3]==['route','show','table']:end(text=d['route'])
 if a[:2]==['route','add']:
  if d['route']:end(2)
  d['route']='local default dev lo scope host';end()
 if a[:2]==['route','del']:
  if not d['route']:end(2)
  d['route']='';end()
if tool=='systemctl':
 if a[:2]==['is-active','--quiet']:end(0 if d['active'] else 3)
 end()
if tool=='ss':
 if not d['active']:end()
 end(text='\n'.join('LISTEN 0 128 '+addr+' 0.0.0.0:*' for addr in ['127.0.0.1:7890','127.0.0.1:7893','192.168.1.1:1053']))
if tool=='curl':end(0 if d.get('healthy',True) else 22)
if tool in ['modprobe','conntrack','flock']:end()
end(2,'Unhandled test command: '+tool+' '+shlex.join(a))
