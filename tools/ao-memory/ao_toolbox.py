"""Bảng công cụ AO: chỉ đọc nguồn ngoài ao-memory, chỉ ghi data/toolbox.json."""
import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / 'data'
LIB = Path(os.environ.get('AO_REPO_LIBRARY', str(Path(__file__).resolve().parents[2] / 'repo-library')))
TOOLS = HERE.parent
ROOT = TOOLS.parent
SYNCED = Path.home() / '.claude' / 'skills' / 'synced'
SCHEDULED = Path.home() / '.claude' / 'scheduled-tasks'

def norm(value):
    s = unicodedata.normalize('NFD', str(value).casefold().replace('đ', 'd'))
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in s if unicodedata.category(c) != 'Mn')).strip()

def short(value, limit=160):
    return re.sub(r'\s+', ' ', str(value)).strip()[:limit]

def read(path):
    return path.read_text(encoding='utf-8-sig')

def readme_description(directory):
    p = next((directory / name for name in ('README.md', 'readme.md', 'README.MD') if (directory / name).is_file()), None)
    if p is None:
        return 'chưa có mô tả'
    for line in read(p).splitlines():
        line = line.strip().strip('#').strip()
        if line and not line.startswith(('<', '![', '[!', '---', '```', '|', 'http')):
            return short(line)
    return 'chưa có mô tả'

def item(name, kind, path, description='', **extra):
    return {'id': norm(name).replace(' ', '-'), 'ten': name, 'loai': kind,
            'mo_ta': short(description or 'chưa có mô tả'), 'duong_dan': str(path), **extra}

def profile_excerpt(content, repo_name):
    lines = content.splitlines()
    headings = [i for i, line in enumerate(lines) if line.startswith('### ') and norm(repo_name) in norm(line)]
    if headings:
        start = headings[0] + 1
        end = next((i for i in range(start, len(lines)) if lines[i].startswith(('## ', '### '))), len(lines))
        return short(' '.join(x.strip('- ').strip() for x in lines[start:end] if x.strip()), 160)
    useful = [line.strip() for line in lines if line.strip().startswith(('- Giay phep:', '- Quyen truy cap:', '- Chi phi:', '- Rui ro:', '- Danh gia:'))]
    return short(' '.join(useful), 160)

def scan(repo_lib=LIB, tools_dir=TOOLS, synced=SYNCED, scheduled=SCHEDULED,
         root=ROOT, workers_path=None):
    errors, items = [], []
    def safe(label, fn):
        try: return fn()
        except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
            errors.append(f'không đọc được: {label} ({type(exc).__name__}: {short(exc, 100)})')
            return None
    catalog = safe(str(repo_lib / 'catalog.json'), lambda: json.loads(read(repo_lib / 'catalog.json')))
    records = {norm(x.get('local_path', '').split('/')[-1]): x for x in (catalog or {}).get('repositories', [])}
    library_readme = safe(str(repo_lib / 'README.md'), lambda: read(repo_lib / 'README.md')) or ''
    entry_dir = repo_lib / 'entries'
    profiles = {}
    if not entry_dir.is_dir(): errors.append(f'không đọc được: {entry_dir} (không tồn tại)')
    for p in safe(str(entry_dir), lambda: sorted(entry_dir.glob('*.md'))) or []:
        content = safe(str(p), lambda p=p: read(p))
        if content is not None: profiles[p.name] = content
    repos_dir = repo_lib / 'repos'
    for d in safe(str(repos_dir), lambda: sorted(p for p in repos_dir.iterdir() if p.is_dir())) or []:
        record = records.get(norm(d.name), {})
        desc = safe(str(d / 'README.md'), lambda d=d: readme_description(d)) or 'chưa có mô tả'
        row = item(record.get('name', d.name), 'repo', d, record.get('summary') or desc,
                   ho_so='có hồ sơ' if record else 'chưa có hồ sơ',
                   giay_phep=record.get('license', 'chưa rõ'), tu_khoa=record.get('tags', []))
        if record:
            row['id'] = norm(d.name).replace(' ', '-')
        relevant = [p for p, text in profiles.items() if norm(d.name) in norm(p + ' ' + text)]
        if relevant:
            row['ho_so_nguon'] = [str(entry_dir / p) for p in relevant]
            excerpt = next((profile_excerpt(profiles[p], d.name) for p in relevant if profile_excerpt(profiles[p], d.name)), '')
            if excerpt: row['ho_so_tom_tat'] = excerpt
        if not record and not relevant: row['ho_so'] = 'chưa có hồ sơ'
        items.append(row)
    # README table may describe repos absent from catalog; its purpose is searchable context.
    for row in items:
        if row['loai'] == 'repo' and row['mo_ta'] == 'chưa có mô tả':
            match = next((line for line in library_readme.splitlines() if f'`repos/{Path(row["duong_dan"]).name}`' in line), '')
            cells = [x.strip() for x in match.split('|')]
            if len(cells) > 2: row['mo_ta'] = short(cells[2])
    for base in (synced, scheduled):
        if not base.is_dir(): errors.append(f'không đọc được: {base} (không tồn tại)')
        paths = safe(str(base), lambda base=base: sorted(base.glob('*/*/SKILL.md' if base == synced else '*/SKILL.md'))) or []
        for p in paths:
            content = safe(str(p), lambda p=p: read(p))
            if content is None: continue
            head = content.split('---', 2)[1] if content.startswith('---') and content.count('---') >= 2 else content[:1000]
            fields = dict(re.findall(r'^\s*(name|description):\s*(.+)$', head, re.M))
            name = fields.get('name', p.parent.name).strip('"\' ')
            desc = fields.get('description', '').strip('"\' ')
            items.append(item(name, 'skill', p, desc, nguon='lịch' if base == scheduled else 'đồng bộ'))
    for d in safe(str(tools_dir), lambda: sorted(p for p in tools_dir.iterdir() if p.is_dir() and not p.name.startswith('.'))) or []:
        desc = safe(str(d / 'README.md'), lambda d=d: readme_description(d)) or 'chưa có mô tả'
        items.append(item(d.name, 'tools', d, desc))
    research = root / 'docs' / 'research'
    for p in safe(str(research), lambda: sorted(p for p in research.iterdir() if p.is_file())) or []:
        items.append(item(p.stem, 'tools', p, 'Báo cáo nghiên cứu: ' + p.name, nguon='báo cáo'))
    for d in (repo_lib / 'sandbox', repo_lib / 'runtime-tests'):
        if d.is_dir(): items.append(item(d.name, 'tools', d, 'Môi trường kiểm tra cục bộ trong thư viện repo'))
        else: errors.append(f'không đọc được: {d} (không tồn tại)')
    workers_path = workers_path or HERE / 'workers.json'
    if not workers_path.is_file() and (HERE / 'workers.example.json').is_file(): workers_path = HERE / 'workers.example.json'
    workers = safe(str(workers_path), lambda: json.loads(read(workers_path))) or {}
    for name, config in workers.items():
        note = config.get('reason') or config.get('note') or (f"{config.get('kind', 'worker')}; " + ('đang bật' if config.get('enabled') else 'đã tắt'))
        items.append(item(name, 'worker', workers_path, note, trang_thai='đang bật' if config.get('enabled') else 'đã tắt'))
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg: items.append(item('ffmpeg', 'tools', ffmpeg, 'Xử lý, ghép, chuyển mã video và âm thanh'))
    return items, errors

def merge_notes(items, notes):
    by_key = {(x['loai'], norm(x['id'])): x for x in items}
    for note in notes:
        key = (note['loai'], norm(note['id']))
        target = by_key.get(key)
        if target is None:
            target = item(note['id'], note['loai'], note['duong_dan'], 'Chưa xác minh trong nguồn quét')
            target['nguon'] = 'chú thích tay; chưa xác minh'
            items.append(target)
            by_key[key] = target
        for field in ('dung_khi', 'tu_khoa', 'canh_bao', 'trang_thai'):
            target[field] = note[field]
        if note.get('ten'): target['ten'] = note['ten']
        if not target.get('duong_dan'): target['duong_dan'] = note['duong_dan']
    return items

def build(**scan_args):
    items, errors = scan(**scan_args)
    notes_path = DATA / 'toolbox_notes.json'
    try: notes = json.loads(read(notes_path))
    except (OSError, ValueError, UnicodeError) as exc:
        errors.append(f'không đọc được: {notes_path} ({type(exc).__name__}: {short(exc, 100)})')
        notes = []
    items = merge_notes(items, notes)
    result = {'cong_cu': items, 'loi_nguon': errors, 'so_luong': {k: sum(x['loai'] == k for x in items) for k in ('repo','skill','tools','worker')}}
    DATA.mkdir(parents=True, exist_ok=True)
    target = DATA / 'toolbox.json'
    temp = DATA / 'toolbox.json.tmp'
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(target)
    return result

def load():
    target = DATA / 'toolbox.json'
    return json.loads(read(target)) if target.exists() else build()

def search(items, query, limit=6):
    words = set(norm(query).split())
    def score(x):
        name = norm(x['ten'] + ' ' + x['id'])
        keys = norm(' '.join(x.get('tu_khoa', [])))
        desc = norm(x.get('dung_khi', '') + ' ' + x.get('mo_ta', ''))
        matched = words & set((name + ' ' + keys + ' ' + desc).split())
        if len(words) > 1 and len(matched) < min(len(words), 3): return 0
        n = 8 * len(words & set(name.split())) + 5 * len(words & set(keys.split())) + 2 * len(words & set(desc.split()))
        if norm(query) in name: n += 12
        if norm(query) in keys: n += 8
        if x.get('trang_thai') == 'khong_nen_dung': n -= 100
        if x.get('nguon') == 'chú thích tay; chưa xác minh': n -= 8
        return n
    ranked = sorted(((score(x), x) for x in items), key=lambda z: (-z[0], z[1]['ten'].casefold()))
    return [x for n, x in ranked if n > 0][:limit]

def display(x):
    return f"{x['ten']} · {x['loai']} · {x.get('dung_khi') or x['mo_ta']} · {x.get('canh_bao') or 'Không có cảnh báo riêng'} · {x['duong_dan']}"

GROUPS = [
    ('Lấy dữ liệu web', ('browserskill','agent-reach','chrome-browser','built-in-browser')),
    ('Video/âm thanh', ('ffmpeg','whisper-small')),
    ('Worker & điều phối', ('ao-memory','codex-main','codex-2','deepseek-nvidia','deepseek-paid')),
    ('Tài liệu/Excel/PDF', ('xlsx','docx','pdf','pptx','docs','google-workspace')),
    ('Quản trị/sổ việc', ('ao-journal','ao-memory')),
]

def markdown(result):
    by_id = {x['id']: x for x in result['cong_cu']}
    lines = []
    used = set()
    for heading, ids in GROUPS:
        lines.append(f'### {heading}')
        for ident in ids:
            x = by_id.get(ident)
            if x is None or (heading == 'Repo chưa dùng' and x.get('trang_thai') == 'da_dung'): continue
            if ident in used: continue
            used.add(ident)
            suffix = ' [không nên dùng]' if x.get('trang_thai') == 'khong_nen_dung' else ' [chưa xác minh]' if x.get('nguon') == 'chú thích tay; chưa xác minh' else ''
            lines.append(f"- {x['ten']}: {short(x.get('dung_khi') or x['mo_ta'], 115)}" + suffix)
        lines.append('')
    remaining = [x for x in result['cong_cu'] if x['id'] not in used]
    lines.append(f"Tra thêm {len(remaining)} công cụ: `python ao_toolbox.py find \"<việc>\"` hoặc `list`. Chú thích tay: data/toolbox_notes.json.")
    for e in result['loi_nguon'][:5]: lines.append('- ' + e)
    return '\n'.join(lines).strip()

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('build'); find = sub.add_parser('find'); find.add_argument('query')
    listing = sub.add_parser('list'); listing.add_argument('--loai', choices=['repo','skill','tools','worker'])
    sub.add_parser('markdown')
    args = parser.parse_args()
    result = build() if args.cmd == 'build' else load()
    if args.cmd == 'build':
        print('Số công cụ:', result['so_luong'])
        for e in result['loi_nguon']: print(e)
    elif args.cmd == 'find':
        for x in search(result['cong_cu'], args.query): print(display(x))
    elif args.cmd == 'list':
        for x in result['cong_cu']:
            if args.loai is None or x['loai'] == args.loai: print(display(x))
    else: print(markdown(result))

if __name__ == '__main__': main()
