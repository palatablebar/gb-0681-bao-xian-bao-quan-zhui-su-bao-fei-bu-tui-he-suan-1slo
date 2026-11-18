"""年龄更正记录的采信：来源行 kind=age 与 parameters.age_corrections。

- 同一更正重试（内容指纹相同）只保留一条，不新增期次；
- 同一 correction_id 内容不同视为冲突，整组保留证据并挂起，不猜最后一条；
- 表述错误（缺依据、年龄非法）的来源行隔离；显式参数错误直接 422。
"""
import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from app.errors import DomainError

AGE_KIND = 'age'
FINGERPRINT_FIELDS = ('policy_id', 'effective', 'recorded_age', 'corrected_age',
                      'basis', 'start', 'end')


def fingerprint(item):
    canonical = {key: item.get(key) for key in FINGERPRINT_FIELDS}
    raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]


def coerce_age(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('age')
    text = str(value).strip()
    if not text:
        raise ValueError('age')
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError('age')
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError('age')
    age = int(number)
    if age < 0 or age > 120:
        raise ValueError('age')
    return age


def _date(value):
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except (TypeError, ValueError):
        raise ValueError('date')


def _basis(value):
    return str(value or '').strip() or None


def correction(item, source, line=0):
    policy_id = str(item.get('policy_id', '')).strip()
    if not policy_id:
        raise DomainError('MISSING', '年龄更正缺少保单号')
    try:
        effective = _date(item.get('effective'))
    except ValueError:
        raise DomainError('DATE', '年龄更正核定日无效')
    try:
        corrected_age = coerce_age(item.get('corrected_age'))
    except ValueError:
        raise DomainError('NUMBER', '更正后年龄无效')
    recorded_raw = item.get('recorded_age', item.get('issue_age'))
    recorded_age = None
    if recorded_raw not in (None, ''):
        try:
            recorded_age = coerce_age(recorded_raw)
        except ValueError:
            raise DomainError('NUMBER', '原投保年龄无效')
    basis = _basis(item.get('age_basis') or item.get('basis') or item.get('correction_basis'))
    if not basis:
        raise DomainError('MISSING', '年龄更正缺少更正依据')
    term = {}
    for field in ('start', 'end'):
        value = item.get(field)
        if value in (None, ''):
            term[field] = None
            continue
        try:
            term[field] = _date(value)
        except ValueError:
            raise DomainError('DATE', '年龄更正期间无效')
    record = {
        'policy_id': policy_id,
        'correction_id': str(item.get('correction_id') or item.get('id') or '').strip(),
        'effective': effective, 'recorded_age': recorded_age,
        'corrected_age': corrected_age, 'basis': basis,
        'start': term['start'], 'end': term['end'],
        'version': str(item.get('version', '0')), 'line': line, 'source': source,
    }
    record['fingerprint'] = fingerprint(record)
    if not record['correction_id']:
        record['correction_id'] = 'AGE-' + record['fingerprint']
    return record


def _inherit_term(record, policy):
    if not record['start'] or not record['end']:
        record['start'] = record['start'] or policy['base']['start']
        record['end'] = record['end'] or policy['base']['end']
        record['fingerprint'] = fingerprint(record)
        if record['correction_id'].startswith('AGE-'):
            record['correction_id'] = 'AGE-' + record['fingerprint']
    return record


def _merge(records):
    unique = []
    seen = set()
    duplicates = 0
    for record in sorted(records, key=lambda r: (r['version'], r['correction_id'], r['line'])):
        marker = (record['policy_id'], record['fingerprint'])
        if marker in seen:
            duplicates += 1
            continue
        seen.add(marker)
        unique.append(record)
    return unique, duplicates


def collect(state, rows, policies):
    by_policy = {p['id']: p for p in policies}
    from_rows = []
    rejected_lines = set()

    def reject(line, ident, code, raw):
        # 隔离的来源行必须同时退出采纳集合，行数对账才不会被重复计入。
        rejected_lines.add(line)
        state.setdefault('age_rejections', []).append(
            {'line': line, 'id': ident, 'code': code, 'raw': raw})
        state['quarantine'].append({'line': line, 'id': ident, 'code': code, 'raw': raw})

    for row in [r for r in rows if r.get('kind') == AGE_KIND]:
        line = row.get('_line', 0)
        try:
            record = correction(row, 'source', line)
        except DomainError as error:
            reject(line, row.get('id', ''), error.code, row.get('_raw', {}))
            continue
        if record['policy_id'] not in by_policy:
            reject(line, row.get('id', ''), 'REFERENCE', row.get('_raw', {}))
            continue
        from_rows.append(_inherit_term(record, by_policy[record['policy_id']]))

    params = state.get('parameters') or {}
    raw_params = params.get('age_corrections') or []
    if not isinstance(raw_params, list):
        raise DomainError('AGE_CORRECTIONS', '年龄更正参数必须是数组')
    from_params = []
    for item in raw_params:
        if not isinstance(item, dict):
            raise DomainError('AGE_CORRECTIONS', '年龄更正条目必须是对象')
        record = correction(item, 'parameters')
        if record['policy_id'] not in by_policy:
            raise DomainError('REFERENCE', '年龄更正指向不存在的保单')
        from_params.append(_inherit_term(record, by_policy[record['policy_id']]))

    records, duplicates = _merge(from_rows + from_params)

    groups = {}
    for record in records:
        groups.setdefault((record['policy_id'], record['correction_id']), []).append(record)
    conflicted = set()
    for (policy_id, _), group in groups.items():
        if len({r['fingerprint'] for r in group}) > 1:
            conflicted.add(policy_id)
            if any(r['source'] == 'parameters' for r in group):
                raise DomainError('CONFLICT', '同一更正编号存在不同内容')
            for record in group:
                if record['source'] == 'source':
                    reject(record['line'], record['correction_id'], 'CONFLICT',
                           {'policy_id': policy_id, 'basis': record['basis'],
                            'effective': record['effective']})
    kept = [r for r in records if r['policy_id'] not in conflicted]

    for policy in policies:
        policy['age_corrections'] = sorted(
            (r for r in kept if r['policy_id'] == policy['id']),
            key=lambda r: (r['effective'], r['version'], r['correction_id']))
        policy['age_hold'] = ({'code': 'CONFLICT', 'message': '同一更正编号存在不同内容'}
                              if policy['id'] in conflicted else None)
    state['age_duplicate_count'] = duplicates
    if rejected_lines:
        state['rows'] = [r for r in state['rows']
                         if not (r.get('kind') == AGE_KIND and r.get('_line') in rejected_lines)]
    return state
