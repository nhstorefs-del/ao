"""Approval check to place immediately before an intake-origin external action."""
import os
import sys
from approval_ledger import Ledger

DENIED = 'Việc từ giao diện cần Owner gõ APPROVAL BY OWNER kèm mã REQ-… trong chat'

class ApprovalRequired(RuntimeError):
    code = 7


def require(tool_name, summary):
    rid = os.environ.get('AO_INTAKE_REQUEST')
    if rid is None:
        return True
    try:
        ledger = Ledger()
        item = ledger.find_valid(rid, tool_name)
        if not item:
            raise ApprovalRequired(DENIED)
        ledger.consume(item['id'], tool_name, summary, request_id=rid)
        return True
    except Exception as exc:
        raise ApprovalRequired(DENIED) from exc


def main():
    try:
        require(sys.argv[1], sys.argv[2])
        return 0
    except (IndexError, ApprovalRequired) as exc:
        print(DENIED, file=sys.stderr)
        return 7


if __name__ == '__main__':
    raise SystemExit(main())
