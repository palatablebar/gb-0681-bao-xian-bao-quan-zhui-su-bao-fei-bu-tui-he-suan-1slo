from copy import deepcopy
from app.config import REQUIRED,NUMERIC,DATES,TIMES
from app.errors import DomainError
from app.modules.m2_mapping.coerce import number,day,instant,SENTINELS

AGE_REQUIRED=('id','policy_id','version','kind','effective')

def is_age(row):
    return str(row.get('kind','')).strip()=='age'

def normalize(state):
    state=deepcopy(state);good=[];groups={}
    for row in state['rows']:
        try:
            if is_age(row):
                # 年龄更正行是依据记录，不携带当年保费；期间留空时沿用保单基线。
                for field in AGE_REQUIRED:
                    if str(row.get(field,'')).strip().upper() in SENTINELS:raise DomainError('MISSING','必填值缺失')
                item=dict(row)
                for field in DATES:
                    if item.get(field):item[field]=day(item[field])
                groups.setdefault((item['id'],str(item.get('version','1'))),[]).append(item)
                continue
            for field in REQUIRED:
                if str(row.get(field,'')).strip().upper() in SENTINELS:raise DomainError('MISSING','必填值缺失')
            item=dict(row)
            for field in DATES:
                if item.get(field):item[field]=day(item[field])
            for field in NUMERIC:
                if item.get(field) not in (None,''):item[field]=number(item[field])
            for field in TIMES:
                if field in item and item[field] != '':item[field]=instant(item[field])
            groups.setdefault((item['id'],str(item.get('version','1'))),[]).append(item)
        except DomainError as error:state['quarantine'].append({'line':row['_line'],'id':row.get('id',''),'code':error.code,'raw':row['_raw']})
    for key,rows in groups.items():
        unique={str(sorted((k,str(v)) for k,v in r.items() if not k.startswith('_'))) for r in rows}
        if len(unique)>1:
            for r in rows:state['quarantine'].append({'line':r['_line'],'id':r['id'],'code':'CONFLICT','raw':r['_raw']})
        else:
            good.append(rows[0]);state['duplicate_count']+=len(rows)-1
    state['rows']=good
    return state
