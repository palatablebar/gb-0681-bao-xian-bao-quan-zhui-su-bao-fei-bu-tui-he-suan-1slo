from datetime import date
from decimal import Decimal,ROUND_DOWN,ROUND_HALF_UP

def allocate(values):
    values=[Decimal(str(v)) for v in values]
    total=sum(values,Decimal(0)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    floors=[v.quantize(Decimal('.01'),rounding=ROUND_DOWN) for v in values]
    residual=int((total-sum(floors))/Decimal('.01'))
    ranks=sorted(range(len(values)),key=lambda i:(-(values[i]-floors[i]),i))
    for i in ranks[:residual]:floors[i]+=Decimal('.01')
    return floors

def _days(a,b):
    return max(0,(date.fromisoformat(b)-date.fromisoformat(a)).days)

def attribute(original,segments):
    """把每期原账单按天分到最终期次；同一原账单的分配合计严格等于原到分金额。"""
    shares={}
    for bill in original:
        pieces=[s for s in segments if s['start']<bill['end'] and s['end']>bill['start']]
        if not pieces:continue
        days=[_days(max(s['start'],bill['start']),min(s['end'],bill['end'])) for s in pieces]
        total=sum(days)
        if total<=0:continue
        premium=Decimal(str(bill.get('original_premium','0')))
        parts=allocate([premium*d/total for d in days])
        for piece,part,span in zip(pieces,parts,days):
            shares[piece['bill_id']]={
                'original_bill_id':bill['bill_id'],'original_premium':str(part),
                'original_days':span,'original_start':bill['start'],
                'original_end':bill['end'],'original_annual':bill['annual'],
                'original_source':bill.get('source',''),'original_line':bill.get('line'),
                'original_bill_stage':bill.get('bill_stage','ISSUED')}
    return shares

def apply(state):
    originals=state.get('original_bills') or []
    calculations=[];total=Decimal(0);paidtotal=Decimal(0)
    for p in state['policies']:
        segments=[s for s in state['segments'] if s['id']==p['id']]
        original=[s for s in originals if s['id']==p['id']]
        for bill,value in zip(original,allocate([s['exact'] for s in original])):
            bill['original_premium']=str(value)
        shares=attribute(original,segments)
        for s in segments:s.update(shares.get(s['bill_id'],{}))
        # 已寄出的原账单不再改数：沿用原到分金额，只有更正后的期次按新费率分配。
        issued=[s for s in segments if s.get('bill_stage')=='ISSUED']
        corrected=[s for s in segments if s.get('bill_stage')!='ISSUED']
        for s in issued:s['premium']=s.get('original_premium',s.get('exact','0'))
        for s,value in zip(corrected,allocate([s['exact'] for s in corrected])):
            s['premium']=str(value)
        expected=sum((Decimal(str(s['premium'])) for s in segments),Decimal(0))
        paid=sum((Decimal(str(r['paid'])) for r in state['rows']
                  if r['policy_id']==p['id'] and r.get('kind')!='age'
                  and str(r.get('paid','')).strip() not in ('','-','N/A','NULL','未知')),Decimal(0))
        original_expected=sum((Decimal(str(s.get('original_premium','0'))) for s in segments),Decimal(0))
        revision=p.get('age_revision') or {}
        applied=bool(revision.get('applied'))
        age_status=revision.get('status','NONE')
        if revision.get('held_by_batch'):age_status='HELD'
        delta=expected-original_expected if applied else Decimal(0)
        calculations.append({
            'id':p['id'],'line':p['base']['_line'],
            'code':'SUPPLEMENT' if expected>paid else 'REFUND' if expected<paid else 'EVEN',
            'expected':str(expected),'paid':str(paid),'difference':str(expected-paid),
            'original_expected':str(original_expected),
            'original_difference':str(original_expected-paid),
            'age_delta':str(delta),'age_status':age_status,
            'age_mode':revision.get('selected_mode',''),'basis':revision.get('basis',''),
            'correction_id':revision.get('correction_id',''),
            'original_bill_ids':[s['bill_id'] for s in original],
            'periods':[{k:s.get(k) for k in ('bill_id','start','end','days','annual','premium',
                       'original_bill_id','original_premium','original_start','original_end',
                       'original_annual','age_at_start','band','bill_stage','source')}
                       for s in segments],
            'segments':segments})
        total+=expected;paidtotal+=paid
    state['calculations']=calculations
    state['business']={'total':str(total),'paid':str(paidtotal),'difference':str(total-paidtotal),'unit':'CNY'}
    return state
