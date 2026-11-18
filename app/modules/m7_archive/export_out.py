import io,csv,json,hashlib

COLUMNS=['记录类型','批次','来源行','业务ID','原因码','内容']

def _row(kind,rid,line,ident,code,payload):
    return [kind,rid,line,ident,code,json.dumps(payload,ensure_ascii=False,sort_keys=True)]

def evidence(state):
    rid=state.get('run_id','preview')
    rows=[_row('运行清单',rid,'','','',state.get('manifest',{})),
          _row('对账',rid,'','','',state.get('reconcile',{})),
          _row('年龄更正口径',rid,'','','',state.get('age_policy',{}))]
    for revision in state.get('age_revisions',[]):
        rows.append(_row('年龄更正',rid,revision.get('line',''),revision.get('correction_id',''),
                         revision.get('status',''),{
                             'policy_id':revision.get('policy_id',''),
                             'effective':revision.get('effective',''),
                             'recorded_age':revision.get('recorded_age'),
                             'corrected_age':revision.get('corrected_age'),
                             'basis':revision.get('basis',''),
                             'selected_mode':revision.get('selected_mode',''),
                             'selected_boundary':revision.get('selected_boundary',''),
                             'superseded_by':revision.get('superseded_by',''),
                             'failure':revision.get('failure'),
                             'scenario_totals':{m:s.get('exact_total')
                                for m,s in (revision.get('scenarios') or {}).items()}}))
    for bill in state.get('baseline',[]):
        rows.append(_row('原切段',rid,bill.get('line',''),bill.get('id',''),'ISSUED',bill))
    # 每期原账单独立留存，更正改写后仍可逐期对客户解释。
    for bill in state.get('original_bills',[]):
        rows.append(_row('原账单',rid,bill.get('line',''),bill.get('bill_id',''),'ISSUED',{
            'policy_id':bill.get('id',''),'source':bill.get('source',''),
            'start':bill.get('start',''),'end':bill.get('end',''),'days':bill.get('days'),
            'annual':bill.get('annual',''),'year_days':bill.get('year_days'),
            'exact':bill.get('exact',''),'original_premium':bill.get('original_premium','')}))
    for row in state.get('calculations',[]):
        rows.append(_row('计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),row))
        for period in row.get('periods',[]):
            rows.append(_row('最终账单',rid,row.get('line',''),period.get('bill_id',''),
                             'CORRECTED' if period.get('bill_stage')!='ISSUED' else 'ISSUED',
                             period))
    for row in state.get('quarantine',[]):
        rows.append(_row('隔离',rid,row['line'],row.get('id',''),row['code'],row.get('raw',{})))
    return rows

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out);writer.writerow(COLUMNS)
    for row in evidence(state):writer.writerow(row)
    return '﻿'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
