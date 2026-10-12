#!/usr/bin/env python3
"""Sync real Prelint check evidence for this checkout; no GitHub key is stored."""
import json, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import routing

def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT,text=True).strip()
def api(path):return json.loads(subprocess.check_output(['gh','api',path],cwd=ROOT,text=True))

def main():
    head=git('rev-parse','HEAD')
    if git('diff','HEAD','--',*routing.REVIEW_FILES):raise SystemExit('Commit the routing policy before syncing a review.')
    checks=api(f'repos/{routing.REPOSITORY}/commits/{head}/check-runs?per_page=100')['check_runs']
    matches=[c for c in checks if c.get('app',{}).get('slug')=='prelint' and c.get('head_sha')==head]
    if not matches:
        print('No Prelint check on this commit yet. Connect only 4horizons/vidset in Prelint, then review its pull request.')
        return 2
    check=max(matches,key=lambda c:c['id'])
    receipt={'repository':routing.REPOSITORY,'head_sha':head,'policy_sha256':routing.policy_hash(),
             'check_id':check['id'],'app_slug':'prelint','status':check['status'],'conclusion':check.get('conclusion'),
             'details_url':check.get('details_url')}
    routing.RECEIPT.parent.mkdir(exist_ok=True)
    routing.RECEIPT.write_text(json.dumps(receipt,indent=2))
    print(json.dumps({'status':receipt['status'],'conclusion':receipt['conclusion'],'check_id':receipt['check_id']}))
    return 0
if __name__=='__main__':raise SystemExit(main())
