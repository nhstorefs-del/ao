"""Khóa băm bài kiểm và phát hiện một số mẫu làm yếu rõ ràng (stdlib)."""
import argparse
import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


class GuardError(ValueError):
    pass


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted(node.value) + '.' + node.attr
    if isinstance(node, ast.Call):
        return dotted(node.func)
    return ''


def signs(text, suffix):
    result = []
    def add(rule, node, signature=None):
        result.append({'rule': rule, 'line': node.lineno,
                       'signature': signature or ast.dump(node, include_attributes=False)})
    def true(node):
        return isinstance(node, ast.Constant) and node.value is True
    def literal(node):
        return isinstance(node, ast.Constant) or (isinstance(node, (ast.Tuple, ast.List))
                and all(literal(x) for x in node.elts))
    if suffix.lower() == '.py':
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise GuardError(f'Python sai cú pháp tại dòng {exc.lineno}') from exc
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for dec in node.decorator_list:
                    name = dotted(dec)
                    if name.startswith('unittest.skip') or name.startswith('pytest.mark.skip') or name == 'pytest.mark.xfail':
                        add('python-skip-xfail', dec)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                    body = body[1:]
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    add('test-only-pass', body[0], node.name)
            if isinstance(node, ast.Call):
                name = dotted(node.func)
                if name in {'pytest.skip', 'self.skipTest'}:
                    add('python-skip-call', node)
                if name == 'self.assertTrue' and node.args and true(node.args[0]):
                    add('assert-true', node)
                if name == 'self.assertEqual' and len(node.args) >= 2 and literal(node.args[0]) and ast.dump(node.args[0]) == ast.dump(node.args[1]):
                    add('constant-equals-itself', node)
            if isinstance(node, ast.Assert):
                if true(node.test):
                    add('assert-true', node)
                if isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq) and literal(node.test.left) and ast.dump(node.test.left) == ast.dump(node.test.comparators[0]):
                    add('constant-equals-itself', node)
            if isinstance(node, ast.Try) and any(isinstance(x, ast.Assert) or (isinstance(x, ast.Call) and dotted(x.func).startswith('self.assert')) for part in node.body for x in ast.walk(part)):
                for handler in node.handlers:
                    if handler.type is None and len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
                        add('except-pass-around-assert', handler)
    elif suffix.lower() in {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'}:
        # Mask comments and quoted strings, preserving offsets and line numbers.
        masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`',
                        lambda m: re.sub(r'[^\n]', ' ', m.group()), text)
        patterns = [('js-skip-only', r'\.\s*(?:skip|only)\s*\('),
                    ('js-disabled-test', r'\b(?:xit|xdescribe)\s*\('),
                    ('js-assert-true', r'\b(?:expect\s*\(\s*true\s*\)\s*\.\s*toBe\s*\(\s*true\s*\)|assert\s*\(\s*true\s*\))'),
                    ('js-constant-equals-itself', r'\bassert\s*\(\s*(?P<num>\d+)\s*={2,3}\s*(?P=num)\s*\)')]
        for rule, pattern in patterns:
            for match in re.finditer(pattern, masked):
                result.append({'rule': rule, 'line': masked.count('\n', 0, match.start()) + 1,
                               'signature': re.sub(r'\s+', '', match.group())})
    return result


def files(root, globs):
    found = {}
    for pattern in globs:
        if not isinstance(pattern, str) or not pattern or Path(pattern).is_absolute() or '..' in Path(pattern).parts:
            raise GuardError('Mẫu glob phải tương đối, không chứa ..')
        matches = [p for p in root.glob(pattern) if p.is_file()]
        if not matches:
            raise GuardError(f'Mẫu glob không khớp tệp nào: {pattern}')
        for path in matches:
            if not path.resolve().is_relative_to(root):
                raise GuardError('Tệp nằm ngoài root')
            found[path.relative_to(root).as_posix()] = path
    return found


def record(path):
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise GuardError(f'{path.name}: không đọc được UTF-8') from exc
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'signs': signs(text, path.suffix)}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        name = stream.name
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def snapshot(root, globs, out):
    selected = files(root, globs)
    if out.resolve() in [p.resolve() for p in selected.values()]:
        raise GuardError('Snapshot không được ghi đè bài kiểm')
    data = {'version': 1, 'globs': globs, 'files': {name: record(p) for name, p in sorted(selected.items())}}
    atomic_json(out, data)
    return {'status': 'snapshot', 'count': len(selected), 'violations': []}


def check(root, snap, allow_new=False):
    data = json.loads(snap.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not data.get('files') or not data.get('globs'):
        raise GuardError('Snapshot sai định dạng hoặc rỗng')
    previous = data['files']
    for name, value in previous.items():
        if Path(name).is_absolute() or '..' in Path(name).parts or not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256']):
            raise GuardError('Snapshot chứa đường dẫn/băm sai')
        Counter((s['rule'], s['signature']) for s in value['signs'])
    # Missing patterns during check mean deletions, not a falsely clean snapshot.
    selected = {}
    for pattern in data['globs']:
        try:
            selected.update(files(root, [pattern]))
        except GuardError as exc:
            if not str(exc).startswith('Mẫu glob không khớp tệp nào:'):
                raise
    for name in previous:
        path = root / name
        if path.is_file():
            if not path.resolve().is_relative_to(root):
                raise GuardError('Tệp nằm ngoài root')
            selected[name] = path
    violations = []
    def violation(name, line, rule):
        violations.append({'file': name, 'line': line, 'rule': rule})
    for name in sorted(previous.keys() - selected.keys()):
        violation(name, 1, 'tệp bị xóa')
    for name, path in sorted(selected.items()):
        current = record(path)
        old = previous.get(name)
        if old and old['sha256'] != current['sha256']:
            violation(name, 1, 'tệp bị sửa')
        if not old and not allow_new:
            violation(name, 1, 'tệp mới chưa được phép')
        baseline = Counter((s['rule'], s['signature']) for s in old['signs']) if old else Counter()
        for sign in current['signs']:
            key = sign['rule'], sign['signature']
            if baseline[key]:
                baseline[key] -= 1
            else:
                violation(name, sign['line'], sign['rule'])
    return {'status': 'violation' if violations else 'clean', 'count': len(selected), 'violations': violations}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('snapshot', 'check'):
        command = sub.add_parser(name)
        command.add_argument('--root', required=True, type=Path)
        command.add_argument('--json', action='store_true')
        if name == 'snapshot':
            command.add_argument('--glob', required=True, action='append')
            command.add_argument('--out', required=True, type=Path)
        else:
            command.add_argument('--snapshot', required=True, type=Path)
            command.add_argument('--allow-new', action='store_true')
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        if not root.is_dir():
            raise GuardError('Root không phải thư mục')
        result = snapshot(root, args.glob, args.out) if args.command == 'snapshot' else check(root, args.snapshot, args.allow_new)
        code = 3 if result['violations'] else 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result = {'status': 'error', 'error': str(exc), 'violations': []}
        code = 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print({'snapshot': 'Đã khóa bài kiểm', 'clean': 'Sạch', 'violation': 'Có vi phạm', 'error': 'Lỗi dùng'}[result['status']] + ': ' + str(result.get('error', result.get('count', 0))).replace('\n', ' '))
        for item in result['violations'][:27]:
            print(f"{item['file']}:{item['line']}: {item['rule']}")
        if len(result['violations']) > 27:
            print(f"Còn {len(result['violations']) - 27} vi phạm; dùng --json xem đủ.")
    return code


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
