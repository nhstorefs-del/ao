"""Small shared heartbeat writer; each component has its own timestamp."""
import json
import os
from pathlib import Path

import reporting

PATH = reporting.HERE / 'heartbeat.json'


def beat(component, watched=0, clock=reporting.now, path=PATH):
    data = reporting.read(path)
    data[component] = {'at': clock().isoformat(), 'version': '2', 'watched': watched}
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)
    return data
