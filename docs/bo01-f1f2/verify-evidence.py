from pathlib import Path
import argparse,json,hashlib,subprocess
R=Path(__file__).resolve().parent
p=argparse.ArgumentParser();p.add_argument('--repo',required=True);a=p.parse_args()
c=json.loads((R/'CANDIDATE.json').read_text());manifest=json.loads((R/'MANIFEST.json').read_text())
for v in manifest['files']:
 f=R/v['file'];assert f.stat().st_size==v['bytes'];assert hashlib.sha256(f.read_bytes()).hexdigest()==v['sha256'],v['file']
head=subprocess.check_output(['git','-C',a.repo,'rev-parse','HEAD'],text=True).strip()
assert subprocess.run(['git','-C',a.repo,'cat-file','-e',c['pin']+'^{commit}'],capture_output=True).returncode==0
for row in json.loads((R/'CALLERS.json').read_text()):
 for key,hashkey in [('source','sourceSHA256'),('wireSource','wireSHA256')]:
  data=subprocess.check_output(['git','-C',a.repo,'show',c['pin']+':'+row[key]])
  assert hashlib.sha256(data).hexdigest()==row[hashkey],row['id']
for row in c['UIpatchPostimages']:
 data=subprocess.check_output(['git','-C',a.repo,'show',c['pin']+':'+row['file']]);assert hashlib.sha256(data).hexdigest()==row['postSHA256']
for row in c['normalCIrowBindings']:
 data=subprocess.check_output(['git','-C',a.repo,'show',c['pin']+':'+row['sourceFile']]);assert hashlib.sha256(data).hexdigest()==row['expectedSHA256'],row['id']
for row in json.loads((R/'REQUIREMENTS.json').read_text()):
 for test in row['nativeTests']:
  data=subprocess.check_output(['git','-C',a.repo,'show',c['pin']+':'+test['source']]);assert hashlib.sha256(data).hexdigest()==test['currentSHA256'],test['nodeID']
print(json.dumps({'integrity':'PASS','product':c['product'],'pinVerified':c['pin'],'workingHEAD':head,'CI':c['CI'],'HOLD':[x['id']for x in c['normalCIrowBindings']if x['state']=='HOLD'],'acceptance':'Not assigned','fixturesStarted':False}))
