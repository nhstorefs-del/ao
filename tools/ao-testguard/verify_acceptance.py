"""Chạy nghiệm thu thật, giữ mọi báo cáo mới trong ao-testguard."""
import contextlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import testguard


def main():
    here = Path(__file__).resolve().parent
    repo = here.parents[1]
    evidence = here / 'evidence'
    evidence.mkdir(exist_ok=True)
    report = repo / 'tools' / 'ao-report'
    snap = evidence / 'ao_report_snapshot.json'
    testguard.snapshot(report, ['**/test_*.py'], snap)
    clean = testguard.check(report, snap)
    assert clean['status'] == 'clean', clean
    testguard.atomic_json(evidence / 'ao_report_check.json', clean)
    print(f"ao-report snapshot/check: {clean['count']} tệp, sạch")

    with tempfile.TemporaryDirectory(prefix='testguard-copy-') as temporary:
        copied = Path(temporary)
        for name in json.loads(snap.read_text(encoding='utf-8'))['files']:
            destination = copied / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(report / name, destination)
        first = next(iter(json.loads(snap.read_text(encoding='utf-8'))['files']))
        with (copied / first).open('a', encoding='utf-8') as stream:
            stream.write('\nassert True\n')
        dirty = testguard.check(copied, snap)
        assert dirty['status'] == 'violation'
        assert any(v['rule'] == 'assert-true' for v in dirty['violations'])
        testguard.atomic_json(evidence / 'temporary_copy_check.json', dirty)
        print('Bản sao tạm thêm assert True: bắt đúng')

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(report))
    spec = importlib.util.spec_from_file_location('testguard_independent_runner', report / 'run_independent.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Only redirect report destination; run original main/evaluate/adapter unchanged.
    module.OUTPUT = evidence / 'independent_results.json'
    log_path = evidence / 'independent_stdout.txt'
    with log_path.open('w', encoding='utf-8') as log:
        with contextlib.redirect_stdout(log):
            code = module.main()
    print(log_path.read_text(encoding='utf-8').splitlines()[-1])
    if code:
        return code
    executable = shutil.which('sha256sum')
    if not executable:
        raise RuntimeError('Không tìm thấy sha256sum')
    hashed = subprocess.run([executable, '-c', 'independent_tests.SHA256'],
                            cwd=repo / 'projects' / 'AO_QA_20261011', capture_output=True, text=True)
    (evidence / 'hashes.txt').write_text(hashed.stdout + hashed.stderr, encoding='utf-8')
    print(hashed.stdout, end='')
    return hashed.returncode


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
