"""Durable state on a dedicated Git branch. No force pushes, caches or tokens on disk."""
import argparse,base64,json,os,re,subprocess
from pathlib import Path

BRANCH='paper-watch-state-v1'
class StoreError(RuntimeError):pass

class GitStore:
    def __init__(self,root,repository=None,token=None,remote=None):
        self.root=Path(root).resolve()
        self.env=os.environ.copy()
        if remote is None:
            repository=repository or os.environ.get('GITHUB_REPOSITORY','')
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):raise StoreError('Invalid GitHub repository')
            remote=f'https://github.com/{repository}.git'
            token=token or os.environ.get('GITHUB_TOKEN')
            if not token:raise StoreError('GITHUB_TOKEN required for state storage')
            auth=base64.b64encode(('x-access-token:'+token).encode()).decode()
            self.env.update(GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',GIT_CONFIG_VALUE_0='AUTHORIZATION: basic '+auth)
        # Explicit local remotes are for offline integration tests only.
        elif not (Path(remote).is_absolute() and Path(remote).is_dir()):raise StoreError('Test remote must be an existing absolute local directory')
        self.remote=remote
    def git(self,*args,check=True):
        proc=subprocess.run(['git','-c','core.hooksPath=/dev/null',*args],cwd=self.root,env=self.env,capture_output=True,text=True)
        if check and proc.returncode:raise StoreError('State Git operation failed: '+args[0]+'; no publication allowed')
        return proc
    def open(self):
        if not (self.root/'.git').exists():raise StoreError('State repository not restored')
        if self.git('remote','get-url','origin').stdout.strip()!=self.remote:raise StoreError('State remote mismatch')
        return self
    def restore(self):
        if self.root.exists() and any(self.root.iterdir()):raise StoreError('Restore directory must be empty')
        self.root.mkdir(parents=True,exist_ok=True)
        self.git('init','--quiet');self.git('remote','add','origin',self.remote)
        # Absence of state NEVER creates a replacement session implicitly.
        self.git('fetch','--quiet','origin','refs/heads/'+BRANCH)
        self.git('checkout','--quiet','--detach','FETCH_HEAD')
        if not (self.root/'deployment.json').is_file():raise StoreError('Missing deployment manifest')
        return self
    def validate_files(self):
        if not (self.root/'deployment.json').is_file() or not (self.root/'session/events.csv').is_file():raise StoreError('Incomplete state')
        # Verify authoritative chains without acquiring another lock: publish()
        # already holds both ledgers while committing its intent to the branch.
        from .ledger import Ledger,VIEWS
        for folder in [self.root/'session',self.root/'session/notifications']:
            if (folder/'events.csv').exists():
                reader=object.__new__(Ledger);reader.root=folder;reader.read()
        allowed={'deployment.json','.gitignore','status.json','session/events.csv','session/warmup_BID.csv','session/warmup_ASK.csv','session/notifications/events.csv'}
        allowed.update('session/'+name for name in VIEWS)
        for p in self.root.rglob('*'):
            rel=p.relative_to(self.root)
            if '.git' in rel.parts:continue
            if p.is_symlink():raise StoreError('Symlink in state refused')
            if not p.is_file():continue
            if p.name=='writer.lock':continue
            if str(rel) in allowed:continue
            if len(rel.parts)==3 and rel.parts[:2]==('session','raw') and re.fullmatch(r'[a-f0-9]{64}\.csv',p.name):continue
            raise StoreError('Unexpected file in state; refused to upload')
    def save(self):
        self.open();self.validate_files()
        self.git('config','user.name','github-actions[bot]')
        self.git('config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
        (self.root/'.gitignore').write_text('**/writer.lock\n')
        self.git('add','--all','--','session','deployment.json','.gitignore')
        if (self.root/'status.json').exists():self.git('add','--','status.json')
        if self.git('diff','--cached','--quiet',check=False).returncode:
            self.git('commit','--quiet','-m','Persist GBPUSD PAPER state')
        # Ordinary fast-forward push: stale concurrent writers cannot overwrite state.
        self.git('push','--quiet','origin','HEAD:refs/heads/'+BRANCH)
    def upload_seed(self):
        self.validate_files()
        if (self.root/'.git').exists():raise StoreError('Seed must not already be a Git repository')
        self.git('init','--quiet');self.git('remote','add','origin',self.remote)
        existing=self.git('ls-remote','--exit-code','--heads','origin','refs/heads/'+BRANCH,check=False)
        if existing.returncode!=2:raise StoreError('State branch exists or remote unavailable; seed refused')
        self.save()

def main():
    p=argparse.ArgumentParser();p.add_argument('operation',choices=['restore','save','upload-seed']);p.add_argument('--root',required=True);p.add_argument('--confirm-new-state',action='store_true');a=p.parse_args()
    if a.operation=='upload-seed' and not a.confirm_new_state:p.error('Explicit --confirm-new-state required; never overwrite an existing state branch')
    store=GitStore(a.root)
    {'restore':store.restore,'save':store.save,'upload-seed':store.upload_seed}[a.operation]()
    print('State operation completed:',a.operation)
if __name__=='__main__':main()
