"""Deterministic local regression runner; never uses registered live workers."""
from concurrent.futures import ThreadPoolExecutor  # Eager stdlib queue before AO queue.py.
import argparse
import io
import json
import sys
import time
import unittest
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--suite',choices=['gate','dispatcher','journal'],required=True);p.add_argument('--output',required=True);a=p.parse_args()
base=Path(__file__).resolve().parents[1]; journal=base.parent/'ao-journal'
if a.suite=='journal':
    sys.path.insert(0,str(journal));suite=unittest.defaultTestLoader.discover(str(journal/'tests'))
else:
    sys.path.insert(0,str(base));suite=unittest.defaultTestLoader.discover(str(base/'tests'),pattern='test_delegation_gate.py' if a.suite=='gate' else 'test_ao_run.py')
stream=io.StringIO();started=time.time();result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
value={'suite':a.suite,'tests_run':result.testsRun,'passed':result.wasSuccessful(),'failures':len(result.failures),'errors':len(result.errors),'elapsed_seconds':round(time.time()-started,3),'log':stream.getvalue()}
output=Path(a.output);output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(value,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in value.items() if k!='log'}));raise SystemExit(not result.wasSuccessful())
