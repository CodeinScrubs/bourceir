"""فهرست ادعاهای تعداد سهام و گسست پیوندها؛ تاریخچه کامل یا ارزش‌گذاری سبد نیست."""

import argparse
import hashlib
import uuid
from collections import Counter
from pathlib import Path

from config import settings
from core.share_changes import FALSE_FLAGS
from core.timeutils import now_tehran_iso
from ops.report_files import private_report_path, read_private_report, write_private_report
from ops.share_changes import review_share_changes

VERSION='share-change-catalog-v1'
CODE_FILES=('ops/share_change_catalog.py','ops/share_changes.py','core/share_changes.py',
            'core/timeutils.py','core/text.py','ops/report_files.py')


def build_share_catalog(studies):
    if not isinstance(studies,list) or not 1<=len(studies)<=100:
        raise ValueError('یک تا صد پرونده صریح لازم است')
    sources,issuers,events,seen_codes,seen_symbols=[],[],[],set(),set()
    windows=set()
    for source in studies:
        folder=Path(source).resolve()
        review=review_share_changes(folder)
        plan=read_private_report(folder/'plan.json')
        windows.add((plan['start'],plan['end_exclusive'],plan['cutoff']))
        if len(windows)>1:
            raise ValueError('پنجره و مرز پرونده‌های ادغام‌شده باید برابر باشند')
        sources.append(dict(study_id=folder.name,
            manifest_sha256=hashlib.sha256((folder/'manifest.json').read_bytes()).hexdigest(),
            capture_code_matches_current=review['capture_code_matches_current'],
            semantic_code_matches_current=review['semantic_code_matches_current'],comparison=review['comparison']))
        for case in review['cases']:
            request=case['request'];code,symbol=request['ins_code'],request['symbol_fa']
            if code in seen_codes or symbol in seen_symbols:
                raise ValueError('نماد/ناشر تکراری در فهرست، مخرج دوگانه می‌سازد')
            seen_codes.add(code);seen_symbols.add(symbol)
            assessment=case['assessment'];observed=assessment.get('events',[])
            gaps=[]
            for old,new in zip(observed,observed[1:]):
                if old['source_new_shares']!=new['source_old_shares']:
                    gaps.append(dict(previous_date=old['source_change_date'],next_date=new['source_change_date'],
                        previous_new_shares=old['source_new_shares'],next_old_shares=new['source_old_shares']))
            chain=('discontinuous_observed' if gaps else 'no_break_observed' if len(observed)>1 else 'not_assessable')
            issuers.append(dict(symbol_fa=symbol,ins_code=code,source_study_id=folder.name,
                source_status=assessment['status'],observed_groups=len(observed),
                exact_duplicate_rows=assessment.get('duplicate_rows',0),
                conflicting_dates=assessment.get('conflicting_dates',[]),
                chain_status=chain,observed_chain_breaks=gaps,
                sealed_rows_excluded=assessment.get('excluded_sealed_rows'),
                projection_eligible=False,**dict.fromkeys(FALSE_FLAGS,False)))
            events.extend(dict(event,symbol_fa=symbol,ins_code=code,source_study_id=folder.name,
                source_status=assessment['status']) for event in observed)
    start,end,cutoff=next(iter(windows))
    if any(not start<=e['source_change_date']<end<=cutoff for e in events):
        raise ValueError('رخداد از پنجره برنامه بیرون است')
    groups=Counter('increase' if e['source_new_shares']>e['source_old_shares'] else
        'decrease' if e['source_new_shares']<e['source_old_shares'] else 'unchanged' for e in events)
    return dict(version=VERSION,generated_at=now_tehran_iso(),start=start,end_exclusive=end,cutoff=cutoff,
        code_sha256={n:hashlib.sha256((settings.BASE_DIR/n).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for n in CODE_FILES},
        sources=sources,issuers=issuers,events=events,issuer_count=len(issuers),observed_group_count=len(events),
        source_status_counts=dict(Counter(i['source_status'] for i in issuers)),observed_change_types=dict(groups),
        exact_duplicate_rows=sum(i['exact_duplicate_rows'] for i in issuers),
        issuers_with_chain_breaks=sum(bool(i['observed_chain_breaks']) for i in issuers),
        observed_chain_break_count=sum(len(i['observed_chain_breaks']) for i in issuers),
        projection_eligible=False,independent_strategy_test=False,economic_valuation_verified=False,
        external_authenticity_verified=False,historical_instrument_mapping_verified=False,
        database_read=False,network_requested=False,**dict.fromkeys(FALSE_FLAGS,False))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--studies',nargs='+',required=True)
    parser.add_argument('--json')
    args=parser.parse_args(argv)
    target=Path(args.json) if args.json else settings.DATA_DIR/'private/share-change-catalog'/('CATALOG-'+uuid.uuid4().hex+'.json')
    target=private_report_path(target,forbidden_paths=(settings.DB_PATH,))
    if any(target.is_relative_to(Path(source).resolve()) for source in args.studies):
        raise ValueError('خروجی فهرست نمی‌تواند داخل پرونده ورودی باشد')
    result=build_share_catalog(args.studies)
    write_private_report(target,result)
    print(str(target))


if __name__=='__main__':main()
