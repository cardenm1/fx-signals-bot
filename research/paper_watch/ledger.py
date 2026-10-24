"""Append-only authoritative CSV with SHA256 chain and crash-recoverable CSV views."""
import csv,fcntl,hashlib,io,json,os
from pathlib import Path

FIELDS=['seq','event_id','observed_utc','event_utc','kind','payload_json','prev_hash','hash']
VIEW_FIELDS=FIELDS+['signal_id','direction','D1_level','entry','SL','TP','spread_pips','R','reason']
VIEWS={'signals.csv':{'SIGNAL'},'trades.csv':{'ENTRY','EXIT','TRADE_GAP'},'rejections.csv':{'REJECTION'},'errors.csv':{'ERROR','HALT'},'market.csv':{'MARKET_CLOSED'},'stages.csv':{'ANALYSIS_CHECKPOINT','D1_ZONE','H1_CONTEXT','M5_SWEEP_RETURN','M5_WAIT','HEARTBEAT','DATA_GAP','H1_FILTERS'}}

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def event_id(kind,key):return digest(kind+'|'+key)

class IntegrityError(RuntimeError):pass

class Ledger:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.lock=open(self.root/'writer.lock','a+')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:self.lock.close();raise IntegrityError('Another writer owns the journal')
        try:
            self.events=self.read();self.ids={r['event_id']:r for r in self.events}
            self.sync_views()
        except BaseException:self.close();raise
    def close(self):
        if not self.lock.closed:fcntl.flock(self.lock,fcntl.LOCK_UN);self.lock.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def read(self):
        p=self.root/'events.csv'
        if not p.exists():return []
        raw=p.read_bytes()
        if not raw.endswith(b'\n'):raise IntegrityError('Incomplete journal tail: stop; do not truncate automatically')
        reader=csv.DictReader(io.StringIO(raw.decode()))
        if reader.fieldnames!=FIELDS:raise IntegrityError('Invalid journal schema')
        rows=[];prev='0'*64;ids=set()
        for seq,r in enumerate(reader,1):
            if set(r)!=set(FIELDS) or None in r or any(v is None for v in r.values()):raise IntegrityError('Malformed CSV row')
            if r['seq']!=str(seq) or r['prev_hash']!=prev or r['event_id'] in ids:raise IntegrityError('Sequence/chain/ID mismatch')
            expected=digest(canonical({k:r[k] for k in FIELDS if k!='hash'}))
            if r['hash']!=expected:raise IntegrityError('Journal content changed')
            json.loads(r['payload_json']);ids.add(r['event_id']);prev=r['hash'];rows.append(r)
        return rows
    @staticmethod
    def write_row(path,fields,row):
        exists=path.exists();buf=io.StringIO(newline='');writer=csv.DictWriter(buf,fieldnames=fields,lineterminator='\n')
        if not exists:writer.writeheader()
        writer.writerow(row);data=buf.getvalue().encode()
        fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
        try:
            written=os.write(fd,data)
            if written!=len(data):raise IntegrityError('Partial append: stop and inspect journal')
            os.fsync(fd)
        finally:os.close(fd)
    def append(self,kind,key,event_utc,observed_utc,payload):
        eid=event_id(kind,key);packed=canonical(payload)
        if eid in self.ids:
            old=self.ids[eid]
            if old['payload_json']!=packed or old['event_utc']!=str(event_utc):raise IntegrityError('ID already exists with different data')
            return old
        row={'seq':str(len(self.events)+1),'event_id':eid,'observed_utc':str(observed_utc),'event_utc':str(event_utc),'kind':kind,'payload_json':packed,'prev_hash':self.events[-1]['hash'] if self.events else '0'*64}
        row['hash']=digest(canonical(row));self.write_row(self.root/'events.csv',FIELDS,row)
        self.events.append(row);self.ids[eid]=row
        # Authoritative commit precedes views. Restart appends any missing views.
        self.sync_views();return row
    def view_row(self,row):
        p=json.loads(row['payload_json']);return {**row,**{k:p.get(k,'') for k in VIEW_FIELDS if k not in FIELDS}}
    def sync_views(self):
        for name,kinds in VIEWS.items():
            expected=[self.view_row(r) for r in self.events if r['kind'] in kinds];path=self.root/name
            have=[]
            if path.exists():
                raw=path.read_bytes()
                if not raw.endswith(b'\n'):raise IntegrityError('Incomplete view tail: '+name)
                reader=csv.DictReader(io.StringIO(raw.decode()))
                if reader.fieldnames!=VIEW_FIELDS:raise IntegrityError('View schema changed: '+name)
                have=list(reader)
            if len(have)>len(expected):raise IntegrityError('Unexpected view rows: '+name)
            for old,want in zip(have,expected):
                want={k:str(v) for k,v in want.items()}
                if old!=want:raise IntegrityError('View altered: '+name)
            for row in expected[len(have):]:self.write_row(path,VIEW_FIELDS,row)
