from datetime import date
from decimal import Decimal
import calendar
import hashlib

from app.modules.m4_segments.age import revise

def period_id(policy_id,start,end,source):
    raw=f'{policy_id}|{start}|{end}|{source}'.encode()
    return 'B'+hashlib.sha1(raw).hexdigest()[:10].upper()

def cut(policy):
    base=policy['base'];start=date.fromisoformat(base['start']);end=date.fromisoformat(base['end'])
    changes=policy['changes'];points={start,end}
    for r in changes:
        d=date.fromisoformat(r['effective'])
        if start<d<end:points.add(d)
    for year in range(start.year+1,end.year+1):
        d=date(year,1,1)
        if start<d<end:points.add(d)
    result=[];points=sorted(points)
    for left,right in zip(points,points[1:]):
        active=base
        for r in changes:
            if r['effective']<=left.isoformat():active=r
        days=(right-left).days;denominator=366 if calendar.isleap(left.year) else 365
        result.append({'id':policy['id'],'source':active['id'],'line':active['_line'],'start':left.isoformat(),'end':right.isoformat(),'days':days,'annual':str(active['annual_premium']),'year_days':denominator,'exact':str(Decimal(str(active['annual_premium']))*days/denominator)})
    return result

def clip(periods,lo,hi):
    """截到更正边界前；边界落在原账单内部时派生一个片段，原账单本身保持不变。"""
    out=[]
    for p in periods:
        start=max(date.fromisoformat(p['start']),lo);end=min(date.fromisoformat(p['end']),hi)
        if start>=end:continue
        item=dict(p);item['start']=start.isoformat();item['end']=end.isoformat();item['days']=(end-start).days
        item['exact']=str(Decimal(item['annual'])*item['days']/item['year_days'])
        if start.isoformat()!=p['start'] or end.isoformat()!=p['end']:
            item['original_bill_id']=p['bill_id']
            item['original_start']=p['start'];item['original_end']=p['end']
            item['original_annual']=p['annual']
            item['bill_id']=period_id(p['id'],item['start'],item['end'],'PRE'+p['bill_id'])
        out.append(item)
    return out

def stamp(period,policy_id,stage,mode,revision):
    item=dict(period)
    item['bill_id']=period_id(policy_id,item['start'],item['end'],item.get('source',''))
    item['bill_stage']=stage
    item['scenario']=mode
    if revision:
        item['correction_id']=revision['correction_id']
        item['basis']=revision['basis']
        item['age_at_start']=period.get('age_at_start')
    return item

def apply(state):
    policies=state['policies']
    originals={}
    for p in policies:
        # 基线按原批单切段，年龄更正不得改写这些已寄出的期次。
        p['baseline_segments']=cut(p)
        originals[p['id']]=[stamp(s,p['id'],'ISSUED','original',None) for s in cut(p)]
    age=revise(state)
    state.update(age)
    policy_state=state['age_policy']
    failed=[r for r in state['age_revisions'] if r['status']=='FAILED']
    if failed:
        # 一次性切换是原子的：任一更正无法完成，全批保留旧账单，不留下半套期次。
        # 失败更正本身的 selected_boundary 仍保留，供复核定位受影响的起期。
        policy_state['status']='HELD'
        policy_state['hold_reason']='存在无法完成的年龄更正，已保留全部原账单'
        for revision in state['age_revisions']:
            if revision['status']=='SUPERSEDED':
                continue
            revision['held_by_batch']=True
            revision['periods']=[]
            revision['applied']=False
            policy=next((p for p in policies if p['id']==revision['policy_id']),None)
            if policy:
                revision['final_bill_ids']=[s['bill_id'] for s in originals[policy['id']]]
        policy_state['applied']=0
        policy_state['held']=sum(1 for r in state['age_revisions'] if r['status']!='SUPERSEDED')
    else:
        policy_state['status']='APPLIED'
        policy_state['hold_reason']=''
        for p in policies:
            revision=p.get('age_revision')
            if not revision or revision['status']!='APPLIED':
                if revision:
                    revision['applied']=False
                    revision['final_bill_ids']=[s['bill_id'] for s in originals[p['id']]]
                continue
            boundary=date.fromisoformat(revision['selected_boundary'])
            base_start=date.fromisoformat(p['base']['start'])
            end=date.fromisoformat(p['base']['end'])
            pre=clip(originals[p['id']],base_start,boundary)
            post=[stamp(s,p['id'],'CORRECTED',revision['selected_mode'],revision) for s in revision['periods']]
            final=pre+post
            revision['applied']=True
            revision['periods']=post
            revision['original_bill_ids']=[s['bill_id'] for s in originals[p['id']]]
            revision['final_bill_ids']=[s['bill_id'] for s in final]
            p['age_final']=final
    segments=[]
    for p in policies:
        final=p.get('age_final') or originals[p['id']]
        segments.extend(final)
    state['segments']=segments
    state['original_bills']=[s for p in policies for s in originals[p['id']]]
    state['baseline']=[{'id':s['id'],'start':s['start'],'end':s['end'],'days':s['days'],
                        'annual':s['annual'],'year_days':s['year_days'],'exact':s['exact'],
                        'source':s['source'],'line':s['line']}
                       for p in policies for s in p.pop('baseline_segments')]
    state.pop('age_final',None)
    for p in policies:p.pop('age_final',None)
    return state
