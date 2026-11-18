from decimal import Decimal

def balance(state):
    accepted=len(state['rows']);isolated=len(state['quarantine'])
    accounted=accepted+isolated+state['blank_count']+state['header_count']+state['duplicate_count']
    original=sum((Decimal(str(s.get('original_premium','0'))) for s in state.get('segments',[])),Decimal(0))
    corrected=sum((Decimal(str(s.get('premium','0'))) for s in state.get('segments',[])),Decimal(0))
    age=state.get('age_policy',{})
    bills_kept=len(state.get('original_bills',[]))>0
    revisions=[r for r in state.get('age_revisions',[]) if r['status']!='SUPERSEDED']
    rejected=len(state.get('age_rejections',[]))
    return {'input':state['input_count'],'accepted':accepted,'isolated':isolated,
        'blank':state['blank_count'],'headers':state['header_count'],'duplicates':state['duplicate_count'],
        'accounted':accounted,'balanced':accounted==state['input_count'],
        'age_mode':age.get('mode',''),'age_status':age.get('status',''),
        'age_applied':age.get('applied',0),'age_held':age.get('held',0),
        'age_rejected':rejected,'age_pending':len(revisions),
        'age_duplicates':state.get('age_duplicate_count',0),
        'original_bills':len(state.get('original_bills',[])),
        'original_total':str(original),'corrected_total':str(corrected),
        'age_delta':str(corrected-original),
        'bills_retained':bills_kept,
        'business':state.get('business',{})}
