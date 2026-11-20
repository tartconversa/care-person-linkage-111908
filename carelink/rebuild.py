"""从证据状态构建一个版本的全部维表/事实行（纯函数）并计算内容指纹。

重建是确定性的：同一证据状态产生完全相同的行与人员引用，因此重放文件
不会新增人员或服务（内容指纹相同的版本不重复发布）。
"""
from __future__ import annotations

import hashlib
import json

from .periods import contains, union_contains
from .resolve import Edge, person_id_for, resolve
from .schema import VERSIONED_COLUMNS


def _j(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def collect_edges(state):
    """当前生效的证据边：来源证明 + 数据人员确认 + 裁定；扣除撤销与裁定否决。

    撤销只让目标证据失效，不删来源历史；裁定的 overrides/rejects 只中和
    该裁定针对的证据，其余独立证据继续有效。
    """
    revoked = {r['target_id'] for r in state['revocations']}
    rejected, overridden = set(), set()
    for r in state['rulings']:
        rejected |= set(json.loads(r['rejects']))
        overridden |= set(json.loads(r['overrides']))
    same, diff = [], []
    for p in state['proofs']:
        if p['proof_id'] in revoked or p['proof_id'] in rejected:
            continue
        if p['relation'] == 'different' and p['proof_id'] in overridden:
            continue
        e = Edge(p['proof_id'], p['relation'], p['left_id'], p['right_id'],
                 (p['from_date'], p['until_date']), 'proof')
        (same if p['relation'] == 'same' else diff).append(e)
    for c in state['confirmations']:
        if c['confirmation_id'] in revoked or c['confirmation_id'] in rejected:
            continue
        same.append(Edge(c['confirmation_id'], 'same', c['left_id'], c['right_id'],
                         (c['from_date'], c['until_date']), 'confirmation'))
    for r in state['rulings']:
        if r['decision'] == 'confirm_same':
            same.append(Edge(r['ruling_id'], 'same', r['left_id'], r['right_id'],
                             (r['from_date'], r['until_date']), 'ruling'))
    return same, diff


def current_resolution(state):
    """当前证据状态下的解析结果与待裁定关联（按 pending_id 排序，确定输出）。"""
    identities = {r['identity_id']: (r['from_date'], r['until_date']) for r in state['identities']}
    same, diff = collect_edges(state)
    res = resolve(identities, same, diff)
    conflicts = []
    for c in res['conflicts']:
        pid = 'ADJ-' + _sha('|'.join([c['left'], c['right'], c['different_edge'],
                                      ','.join(c['same_path_edges'])]))[:10]
        conflicts.append({**c, 'pending_id': pid})
    conflicts.sort(key=lambda c: c['pending_id'])
    return res, conflicts


def build_version(state):
    """构建一个版本的全部行。返回 (built, extras)；built 的键是版本化表名。"""
    warnings = []
    identities = {r['identity_id']: r for r in state['identities']}
    periods = {i: (r['from_date'], r['until_date']) for i, r in identities.items()}
    res, conflicts = current_resolution(state)

    for e in res['inapplicable']:
        warnings.append(f"证据 {e.edge_id} 的适用时段与实例有效期不相交，未生效")

    pub_pid, full_pid = {}, {}
    for comp in res['published']:
        for i in comp:
            pub_pid[i] = person_id_for(comp)
    for comp in res['full']:
        for i in comp:
            full_pid[i] = person_id_for(comp)

    dispute_of = {}
    for c in conflicts:
        affected = next(comp for comp in res['full'] if c['left'] in comp)
        for i in affected:
            dispute_of.setdefault(i, c['pending_id'])

    built = {t: [] for t in VERSIONED_COLUMNS}

    # ---- 人员维表 ----
    members_of, new_payloads = {}, {}
    for comp in sorted(res['published'], key=lambda c: sorted(c)):
        pid = person_id_for(comp)
        members = sorted(comp)
        members_of[pid] = members
        names = sorted({identities[i]['name'] for i in members})
        sources = sorted({identities[i]['source'] for i in members})
        built['dim_person'].append((pid, _j(names), _j(sources), _j(members)))
        new_payloads[pid] = {
            'members': members, 'names': names, 'sources': sources,
            'periods': {i: [periods[i][0], periods[i][1]] for i in members},
        }
        for i in members:
            cov = res['coverage'][i]
            basis = 'confirmed_chain' if cov else 'instance_only'
            segs = sorted(([a, b] for a, b in (cov or [periods[i]])),
                          key=lambda s: (s[0] is None, s[0] or '', s[1] is None, s[1] or ''))
            built['dim_person_member'].append((pid, i, basis, _j(segs), periods[i][0], periods[i][1]))

    # ---- 服务事实：逐条按证据归属，不按旧人员整体搬运，也不按日期一刀切 ----
    svc_person = {}
    for s in sorted(state['services'], key=lambda r: r['service_id']):
        reg = s['identity_id']
        anchor = s['independent_identity'] or None
        eff = anchor or reg
        basis = 'independent_anchor' if anchor else 'registered_instance'
        reasons = []
        if pub_pid[eff] != full_pid[eff]:
            reasons.append('pending_adjudication')
        cov = res['coverage'][eff]
        if cov and not union_contains(cov, s['date']):
            reasons.append('outside_confirmed_period')
        if not contains(periods[eff], s['date']):
            warnings.append(f"服务 {s['service_id']} 的服务日不在实例 {eff} 有效期内")
        if reasons:
            # 尚有歧义：保留原来源身份与候选解释，不随便指派人员
            person = None
            cands = [{'interpretation': 'confirmed_evidence_only', 'person_id': pub_pid[eff]}]
            if full_pid[eff] != pub_pid[eff]:
                cands.append({'interpretation': 'all_same_evidence_unconfirmed',
                              'person_id': full_pid[eff]})
            if 'outside_confirmed_period' in reasons:
                cands.append({'interpretation': 'instance_alone',
                              'person_id': person_id_for([eff])})
            dispute = dispute_of.get(eff) if 'pending_adjudication' in reasons else None
        else:
            person, cands, dispute = pub_pid[eff], [], None
        svc_person[s['service_id']] = person
        built['fact_service'].append((
            s['service_id'], s['date'], reg, eff, person, basis,
            1 if reasons else 0, _j(reasons), _j(cands), dispute, s['household_claim']))

    # ---- 家庭关系：按来源实例有效段适用；未确认声明保持未确认 ----
    for h in sorted(state['households'], key=lambda r: r['evidence_id']):
        m = h['member']
        built['fact_household'].append((
            h['evidence_id'], h['household_id'], pub_pid[m],
            1 if h['confirmed'] else 0, periods[m][0], periods[m][1], m))

    # ---- 合作方人员日：只按授权实例计算，不经同人关系扩张 ----
    svc_by_inst = {}
    for s in state['services']:
        svc_by_inst.setdefault(s['identity_id'], []).append(s)
    for p in sorted(state['permissions'], key=lambda r: r['permission_id']):
        fields = json.loads(p['fields'])
        for s in sorted(svc_by_inst.get(p['identity_id'], []), key=lambda r: r['service_id']):
            if not contains((p['from_date'], p['until_date']), s['date']):
                continue
            visible = {}
            if 'date' in fields:
                visible['date'] = s['date']
            if 'name' in fields:
                visible['name'] = identities[p['identity_id']]['name']
            built['partner_day'].append((
                p['partner'], p['identity_id'], s['service_id'], s['date'],
                _j(visible), p['permission_id']))

    # ---- 结算交付：保留原编号与当时解释；解释变化时发出可核对的替代关联 ----
    baselines_to_set = {}
    for d in sorted(state['deliveries'], key=lambda r: r['delivery_id']):
        cur = svc_person[d['service_id']]
        base = state['delivery_baselines'].get(d['delivery_id'])
        if base is None:
            baseline = cur
            baselines_to_set[d['delivery_id']] = cur
        else:
            baseline = base['person']
        status = 'unresolved' if cur is None else (
            'matches_baseline' if cur == baseline else 'diverged')
        built['settlement_delivery'].append((
            d['delivery_id'], d['service_id'], d['old_person_ref'], d['partner'],
            cur, baseline, status))
        if cur != baseline:
            reason = ('resolved_after_unresolved' if baseline is None else
                      'became_unresolved' if cur is None else 'person_reference_changed')
            rid = 'SR-' + _sha('|'.join([d['delivery_id'], str(baseline), str(cur)]))[:10]
            built['settlement_replacement'].append((
                rid, d['delivery_id'], d['old_person_ref'], baseline, cur, reason))

    # ---- 人员变化：区分 新增/信息补全/两人合一/旧合并被拆开/退役 ----
    prev = state['prev_persons']
    succ_map = {}
    for pid in sorted(members_of):
        mem = set(members_of[pid])
        preds = sorted(op for op, o in prev.items() if set(o['members']) & mem)
        if not preds:
            kind, note = 'new', '新出现的人员'
        elif len(preds) > 1:
            kind, note = 'merge', '两人合一：多个旧人员合并'
        elif set(prev[preds[0]]['members']) == mem:
            if prev[preds[0]] == new_payloads[pid]:
                continue  # 完全不变，不产生变化记录
            kind, note = 'info_completion', '信息补全：人员属性更新'
        else:
            kind, note = 'split', '旧合并被拆开：成员重新归属'
        for op in preds:
            succ_map.setdefault(op, set()).add(pid)
        built['person_change'].append((pid, kind, _j(preds), _j([]), note))
    for op in sorted(prev):
        if op not in members_of:
            succ = sorted(succ_map.get(op, ()))
            note = '旧人员引用退役，由后继人员承接' if succ else '旧人员引用退役'
            built['person_change'].append((op, 'retired', _j([]), _j(succ), note))

    # ---- 待裁定关联：开放的 + 自上一版本以来关闭的 ----
    open_now = set()
    for c in conflicts:
        evidence = {'different_edge': c['different_edge'], 'same_path_edges': c['same_path_edges']}
        built['pending_item'].append((
            c['pending_id'], 'same_different_conflict', c['left'], c['right'],
            _j(evidence), 'open', None))
        open_now.add(c['pending_id'])
    ruled = {r['pending_id'] for r in state['rulings']}
    for pid, item in sorted(state['prev_open_pending'].items()):
        if pid not in open_now:
            built['pending_item'].append((
                pid, item['kind'], item['left'], item['right'], item['evidence'],
                'resolved', 'ruling' if pid in ruled else 'evidence'))

    # ---- 候选线索：只限制可能范围，永不自动确认 ----
    by_name, by_number = {}, {}
    for i, r in identities.items():
        by_name.setdefault(r['name'], []).append(i)
        by_number.setdefault((r['source'], r['local_id']), []).append(i)
    reactivated = {r['identity_id'] for r in state['reactivations']}

    def pairs(ids):
        ids = sorted(ids)
        return [(a, b) for x, a in enumerate(ids) for b in ids[x + 1:]]

    for name, ids in sorted(by_name.items()):
        for a, b in pairs(ids):
            built['candidate_pair'].append(
                (a, b, 'name_match', f'姓名相同：{name}（仅线索，不构成确认）', 'clue_only'))
    for (src, num), ids in sorted(by_number.items()):
        for a, b in pairs(ids):
            if a in reactivated or b in reactivated:
                continue  # 已有编号重新启用记录：复用边界已知，不再作为线索
            built['candidate_pair'].append(
                (a, b, 'local_id_reuse', f'机构 {src} 编号 {num} 复用，高风险线索', 'clue_only'))

    extras = {'warnings': warnings, 'baselines_to_set': baselines_to_set}
    return built, extras


# 人员变化与已关闭的待裁定记录描述的是版本间迁移，不是证据状态本身；
# 版本去重指纹只覆盖纯由证据状态推导的行，保证重放不产生新版本。
_TRANSITION_TABLES = {'person_change'}
_PENDING_STATUS_COL = VERSIONED_COLUMNS['pending_item'].index('status')


def content_hash(built):
    """版本内容指纹：同一证据状态必然得到同一指纹，用于重放去重。"""
    payload = {}
    for table, rows in sorted(built.items()):
        if table in _TRANSITION_TABLES:
            continue
        if table == 'pending_item':
            rows = [r for r in rows if r[_PENDING_STATUS_COL] == 'open']
        payload[table] = [list(r) for r in rows]
    return _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':')))
