
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,math
from pathlib import Path
from fractions import Fraction
from queue_replay.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['extra']=1;self.bad(d)

    def test_schedule(self):
        r=analyze(sample());self.assertEqual([(j['id'],j['server'],j['start'],j['finish']) for j in r['jobs']],[('A',0,0,5),('B',1,0,2),('C',1,2,5),('D',0,5,6)]);self.assertEqual((r['mean_wait'],r['max_wait']),('1',3));self.assertEqual([s['utilization'] for s in r['servers']],['1','5/6'])
    def test_order(self):
        d=sample();d['jobs'].reverse();self.assertEqual(analyze(d),analyze(sample()))
    def test_empty(self):
        r=analyze({'servers':2,'jobs':[]});self.assertIsNone(r['mean_wait']);self.assertIsNone(r['servers'][0]['utilization'])
    def test_zero_service(self):
        d={'servers':1,'jobs':[{'id':'a','arrival':10,'service':0}]};r=analyze(d);self.assertEqual(r['jobs'][0]['wait'],0);self.assertIsNone(r['servers'][0]['utilization'])
    def test_initial_idle_excluded(self):
        d={'servers':1,'jobs':[{'id':'a','arrival':100,'service':10}]};r=analyze(d);self.assertEqual(r['servers'][0]['utilization'],'1')
    def test_duplicate(self):
        d=sample();d['jobs'].append(d['jobs'][0]);self.bad(d)
    def test_serial_oracle(self):
        rng=random.Random(17)
        for _ in range(20):
            jobs=[{'id':str(i),'arrival':rng.randrange(20),'service':rng.randrange(10)} for i in range(30)];r=analyze({'servers':1,'jobs':jobs});finish=0
            for source,row in zip(sorted(jobs,key=lambda j:(j['arrival'],j['id'])),r['jobs']):
                start=max(finish,source['arrival']);self.assertEqual(row['start'],start);finish=start+source['service'];self.assertEqual(row['finish'],finish)

class Command(unittest.TestCase):
    def call(self,src,out):
        return subprocess.run([sys.executable,'-m','queue_replay.cli','--source',str(src),'--output',str(out)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
