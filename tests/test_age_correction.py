import csv,io
from decimal import Decimal
import pytest
from app import store
from app.errors import DomainError
from app.workflow import load,run
from app.modules.m3_versions.rules import apply as version_apply
from app.modules.m4_segments.rules import cut
from app.modules.m4_segments.age import next_anniversary,add_years,attained_age
from app.modules.m5_calculate.rules import allocate
from app.modules.m7_archive.export_out import bundle

def age_params(policy_id='P108',mode='prospective',**extra):
    item={'policy_id':policy_id,'effective':'2026-06-01','recorded_age':30,
          'corrected_age':45,'basis':'出生日期核对','correction_id':'T-1'}
    item.update(extra)
    return {'age_mode':mode,'age_corrections':[item]}

def revision_for(state,policy_id,include_superseded=False):
    rows=[r for r in state['age_revisions'] if r['policy_id']==policy_id]
    if not include_superseded:rows=[r for r in rows if r['status']!='SUPERSEDED']
    return rows[-1]

def test_default_is_prospective_from_next_anniversary():
    s=run(load('age.csv'),{},7)
    assert s['age_policy']['mode']=='prospective'
    revision=[r for r in s['age_revisions'] if r['policy_id']=='P101'][0]
    assert revision['selected_boundary']=='2027-01-01'
    assert [p['start'] for p in revision['periods']]==['2027-01-01']

def test_retroactive_starts_at_issue_date():
    s=run(load('age.csv'),{'age_mode':'retroactive'},7)
    revision=revision_for(s,'P101')
    assert revision['selected_boundary']=='2024-01-01'
    assert len(revision['periods'])==4

def test_two_modes_differ_in_periods_and_amount():
    prospective=revision_for(run(load('age.csv'),age_params(),7),'P108')
    retroactive=revision_for(run(load('age.csv'),age_params(mode='retroactive'),7),'P108')
    assert prospective['scenarios']['prospective']['exact_total']=='5475'
    assert prospective['scenarios']['retroactive']['exact_total']=='21900'
    assert len(prospective['scenarios']['prospective']['segments'])==1
    assert len(prospective['scenarios']['retroactive']['segments'])==4
    assert prospective['scenarios']['prospective']['boundary']=='2027-01-01'
    assert prospective['scenarios']['retroactive']['boundary']=='2024-01-01'

def test_next_anniversary_is_strict():
    from datetime import date
    start=date(2024,1,1)
    assert next_anniversary(start,date(2025,12,31))==date(2026,1,1)
    assert next_anniversary(start,date(2026,1,1))==date(2027,1,1)
    assert add_years(date(2024,2,29),1)==date(2025,2,28)
    assert attained_age(45,start,date(2027,1,1))==48

def test_rate_band_boundary_creates_periods():
    # 更正后 49 岁：2026 保单周年满 51 岁跨入高档，必须切出新期次。
    s=run(load('age.csv'),age_params(mode='retroactive',corrected_age=49),7)
    periods=revision_for(s,'P108')['periods']
    assert [p['annual'] for p in periods]==['5475','5475','7300','7300']
    assert [p['age_at_start'] for p in periods]==[49,50,51,52]

def test_issued_bill_amounts_unchanged():
    s=run(load('age.csv'),{},7)
    for calc in s['calculations']:
        issued=[p for p in calc['segments'] if p['bill_stage']=='ISSUED']
        for period in issued:
            assert Decimal(period['premium'])==Decimal(period['original_premium'])

def test_corrected_periods_conserve_to_cent():
    s=run(load('age.csv'),age_params(mode='retroactive'),7)
    for calc in s['calculations']:
        assert sum(Decimal(p['premium']) for p in calc['segments'])==Decimal(calc['expected'])
        assert sum(Decimal(p['original_premium']) for p in calc['segments'])==Decimal(calc['original_expected'])

def test_same_correction_retry_does_not_add_periods():
    base=run(load('age.csv'),age_params(),7)
    retry=run(load('age.csv'),age_params(),7)
    assert len(base['segments'])==len(retry['segments'])
    assert [p['bill_id'] for p in base['segments']]==[p['bill_id'] for p in retry['segments']]
    assert base['run_id']==retry['run_id']

def test_duplicate_correction_row_is_counted_not_extra_period():
    s=run(load('age.csv'),{},7)
    assert s['age_duplicate_count']==1
    assert s['reconcile']['balanced'] is True

def test_conflicting_correction_held_without_new_periods():
    s=run(load('age.csv'),{},7)
    held=revision_for(s,'P104')
    assert held['status']=='HELD' and held['failure']['code']=='CONFLICT'
    assert held['periods']==[]
    calc=[c for c in s['calculations'] if c['id']=='P104'][0]
    assert calc['expected']==calc['original_expected'] and calc['age_delta']=='0'

def test_one_failed_switch_keeps_all_old_bills():
    s=run(load('age_hold.csv'),{},7)
    assert s['age_policy']['status']=='HELD'
    assert s['age_policy']['failed']==1
    assert len(s['segments'])==len(s['original_bills'])
    assert all(p['bill_stage']=='ISSUED' for p in s['segments'])
    for calc in s['calculations']:
        assert calc['expected']==calc['original_expected'] and calc['age_delta']=='0'

def test_no_eligible_period_records_renewal_rate():
    s=run(load('age.csv'),{},7)
    revision=revision_for(s,'P105')
    assert revision['status']=='NO_ELIGIBLE_PERIOD'
    scenario=revision['scenarios']['prospective']
    assert scenario['segments']==[] and scenario['renewal_from']=='2028-01-01'
    calc=[c for c in s['calculations'] if c['id']=='P105'][0]
    assert calc['expected']==calc['original_expected']

def test_missing_basis_and_reference_isolated():
    s=run(load('age.csv'),{},7)
    codes=[q['code'] for q in s['quarantine']]
    assert codes.count('MISSING')>=1 and codes.count('REFERENCE')>=1
    assert s['reconcile']['balanced'] is True

def test_invalid_mode_rejected_before_any_periods():
    with pytest.raises(DomainError) as error:
        run(load('age.csv'),{'age_mode':'whatever'},7)
    assert error.value.code=='AGE_MODE' and error.value.status==422

def test_param_correction_conflict_rejected():
    # 参数与来源行同编号不同内容：不猜后到者，整组拒绝。
    params=age_params(policy_id='P101',correction_id='A101',basis='另一份依据')
    with pytest.raises(DomainError) as error:
        run(load('age.csv'),params,7)
    assert error.value.code=='CONFLICT'

def test_archive_keeps_original_bills_and_basis():
    s=run(load('age.csv'),{},7)
    rows=list(csv.reader(io.StringIO(bundle(s).lstrip('﻿'))))
    kinds={row[0] for row in rows[1:]}
    assert {'年龄更正口径','年龄更正','原账单','最终账单'}<=kinds
    original=[row for row in rows if row[0]=='原账单']
    assert len(original)==len(s['original_bills'])
    basis=[row for row in rows if row[0]=='年龄更正' and row[3]=='A101'][0]
    assert '出生日期核对' in basis[5]

def test_download_rejects_lost_bills(client,monkeypatch):
    state=client.post('/api/runs',json={'sample':'age.csv'}).get_json()
    ok=client.get('/api/archive/'+state['run_id']+'/export.csv')
    assert ok.status_code==200
    damaged=dict(state);damaged['original_bills']=[]
    monkeypatch.setattr(store,'get',lambda run_id:damaged)
    blocked=client.get('/api/archive/'+state['run_id']+'/export.csv')
    assert blocked.status_code==409 and blocked.get_json()['code']=='BILLS_LOST'

def test_switch_mode_from_stored_run(client):
    first=client.post('/api/runs',json={'sample':'age.csv'}).get_json()
    second=client.post('/api/runs',json={'from_run':first['run_id'],
                                         'parameters':{'age_mode':'retroactive'}}).get_json()
    assert second['age_policy']['mode']=='retroactive'
    assert second['run_id']!=first['run_id']
    again=client.post('/api/runs',json={'from_run':first['run_id'],
                                        'parameters':{'age_mode':'retroactive'}}).get_json()
    assert again['run_id']==second['run_id']

def _csv(rows):
    header=['id','policy_id','version','kind','effective','start','end','annual_premium','paid',
            'ref','cover','note','recorded_age','corrected_age','age_basis']
    out=io.StringIO(newline='');writer=csv.writer(out);writer.writerow(header);writer.writerows(rows)
    return out.getvalue().encode('utf-8-sig')

def test_boundary_inside_issued_bill_keeps_original_intact():
    raw=_csv([
        ['1','P1','1','issue','2024-04-01','2024-04-01','2028-04-01','5475','21900','',
         '基础险','保单原件','','',''],
        ['A1','P1','1','age','2025-06-01','','','','','','基础险','年龄更正','45','52','出生日期核对'],
    ])
    s=run(raw,{},7)
    calc=s['calculations'][0]
    # 原账单保持原区间原金额（按自然年切段），不因更正被切开或改数。
    spans=[(b['start'],b['end']) for b in s['original_bills']]
    assert spans==[('2024-04-01','2025-01-01'),('2025-01-01','2026-01-01'),
                   ('2026-01-01','2027-01-01'),('2027-01-01','2028-01-01'),
                   ('2028-01-01','2028-04-01')]
    assert sum(Decimal(p['original_premium']) for p in calc['segments'])==Decimal(calc['original_expected'])
    assert sum(Decimal(p['premium']) for p in calc['segments'])==Decimal(calc['expected'])
    # 2027-01-01 边界把 2026 年度那期切成两段，但该原账单分配合计仍等于 5475.00。
    split=[p for p in calc['segments'] if p.get('original_start')=='2026-01-01']
    assert len(split)==2 and sum(Decimal(p['original_premium']) for p in split)==Decimal('5475.00')

def test_issued_bill_ids_survive_correction():
    s=run(load('age.csv'),{},7)
    for calc in s['calculations']:
        assert set(calc['original_bill_ids'])=={b['bill_id'] for b in s['original_bills'] if b['id']==calc['id']}

def test_invalid_parameters_are_422():
    for params,code in [
        ({'rate_bands':[{'from_age':50,'to_age':40,'annual_premium':'1'}]},'RATE_BANDS'),
        ({'age_corrections':{'policy_id':'P101'}},'AGE_CORRECTIONS'),
        ({'age_corrections':[{'policy_id':'NOPE','effective':'2026-06-01',
                              'corrected_age':40,'basis':'x'}]},'REFERENCE'),
        ({'age_corrections':[{'policy_id':'P101','effective':'2026-06-01',
                              'corrected_age':'abc','basis':'x'}]},'NUMBER'),
        ({'age_corrections':[{'policy_id':'P101','effective':'2026-06-01',
                              'corrected_age':40}]},'MISSING'),
        ({'age_corrections':[{'policy_id':'P101','effective':'2026-13-01',
                              'corrected_age':40,'basis':'x'}]},'DATE'),
    ]:
        with pytest.raises(DomainError) as error:
            run(load('age.csv'),params,7)
        assert error.value.code==code and error.value.status==422

def test_rate_bands_are_configurable():
    params={**age_params(corrected_age=45),'rate_bands':[{'from_age':0,'to_age':120,'annual_premium':'4000'}]}
    s=run(load('age.csv'),params,7)
    revision=revision_for(s,'P108')
    assert {p['annual'] for p in revision['periods']}=={'4000'}

def test_higher_premium_than_band_keeps_explicit_change():
    # 批单年保费仍优先于年龄档，年龄更正只调基础费率部分。
    s=run(load('dirty.csv'),{'age_mode':'retroactive','age_corrections':[
        {'policy_id':'P002','effective':'2026-06-01','recorded_age':30,'corrected_age':52,
         'basis':'出生日期核对','correction_id':'X1'}]},7)
    revision=[r for r in s['age_revisions'] if r['policy_id']=='P002'][0]
    assert [(p['start'],p['annual'],p['source']) for p in revision['periods']]==[
        ('2026-01-01','7300','00004'),('2026-07-01','7300','00005'),('2026-10-01','5475','00006')]

def test_correction_term_limits_scope():
    raw=_csv([
        ['1','P1','1','issue','2024-01-01','2024-01-01','2028-01-01','3650','14600','',
         '基础险','保单原件','','',''],
        ['A1','P1','1','age','2026-06-01','2026-01-01','2027-01-01','','','','基础险',
         '年龄更正','30','45','出生日期核对'],
    ])
    revision=run(raw,{'age_mode':'retroactive'},7)['age_revisions'][0]
    assert revision['status']=='APPLIED'
    assert [(p['start'],p['end']) for p in revision['periods']]==[('2026-01-01','2027-01-01')]
    out_of_scope=_csv([
        ['1','P1','1','issue','2024-01-01','2024-01-01','2028-01-01','3650','14600','',
         '基础险','保单原件','','',''],
        ['A1','P1','1','age','2026-06-01','2029-01-01','2030-01-01','','','','基础险',
         '年龄更正','30','45','出生日期核对'],
    ])
    failed=run(out_of_scope,{},7)
    assert failed['age_revisions'][0]['status']=='FAILED'
    assert failed['age_revisions'][0]['failure']['code']=='RANGE'
    assert len(failed['segments'])==len(failed['original_bills'])

def test_new_correction_id_supersedes_instead_of_stacking():
    raw=_csv([
        ['1','P1','1','issue','2024-01-01','2024-01-01','2028-01-01','3650','14600','',
         '基础险','保单原件','','',''],
        ['A1','P1','1','age','2025-06-01','','','','','','基础险','首次更正','30','45','出生日期核对'],
        ['A2','P1','2','age','2026-06-01','','','','','','基础险','复核更正','30','49','出生医学证明'],
    ])
    s=run(raw,{'age_mode':'retroactive'},7)
    revisions=[r for r in s['age_revisions'] if r['policy_id']=='P1']
    statuses={r['correction_id']:r['status'] for r in revisions}
    assert statuses=={'A1':'SUPERSEDED','A2':'APPLIED'}
    applied=[r for r in revisions if r['status']=='APPLIED'][0]
    assert applied['superseded_by']=='' and applied['corrected_age']==49
    assert len(applied['periods'])==4

def test_scoped_correction_keeps_anniversary_anchor():
    raw=_csv([
        ['1','P1','1','issue','2024-01-01','2024-01-01','2028-01-01','3650','14600','',
         '基础险','保单原件','','',''],
        ['A1','P1','1','age','2026-06-01','2026-07-01','2028-01-01','','','','基础险',
         '年龄更正','30','45','出生日期核对'],
    ])
    prospective=run(raw,{'age_mode':'prospective'},7)['age_revisions'][0]
    retroactive=run(raw,{'age_mode':'retroactive'},7)['age_revisions'][0]
    assert prospective['selected_boundary']=='2027-01-01'
    assert [p['start'] for p in prospective['periods']]==['2027-01-01']
    assert retroactive['selected_boundary']=='2026-07-01'
    assert [p['start'] for p in retroactive['periods']]==['2026-07-01','2027-01-01']

def test_legacy_and_clean_unaffected():
    for sample in ('clean.csv','dirty.csv','legacy.csv'):
        s=run(load(sample),{},7)
        assert s['age_policy']['corrections']==0 and s['age_revisions']==[]
        assert s['reconcile']['balanced'] is True
