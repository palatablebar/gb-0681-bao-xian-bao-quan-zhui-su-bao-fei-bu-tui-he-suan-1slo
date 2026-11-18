"""投保年龄更正：口径选择、周年边界与费率档重算。

原则：
1. 默认自下一次保单周年起适用（prospective），已寄出的续期账单保持原样；
2. 只有显式选择 retroactive 才从原承保日重算，且必须保留更正依据与每期原账单；
3. 计算失败或口径不适用时不得产生半套期次，旧账单原样保留（hold）。
本模块只产出 exact 值；到分金额统一由 M5 最大余额法分配。
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from app.errors import DomainError

DEFAULT_RATE_BANDS = (
    {'from_age': 0, 'to_age': 40, 'annual_premium': '3650'},
    {'from_age': 41, 'to_age': 50, 'annual_premium': '5475'},
    {'from_age': 51, 'to_age': 65, 'annual_premium': '7300'},
)

MODES = ('prospective', 'retroactive')
MODE_LABEL = {'prospective': '自下一次保单周年起', 'retroactive': '自原承保日重算'}


class AgeFailure(ValueError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}


def normalize_mode(parameters):
    raw = parameters.get('age_mode', parameters.get('age_correction_mode', 'prospective'))
    text = str(raw).strip().lower()
    aliases = {
        'prospective': 'prospective', 'renewal': 'prospective', 'next_anniversary': 'prospective',
        '周年': 'prospective', '下一次保单周年': 'prospective', '下一周年': 'prospective',
        'retroactive': 'retroactive', 'original': 'retroactive', 'issue': 'retroactive',
        '追溯': 'retroactive', '原承保日': 'retroactive', '原承保日重算': 'retroactive',
    }
    if text not in aliases:
        raise AgeFailure('AGE_MODE', '年龄更正口径无效', {'value': str(raw)})
    return aliases[text]


def parse_bands(parameters):
    raw = parameters.get('rate_bands') or [dict(b) for b in DEFAULT_RATE_BANDS]
    if not isinstance(raw, list) or not raw:
        raise AgeFailure('RATE_BANDS', '费率档必须是非空数组')
    bands = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AgeFailure('RATE_BANDS', '费率档条目必须是对象', {'index': index})
        try:
            low = int(item.get('from_age', item.get('from')))
            high = int(item.get('to_age', item.get('to')))
            premium = Decimal(str(item.get('annual_premium', item.get('premium'))))
        except (TypeError, ValueError, InvalidOperation):
            raise AgeFailure('RATE_BANDS', '费率档字段无效', {'index': index})
        if low < 0 or high < low or high > 120 or not premium.is_finite() or premium < 0:
            raise AgeFailure('RATE_BANDS', '费率档区间或金额无效', {'index': index})
        bands.append({'from_age': low, 'to_age': high, 'annual_premium': str(premium)})
    bands.sort(key=lambda b: (b['from_age'], b['to_age']))
    return bands


def add_years(value, years):
    """保单周年；2 月 29 日在非闰年落到 2 月 28 日。"""
    year = value.year + years
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year, month=2, day=28)


def anniversaries(start, end):
    """严格落在 (start, end) 内的保单周年。"""
    index = 1
    while True:
        point = add_years(start, index)
        if point >= end:
            return
        if point > start:
            yield point
        index += 1


def years_between(start, value):
    years = value.year - start.year
    if (value.month, value.day) < (start.month, start.day):
        years -= 1
    return years


def attained_age(issue_age, start, value):
    return int(issue_age) + max(0, years_between(start, value))


def next_anniversary(start, on):
    """严格晚于更正核定日的下一个保单周年；核定日恰为周年日时取再下一年。"""
    index = max(0, on.year - start.year)
    candidate = add_years(start, index)
    while candidate <= on:
        index += 1
        candidate = add_years(start, index)
    return candidate


def band_for(bands, age):
    for band in bands:
        if band['from_age'] <= age <= band['to_age']:
            return band
    raise AgeFailure('RATE_BAND', '更正后年龄无对应费率档', {'age': age})


def baseline_annual(policy, moment):
    annual = Decimal(str(policy['base']['annual_premium']))
    for change in policy['changes']:
        if change['effective'] <= moment.isoformat():
            annual = Decimal(str(change['annual_premium']))
    return annual


def corrected_annual(policy, bands, corrected_age, base_start, moment):
    """年龄更正只重算基础费率；显式批单的年保费仍然优先。"""
    active = None
    for change in policy['changes']:
        if change['effective'] <= moment.isoformat():
            active = change
    if active is not None:
        return (Decimal(str(active['annual_premium'])), None,
                active['id'], active['_line'])
    age = attained_age(corrected_age, base_start, moment)
    band = band_for(bands, age)
    return Decimal(band['annual_premium']), band, policy['base']['id'], policy['base']['_line']


def build_scenario(policy, correction, bands, mode):
    # 周年锚点永远是保单起期；更正期间只缩小适用范围，不改变周年节奏。
    issue = date.fromisoformat(policy['base']['start'])
    scope_start, end = issue, date.fromisoformat(policy['base']['end'])
    if correction.get('start') and correction.get('end'):
        scope_start = max(scope_start, date.fromisoformat(correction['start']))
        end = min(end, date.fromisoformat(correction['end']))
        if scope_start >= end:
            raise AgeFailure('RANGE', '更正期间与保单期间无交集', {
                'policy_id': policy['id'], 'correction_id': correction['correction_id']})
    corrected_age = correction['corrected_age']
    base_start = issue
    if mode == 'retroactive':
        boundary = scope_start
    else:
        boundary = max(scope_start,
                       next_anniversary(issue, date.fromisoformat(correction['effective'])))
    boundary = max(boundary, scope_start)
    scenario = {'mode': mode, 'boundary': boundary.isoformat(), 'status': 'APPLIED',
                'segments': [], 'exact_total': '0.00'}
    if boundary >= end:
        scenario['status'] = 'NO_ELIGIBLE_PERIOD'
        # 本期不再新开期次；把续期应适用的年费率一并留作依据。
        try:
            scenario['renewal_from'] = boundary.isoformat()
            scenario['renewal_annual'] = str(
                corrected_annual(policy, bands, corrected_age, base_start, boundary)[0])
        except AgeFailure as error:
            scenario['renewal_from'] = boundary.isoformat()
            scenario['renewal_annual'] = ''
            scenario['renewal_failure'] = {'code': error.code, 'message': error.message}
        return scenario

    points = {boundary, end}
    for point in anniversaries(base_start, end):
        if point <= boundary:
            continue
        before = corrected_annual(policy, bands, corrected_age, base_start,
                                  _previous_day(point))[0]
        after = corrected_annual(policy, bands, corrected_age, base_start, point)[0]
        if before != after:
            points.add(point)
    for change in policy['changes']:
        point = date.fromisoformat(change['effective'])
        if boundary < point < end:
            points.add(point)
    for year in range(boundary.year + 1, end.year + 1):
        point = date(year, 1, 1)
        if boundary < point < end:
            points.add(point)
    points = sorted(points)

    total = Decimal(0)
    changed = False
    for left, right in zip(points, points[1:]):
        annual, band, source, line = corrected_annual(policy, bands, corrected_age,
                                                      base_start, left)
        original = baseline_annual(policy, left)
        if annual != original:
            changed = True
        days = (right - left).days
        denominator = 366 if _leap(left.year) else 365
        exact = annual * days / denominator
        total += exact
        scenario['segments'].append({
            'id': policy['id'], 'source': source, 'line': line,
            'start': left.isoformat(), 'end': right.isoformat(), 'days': days,
            'annual': str(annual), 'year_days': denominator, 'exact': str(exact),
            'age_at_start': attained_age(corrected_age, base_start, left),
            'band': band, 'scenario': mode,
        })
    if not changed:
        scenario['status'] = 'NO_RATE_CHANGE'
        scenario['segments'] = []
        scenario['exact_total'] = '0.00'
    else:
        scenario['exact_total'] = str(total)
    return scenario


def _leap(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _previous_day(value):
    from datetime import timedelta
    return value - timedelta(days=1)


def _failed(mode,error):
    return {'mode':mode,'status':'FAILED','boundary':'','segments':[],'exact_total':'0.00',
            'failure':{'code':error.code,'message':error.message,'detail':error.detail}}


def _revision(policy,correction,mode,bands):
    scenarios={}
    for candidate in MODES:
        try:
            scenarios[candidate]=build_scenario(policy,correction,bands,candidate)
        except AgeFailure as error:
            scenarios[candidate]=_failed(candidate,error)
    selected=scenarios[mode]
    status=selected['status']
    failure=selected.get('failure')
    return {
        'policy_id':policy['id'],'correction_id':correction['correction_id'],
        'line':correction['line'],'effective':correction['effective'],
        'recorded_age':correction['recorded_age'],
        'corrected_age':correction['corrected_age'],
        'basis':correction['basis'],'selected_mode':mode,'status':status,
        'superseded_by':'','selected_boundary':selected.get('boundary',''),
        'failure':failure,'scenarios':scenarios,
        'periods':selected.get('segments',[]) if status=='APPLIED' else [],
    }


def revise(state):
    parameters=state.get('parameters') or {}
    try:
        mode=normalize_mode(parameters)
    except AgeFailure as error:
        raise DomainError(error.code,error.message)
    policies=state.get('policies',[])
    targeted=[p for p in policies if p.get('age_corrections') or p.get('age_hold')]
    summary={'mode':mode,'mode_label':MODE_LABEL[mode],
        'effective_from':'next_policy_anniversary' if mode=='prospective' else 'original_issue_date',
        'corrections':0,'superseded':0,'applied':0,'held':0,'failed':0,
        'status':'IDLE','hold_reason':'',
        'rationale':('默认自下一次保单周年起适用：已寄出的续期账单保持原样，不追溯改写；'
                     '追溯全额重算仅作对照，需显式选择 age_mode=retroactive 并留存更正依据。')}
    try:
        # 即使本批无更正，也校验费率档定义，避免坏参数被静默忽略。
        bands=parse_bands(parameters)
    except AgeFailure as error:
        raise DomainError(error.code, error.message)
    if not targeted:
        for candidate in MODES:
            summary[f'{candidate}_total']='0.00'
            summary[f'{candidate}_periods']=0
            summary[f'{candidate}_policies']=0
        return {'age_policy':summary,'age_revisions':[]}
    revisions=[]
    for policy in targeted:
        if policy.get('age_hold'):
            held={'policy_id':policy['id'],'correction_id':'','line':0,'effective':'',
                'recorded_age':None,'corrected_age':None,'basis':'','selected_mode':mode,
                'status':'HELD','superseded_by':'','selected_boundary':'',
                'failure':policy['age_hold'],'scenarios':{},'periods':[]}
            policy['age_revision']=held
            revisions.append(held)
            summary['held']+=1
            continue
        corrections=policy['age_corrections']
        active=corrections[-1]
        for correction in corrections[:-1]:
            revisions.append({
                'policy_id':policy['id'],'correction_id':correction['correction_id'],
                'line':correction['line'],'status':'SUPERSEDED','basis':correction['basis'],
                'superseded_by':active['correction_id'],
                'recorded_age':correction['recorded_age'],
                'corrected_age':correction['corrected_age'],
                'effective':correction['effective'],'selected_mode':mode,
                'selected_boundary':'','failure':None,'scenarios':{},'periods':[],
            })
            summary['superseded']+=1
        revision=_revision(policy,active,mode,bands)
        policy['age_revision']=revision
        revisions.append(revision)
        summary['corrections']+=1
        for candidate,scenario in revision['scenarios'].items():
            bucket=summary.setdefault('scenario_totals',{}).setdefault(
                candidate,{'exact_total':Decimal(0),'policies':0,'periods':0,'applied':0})
            bucket['policies']+=1
            if scenario['status']=='APPLIED':
                bucket['applied']+=1
                bucket['exact_total']+=Decimal(scenario['exact_total'])
                bucket['periods']+=len(scenario['segments'])
        if revision['status']=='APPLIED':
            summary['applied']+=1
        elif revision['status']=='FAILED':
            summary['failed']+=1
        else:
            summary['held']+=1
    totals=summary.pop('scenario_totals',{})
    for candidate in MODES:
        bucket=totals.get(candidate,{'exact_total':Decimal(0),'policies':0,'periods':0,'applied':0})
        summary[f'{candidate}_total']=str(bucket['exact_total'].quantize(Decimal('.01')))
        summary[f'{candidate}_periods']=bucket['periods']
        summary[f'{candidate}_policies']=bucket['applied']
    return {'age_policy':summary,'age_revisions':revisions}
