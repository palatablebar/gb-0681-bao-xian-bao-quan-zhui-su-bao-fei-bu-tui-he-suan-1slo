import base64
from flask import request,render_template,jsonify
from app.errors import DomainError
from app.config import TITLE,MODULES,SHORT
from app.workflow import run,load
from app import store

def payload():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'],str):raise DomainError('CSV','CSV内容必须是文本')
    if data.get('from_run'):
        # 同一来源切换年龄更正口径复核：复用已存来源，避免二次粘贴走样。
        source=base64.b64decode(store.get(str(data['from_run']))['source_b64'])
        return source,data.get('parameters',{})
    sample=data.get('sample',request.args.get('sample','dirty.csv'))
    content=data.get('csv')
    return (content.encode() if isinstance(content,str) else load(sample)),data.get('parameters',{})

def api(stage):
    content,parameters=payload();return jsonify(run(content,parameters,stage))

def page(stage,template):
    latest=store.history();state=store.get(latest[0]['id']) if latest else run(load('dirty.csv'),{})
    if 'sample' in request.args:
        # 指定样例时只预览该样例本身，不套用其它批次的更正参数。
        source=load(request.args['sample']);parameters={}
    else:
        source=base64.b64decode(state['source_b64']) if state.get('source_b64') else load('dirty.csv')
        parameters=state.get('parameters',state.get('manifest',{}).get('parameters',{}))
    data=run(source,parameters,stage)
    return render_template(template,title=TITLE,short=SHORT,modules=MODULES,current=stage,data=data,run=state,history=latest)
