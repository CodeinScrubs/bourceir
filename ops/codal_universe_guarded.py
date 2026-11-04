"""Explicitly guarded future CODAL collection; no modification of old archives."""

import argparse
import json
import uuid

import requests

from config import settings
from core.timeutils import now_tehran_iso
from data_collector.codal import CodalProvider
from data_collector.codal_gate import CodalGate, GatedSession, import_diagnostic, record_manual_review, source_status
from ops.codal_universe import collect, review
from ops.report_files import write_private_report


def guarded_collect(folder, *, max_new=1):
    # Review first; an invalid archive never triggers a source request.
    original = review(folder)
    before = source_status()
    run = dict(version='guarded-codal-universe-run-v1', started_at=now_tehran_iso(), folder=str(folder),
               gate_before=before, gate_after=None, source_attempted=False, transport=None, result=None)
    if before['blocked']:
        run['result'] = 'source_blocked'
    elif original['stop_reason']:
        run['result'] = 'archive_stopped'
    else:
        gate = CodalGate()
        provider = CodalProvider(session=GatedSession(requests.Session(), gate))
        try:
            result = collect(folder, provider, max_new=max_new)
            if (result['stop_reason'] == 'verification_required'
                    and (gate.last_evidence or {}).get('status') != 'verification_required'):
                gate.preserve_verification_required()
            if (result['stop_reason'] == 'invalid_response'
                    and (gate.last_evidence or {}).get('status') == 'response_received'):
                gate.preserve_invalid_response()
            run['result'] = result['stop_reason'] or 'budget_exhausted'
            run['transport'] = gate.last_evidence
            run['source_attempted'] = bool(gate.last_evidence and gate.last_evidence['status'] != 'blocked')
        finally:
            provider.session.close()
    run['gate_after'] = source_status()
    run['completed_at'] = now_tehran_iso()
    path = settings.DATA_DIR/'private/codal-gated-runs'/('RUN-'+uuid.uuid4().hex+'.json')
    write_private_report(path, run)
    return dict(receipt=str(path), **run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    load = sub.add_parser('import-diagnostic')
    load.add_argument('--receipt', required=True)
    manual = sub.add_parser('manual-review')
    manual.add_argument('--not-before', required=True, help='timezone-aware time approved by the operator')
    manual.add_argument('--review-basis', required=True, help='what source evidence and limits were checked')
    run = sub.add_parser('collect')
    run.add_argument('--study', required=True)
    run.add_argument('--max-new', type=int, default=1)
    args = parser.parse_args(argv)
    if args.command == 'status':
        result = source_status()
    elif args.command == 'import-diagnostic':
        result = import_diagnostic(args.receipt)
    elif args.command == 'manual-review':
        result = record_manual_review(not_before=args.not_before, review_basis=args.review_basis)
    else:
        result = guarded_collect(args.study, max_new=args.max_new)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
