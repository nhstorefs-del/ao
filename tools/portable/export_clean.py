"""Export repository files without runtime state; Python stdlib only."""
from pathlib import Path
import argparse
import os
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
STATE_DIRS = {'data', 'jobs', 'threads', 'memory', 'burn', '__pycache__', '.git', '.' + 'worker-keys', 'ao-secrets', 'ao-private-pack', 'optional'}
STATE_FILES = {'journal.jsonl', 'queue.json', 'turn_log.jsonl', 'STATE.md', 'STATE.json', 'toolbox.json'}


def excluded(rel):
    return (any(p in STATE_DIRS for p in rel.parts) or rel.name in STATE_FILES
            or rel.name in {'.env', 'auth.json'} or rel.suffix in {'.pyc', '.pyo', '.log', '.pid', '.lock', '.tmp', '.env', '.key'})


def wildmatch(pattern):
    """Gitignore wildcards: * and ? cannot cross a directory separator."""
    out = ''; i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == '\\' and i + 1 < len(pattern):
            i += 1; out += re.escape(pattern[i])
        elif c == '*':
            if pattern[i:i+2] == '**' and (i == 0 or pattern[i-1] == '/') and (i+2 == len(pattern) or pattern[i+2] == '/'):
                i += 1
                if i+1 < len(pattern) and pattern[i+1] == '/':
                    out += '(?:.*/)?'; i += 1
                else: out += '.*'
            else: out += '[^/]*'
        elif c == '?': out += '[^/]'
        elif c == '[':
            end = pattern.find(']', i+1)
            if end < 0: out += r'\['
            else:
                group = pattern[i+1:end]
                if group.startswith('!'): group = '^' + group[1:]
                out += '[' + group.replace('\\', '\\\\') + ']'; i = end
        else: out += re.escape(c)
        i += 1
    return re.compile('^' + out + '$')


def ignore_rules(directory):
    p = directory / '.gitignore'
    if not p.is_file(): return []
    result = []
    for line in p.read_text(encoding='utf-8-sig').splitlines():
        # Unescaped trailing spaces are insignificant in gitignore.
        while line.endswith(' ') and not line.endswith('\\ '): line = line[:-1]
        if not line or line.startswith('#'): continue
        negated = line.startswith('!')
        if negated: line = line[1:]
        only_dir = line.endswith('/')
        if only_dir: line = line[:-1]
        anchored = line.startswith('/')
        if anchored: line = line[1:]
        result.append((wildmatch(line), negated, only_dir, anchored or '/' in line))
    return result


def fallback_files(root):
    """Apply nested .gitignore rules, pruning ignored parents as Git does."""
    def walk(directory, inherited):
        rules = inherited + [(directory, rule) for rule in ignore_rules(directory)]
        for item in sorted(directory.iterdir()):
            rel = item.relative_to(root)
            if excluded(rel): continue
            if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
                raise ValueError('Refusing link: ' + rel.as_posix())
            is_dir = item.is_dir(); ignored = False
            for base, (regex, negated, only_dir, anchored) in rules:
                name = item.relative_to(base).as_posix() if anchored else item.name
                if (not only_dir or is_dir) and regex.fullmatch(name): ignored = not negated
            if ignored: continue
            if is_dir: yield from walk(item, rules)
            elif item.is_file(): yield rel
    yield from walk(root, [])


def repository_files(root):
    if (root / '.git').exists():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                                capture_output=True, check=True,
                                creationflags=0x08000000 if os.name == 'nt' else 0)
        return sorted({Path(os.fsdecode(p)) for p in result.stdout.split(b'\0') if p and not excluded(Path(os.fsdecode(p)))})
    return sorted(fallback_files(root))


def export_clean(root, destination):
    root = root.resolve(); destination = destination.resolve()
    if destination == root or destination.is_relative_to(root):
        raise ValueError('Destination must be outside the source repository')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('Destination already exists and is not empty')
    files = repository_files(root)
    checked = []
    for rel in files:
        source = root / rel
        if not source.exists(): continue  # Tracked files deleted from the working tree.
        if source.is_symlink() or source.resolve() != root / rel or not source.resolve().is_relative_to(root):
            raise ValueError('Refusing link or escaping path: ' + rel.as_posix())
        if not source.is_file(): raise ValueError('Not a regular file: ' + rel.as_posix())
        checked.append((rel, source))
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    for rel, source in checked:
        target = destination / rel; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target); total += target.stat().st_size
    return len(checked), total


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('destination', type=Path)
    args = p.parse_args()
    try: count, size = export_clean(ROOT, args.destination)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        p.exit(1, 'EXPORT_FAILED: ' + str(exc) + '\n')
    print(f'EXPORT_CLEAN: {count} files, {size} bytes')
    return 0


if __name__ == '__main__': raise SystemExit(main())
