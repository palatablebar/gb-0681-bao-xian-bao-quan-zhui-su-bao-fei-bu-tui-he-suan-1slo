from app.modules.m7_archive.export_out import checksum

def execute(state):
    from app.modules.m7_archive.export_out import evidence
    rows=evidence(state)
    state['archive']={'sha256':checksum(state),'evidence_count':len(rows),
        'bundled':{kind:sum(1 for r in rows if r[0]==kind) for kind in
                   ('原切段','原账单','最终账单','年龄更正','年龄更正口径','计算','隔离')},
        'age_mode':state.get('age_policy',{}).get('mode',''),
        'age_status':state.get('age_policy',{}).get('status',''),
        'original_bills':len(state.get('original_bills',[])),
        'ready':state.get('reconcile',{}).get('balanced',False)}
    return state
