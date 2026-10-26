"""Run isolated before/after jobs, measured unittest discovery, or ONE real job."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from window_probe import WindowSampler
from common import hidden_popen_kwargs

ROOT = Path(__file__).resolve().parent
BASE = ROOT / '.hide-windows'
BASE.mkdir(exist_ok=True)
os.environ.update(TEMP=str(BASE), TMP=str(BASE), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
# The measurement launcher itself must not introduce a visible console.
HIDDEN = hidden_popen_kwargs()


def jobs(label, real=False):
    folder = BASE / label
    folder.mkdir()
    brief = folder / 'brief.md'
    output = folder / 'output.txt'
    brief.write_text(f'Create only this file: {output}\nContent must be exactly AO_HIDE_WINDOWS_OK. Do not inspect other files, use network, delegate, or run tests. Use one local file-write command and finish immediately.\n', encoding='utf-8')
    config = folder / 'workers.json'
    if not real:
        worker = folder / 'fake.py'
        worker.write_text('import time\nfrom pathlib import Path\nprint("fake started",flush=True)\ntime.sleep(5)\nPath(__file__).with_name("output.txt").write_text("AO_HIDE_WINDOWS_OK")\nprint("DONE",flush=True)\n', encoding='utf-8')
        config.write_text(json.dumps({'fake': {'enabled': True, 'command': [sys.executable, str(worker)], 'brief_mode': 'argument', 'cwd': str(folder), 'env': {}, 'key_file': None, 'stall_seconds': 30, 'done_markers': [], 'error_patterns': []}}), encoding='utf-8')
    states = []
    with WindowSampler() as sampler:
        for i in range(1 if real else 3):
            ident = f'HIDE{i}'
            command = [sys.executable, str(ROOT/'ao_run.py'), '--queue', str(folder/'queue.json'), '--journal', str(folder/'journal.jsonl'), '--jobs', str(folder/'jobs')]
            if not real:
                command += ['--workers', str(config)]
            result = subprocess.run(command + ['start', '--id', ident, '--title', 'hide windows probe', '--workflow', 'test', '--worker', 'codex-main' if real else 'fake', '--brief', str(brief), '--signal', 'file:'+str(output), '--max-retries', '0'], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', **HIDDEN)
            (folder/f'start{i}.log').write_text(result.stdout+result.stderr, encoding='utf-8')
            if result.returncode:
                raise RuntimeError(result.stderr)
            deadline = time.monotonic()+ (180 if real else 30)
            while time.monotonic() < deadline:
                status = folder/'jobs'/ident/'status.json'
                try:
                    state = json.loads(status.read_text(encoding='utf-8')).get('state')
                except (OSError, ValueError):
                    state = None
                if state in ('DONE_NEEDS_REVIEW', 'GAVE_UP', 'SUPERVISOR_ERROR', 'STOPPED'):
                    break
                time.sleep(.1)
            else:
                subprocess.run(command+['stop','--id',ident], cwd=ROOT, capture_output=True, **HIDDEN)
                raise RuntimeError('Probe timed out; stop requested')
            states.append(state)
    row = {**sampler.result(), 'states': states, 'content': output.read_text() if output.exists() else None}
    (BASE/f'{label}.json').write_text(json.dumps(row, indent=2), encoding='utf-8')
    print(json.dumps(row), flush=True)
    if row['new_windows'] and label != 'before':
        raise SystemExit('Visible windows detected')
    if any(state != 'DONE_NEEDS_REVIEW' for state in states) or row['content'] != 'AO_HIDE_WINDOWS_OK':
        raise SystemExit('Probe result failed')


def tests(label):
    # Redirect toolbox data so the suite cannot modify real notes.
    code = '''import sys, unittest, tempfile, shutil
from pathlib import Path
root=Path(sys.argv[1]); sys.path.insert(0,str(root))
import ao_toolbox
notes=ao_toolbox.DATA/'toolbox_notes.json'
ao_toolbox.DATA=Path(tempfile.mkdtemp(prefix='toolbox-'))
if notes.is_file(): shutil.copyfile(notes,ao_toolbox.DATA/notes.name)
suite=unittest.defaultTestLoader.discover(str(root/'tests'))
result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
'''
    with WindowSampler() as sampler:
        result = subprocess.run([sys.executable, '-c', code, str(ROOT)], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', **HIDDEN)
    (BASE/f'{label}.log').write_text(result.stdout+result.stderr, encoding='utf-8')
    row = {**sampler.result(), 'returncode': result.returncode, 'summary': result.stderr[-500:]}
    (BASE/f'{label}.json').write_text(json.dumps(row, indent=2), encoding='utf-8')
    print(json.dumps(row), flush=True)
    if result.returncode or row['new_windows']:
        raise SystemExit(1)


if __name__ == '__main__':
    if sys.argv[1] == 'tests':
        tests(sys.argv[2])
    else:
        jobs(sys.argv[2], sys.argv[1] == 'real')
