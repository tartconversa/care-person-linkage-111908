"""持久化接收、证据提交、重建发布与按版本查询。

- 接收：校验资料指纹后，在同一事务内写事件日志并更新当前来源状态；重放
  同一文件是空操作，不新增人员或服务。
- 证据：同人确认（带适用时段）、撤销、编号重新启用、裁定，全部追加式记录。
- 重建：从证据状态确定性地构建新版本，全部版本化表与当前版本指针在同一
  事务内提交；中途失败整体回滚，不会留下新人员维表配旧事实外键。
- 查询：所有仓表按版本读取，旧版本状态永久可查。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import rebuild as rb
from .schema import DDL, VERSIONED_COLUMNS

_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _check_date(d):
    if d is not None and not _DATE.match(d):
        raise ValueError(f'日期格式无效：{d}')


def _check_period(from_date, until_date):
    _check_date(from_date)
    _check_date(until_date)
    if from_date is None:
        raise ValueError('缺少开始日期')
    if until_date is not None and until_date <= from_date:
        raise ValueError('时段结束须晚于开始')


class CareLink:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- 接收

    def intake(self, root, only=None):
        """接收来源文件。only 可限定文件主名集合（如 {'identities','proofs'}）。

        先验证资料指纹与引用，再在同一事务内落库；任一文件无效则整体回滚。
        重放同一文件（指纹相同）是空操作。
        """
        from support.check_inputs import check
        root = Path(root).resolve()
        check(root)
        catalog = json.loads((root / 'data/catalog.json').read_text(encoding='utf-8'))
        applied, replayed = [], []
        with self.conn:
            for entry in catalog['files']:
                stem = Path(entry['path']).stem
                if only and stem not in only:
                    continue
                raw = (root / entry['path']).read_bytes()
                h = hashlib.sha256(raw).hexdigest()
                cur = self.conn.execute(
                    'INSERT OR IGNORE INTO events(event_type,content_hash,payload,received_at)'
                    ' VALUES(?,?,?,?)',
                    ('source_file', h,
                     json.dumps({'path': entry['path'], 'sha256': h}, ensure_ascii=False),
                     _now()))
                if cur.rowcount == 0:
                    replayed.append(entry['path'])
                    continue
                self._load_rows(stem, json.loads(raw.decode('utf-8')))
                applied.append(entry['path'])
            self._validate_refs()
        return {'applied': applied, 'replayed': replayed}

    def _load_rows(self, stem, rows):
        if stem == 'identities':
            for r in rows:
                _check_period(r['from_date'], r['until_date'])
                self.conn.execute(
                    'INSERT INTO identities(identity_id,source,local_id,from_date,until_date,name)'
                    ' VALUES(?,?,?,?,?,?) ON CONFLICT(identity_id) DO UPDATE SET'
                    ' source=excluded.source, local_id=excluded.local_id,'
                    ' from_date=excluded.from_date, until_date=excluded.until_date,'
                    ' name=excluded.name',
                    (r['identity_id'], r['source'], r['local_id'],
                     r['from_date'], r['until_date'], r['name']))
        elif stem == 'proofs':
            for r in rows:
                if r['relation'] not in ('same', 'different'):
                    raise ValueError(f"关系类型无效：{r['relation']}")
                _check_period(r['from_date'], r['until_date'])
                cur = self.conn.execute(
                    'INSERT OR IGNORE INTO proofs VALUES(?,?,?,?,?,?,?)',
                    (r['proof_id'], r['left'], r['right'], r['relation'],
                     r['from_date'], r['until_date'], r['received_at']))
                if cur.rowcount == 0:
                    self._check_unchanged('proofs', 'proof_id', r['proof_id'],
                                          ('left_id', r['left']), ('right_id', r['right']),
                                          ('relation', r['relation']))
        elif stem == 'revocations':
            for r in rows:
                cur = self.conn.execute(
                    'INSERT OR IGNORE INTO revocations(revocation_id,target_kind,target_id,received_at)'
                    " VALUES(?,'proof',?,?)",
                    (r['revocation_id'], r['proof_id'], r['received_at']))
                if cur.rowcount == 0:
                    self._check_unchanged('revocations', 'revocation_id', r['revocation_id'],
                                          ('target_id', r['proof_id']))
        elif stem == 'services':
            for r in rows:
                _check_date(r['date'])
                self.conn.execute(
                    'INSERT INTO services(service_id,identity_id,date,independent_identity,household_claim)'
                    ' VALUES(?,?,?,?,?) ON CONFLICT(service_id) DO UPDATE SET'
                    ' identity_id=excluded.identity_id, date=excluded.date,'
                    ' independent_identity=excluded.independent_identity,'
                    ' household_claim=excluded.household_claim',
                    (r['service_id'], r['identity_id'], r['date'],
                     r['independent_identity'], r['household_claim']))
        elif stem == 'households':
            for r in rows:
                self.conn.execute(
                    'INSERT INTO households(evidence_id,household_id,member,confirmed)'
                    ' VALUES(?,?,?,?) ON CONFLICT(evidence_id) DO UPDATE SET'
                    ' household_id=excluded.household_id, member=excluded.member,'
                    ' confirmed=excluded.confirmed',
                    (r['evidence_id'], r['household_id'], r['member'], 1 if r['confirmed'] else 0))
        elif stem == 'deliveries':
            for r in rows:
                # 更新来源列，绝不动基线解释（baseline_*）
                self.conn.execute(
                    'INSERT INTO deliveries(delivery_id,service_id,old_person_ref,partner)'
                    ' VALUES(?,?,?,?) ON CONFLICT(delivery_id) DO UPDATE SET'
                    ' service_id=excluded.service_id,'
                    ' old_person_ref=excluded.old_person_ref, partner=excluded.partner',
                    (r['delivery_id'], r['service_id'], r['old_person_ref'], r['partner']))
        elif stem == 'permissions':
            for r in rows:
                _check_period(r['from_date'], r['until_date'])
                self.conn.execute(
                    'INSERT INTO permissions(permission_id,identity_id,partner,fields,from_date,until_date)'
                    ' VALUES(?,?,?,?,?,?) ON CONFLICT(permission_id) DO UPDATE SET'
                    ' identity_id=excluded.identity_id, partner=excluded.partner,'
                    ' fields=excluded.fields, from_date=excluded.from_date,'
                    ' until_date=excluded.until_date',
                    (r['permission_id'], r['identity_id'], r['partner'],
                     json.dumps(r['fields'], ensure_ascii=False, sort_keys=True),
                     r['from_date'], r['until_date']))
        else:
            raise ValueError(f'未知来源类型：{stem}')

    def _check_unchanged(self, table, key_col, key, *pairs):
        row = self.conn.execute(
            f'SELECT * FROM {table} WHERE {key_col}=?', (key,)).fetchone()
        if row is None:
            return
        for col, val in pairs:
            if row[col] != val:
                raise ValueError(f'标识 {key} 在 {table} 中已存在且内容不同，拒绝覆盖')

    def _validate_refs(self):
        ids = {r[0] for r in self.conn.execute('SELECT identity_id FROM identities')}
        svcs = {r[0] for r in self.conn.execute('SELECT service_id FROM services')}
        proof_ids = {r[0] for r in self.conn.execute('SELECT proof_id FROM proofs')}
        conf_ids = {r[0] for r in self.conn.execute('SELECT confirmation_id FROM confirmations')}

        def need(value, pool, what):
            if value not in pool:
                raise ValueError(f'引用不存在：{what} {value}')

        for r in self.conn.execute('SELECT proof_id,left_id,right_id FROM proofs'):
            need(r['left_id'], ids, '实例')
            need(r['right_id'], ids, '实例')
        for r in self.conn.execute('SELECT service_id,identity_id,independent_identity FROM services'):
            need(r['identity_id'], ids, '实例')
            if r['independent_identity'] is not None:
                need(r['independent_identity'], ids, '独立证明锚点')
        for r in self.conn.execute('SELECT evidence_id,member FROM households'):
            need(r['member'], ids, '实例')
        for r in self.conn.execute('SELECT delivery_id,service_id FROM deliveries'):
            need(r['service_id'], svcs, '服务')
        for r in self.conn.execute('SELECT permission_id,identity_id FROM permissions'):
            need(r['identity_id'], ids, '实例')
        for r in self.conn.execute('SELECT revocation_id,target_kind,target_id FROM revocations'):
            pool = proof_ids if r['target_kind'] == 'proof' else conf_ids
            need(r['target_id'], pool, '撤销目标')

    # ---------------------------------------------------------------- 证据提交

    def _event(self, event_type, payload, received_at):
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cur = self.conn.execute(
            'INSERT OR IGNORE INTO events(event_type,content_hash,payload,received_at)'
            ' VALUES(?,?,?,?)',
            (event_type, _sha(body), body, received_at or _now()))
        return cur.rowcount == 1

    def _require_instances(self, *identity_ids):
        for i in identity_ids:
            if self.conn.execute('SELECT 1 FROM identities WHERE identity_id=?',
                                 (i,)).fetchone() is None:
                raise ValueError(f'实例不存在：{i}')

    def confirm(self, left, right, from_date, until_date=None, by='system', note='',
                received_at=None):
        """数据人员提交带适用时段的同人确认。"""
        self._require_instances(left, right)
        _check_period(from_date, until_date)
        cid = 'CF-' + _sha('|'.join(['confirm', left, right, from_date,
                                     str(until_date), by, note]))[:10]
        payload = {'confirmation_id': cid, 'left': left, 'right': right,
                   'from_date': from_date, 'until_date': until_date, 'by': by, 'note': note}
        with self.conn:
            if self._event('confirmation', payload, received_at):
                self.conn.execute(
                    'INSERT OR IGNORE INTO confirmations VALUES(?,?,?,?,?,?,?,?)',
                    (cid, left, right, from_date, until_date, by, note,
                     received_at or _now()))
        return cid

    def revoke(self, target_kind, target_id, by='system', received_at=None):
        """撤销一条确认级证据（来源证明或同人确认）；不删来源历史。"""
        if target_kind not in ('proof', 'confirmation'):
            raise ValueError(f'撤销目标类型无效：{target_kind}')
        table, col = ('proofs', 'proof_id') if target_kind == 'proof' else (
            'confirmations', 'confirmation_id')
        if self.conn.execute(f'SELECT 1 FROM {table} WHERE {col}=?',
                             (target_id,)).fetchone() is None:
            raise ValueError(f'撤销目标不存在：{target_id}')
        rid = 'RV-' + _sha('|'.join(['revoke', target_kind, target_id]))[:10]
        payload = {'revocation_id': rid, 'target_kind': target_kind, 'target_id': target_id,
                   'by': by}
        with self.conn:
            if self._event('revocation', payload, received_at):
                self.conn.execute(
                    'INSERT OR IGNORE INTO revocations(revocation_id,target_kind,target_id,received_at)'
                    ' VALUES(?,?,?,?)',
                    (rid, target_kind, target_id, received_at or _now()))
        return rid

    def reactivate(self, source, local_id, identity_id, from_date, by='system', note='',
                   received_at=None):
        """编号重新启用记录：声明本地编号的新使用实例，不连接前后两人。"""
        row = self.conn.execute(
            'SELECT * FROM identities WHERE identity_id=?', (identity_id,)).fetchone()
        if row is None:
            raise ValueError(f'实例不存在：{identity_id}')
        if row['source'] != source or row['local_id'] != local_id:
            raise ValueError('编号与实例不符')
        if row['from_date'] != from_date:
            raise ValueError('重新启用日期须等于新实例开始日期')
        rid = 'RA-' + _sha('|'.join(['reactivate', source, local_id, identity_id,
                                     from_date, note]))[:10]
        payload = {'reactivation_id': rid, 'source': source, 'local_id': local_id,
                   'identity_id': identity_id, 'from_date': from_date, 'by': by, 'note': note}
        with self.conn:
            if self._event('reactivation', payload, received_at):
                self.conn.execute(
                    'INSERT OR IGNORE INTO reactivations VALUES(?,?,?,?,?,?,?)',
                    (rid, source, local_id, identity_id, from_date, note,
                     received_at or _now()))
        return rid

    # ---------------------------------------------------------------- 裁定

    def pending(self):
        """当前证据状态下的待裁定关联（实时计算，无需先重建）。"""
        _, conflicts = rb.current_resolution(self._load_state())
        return conflicts

    def rule(self, pending_id, decision, from_date, until_date=None, faulty=None,
             by='system', note='', received_at=None):
        """业务人员对待裁定关联提交裁定。

        confirm_same：确认两实例同人，中和该组合中的互斥证明；
        confirm_different：确认互斥成立，否决争议路径上的 same 证据
        （可用 faulty 指定具体错误证据，否则否决整条争议路径）。
        """
        if decision not in ('confirm_same', 'confirm_different'):
            raise ValueError(f'裁定类型无效：{decision}')
        _check_period(from_date, until_date)
        items = {c['pending_id']: c for c in self.pending()}
        if pending_id not in items:
            raise ValueError(f'待裁定关联不存在或已关闭：{pending_id}')
        item = items[pending_id]
        if decision == 'confirm_same':
            overrides, rejects = [item['different_edge']], []
        else:
            rejects = sorted(faulty) if faulty else list(item['same_path_edges'])
            bad = set(rejects) - set(item['same_path_edges'])
            if bad:
                raise ValueError('指定的证据不在该关联的争议路径上：' + ','.join(sorted(bad)))
            overrides = []
        rid = 'RL-' + _sha('|'.join(['rule', pending_id, decision, from_date,
                                     str(until_date), ','.join(overrides),
                                     ','.join(rejects), by, note]))[:10]
        payload = {'ruling_id': rid, 'pending_id': pending_id, 'decision': decision,
                   'left': item['left'], 'right': item['right'],
                   'from_date': from_date, 'until_date': until_date,
                   'overrides': overrides, 'rejects': rejects, 'by': by, 'note': note}
        with self.conn:
            if self._event('ruling', payload, received_at):
                self.conn.execute(
                    'INSERT OR IGNORE INTO rulings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (rid, pending_id, decision, item['left'], item['right'],
                     from_date, until_date, json.dumps(overrides), json.dumps(rejects),
                     by, note, received_at or _now()))
        return rid

    # ---------------------------------------------------------------- 重建发布

    def rebuild(self, note='', fault_hook=None):
        """重建并原子发布新版本。

        与当前版本相比证据状态未变化时不产生新版本（重放安全）；若证据
        状态回到了某个历史版本，仍发布新版本——其人员变化行描述从当前
        版本出发的迁移（如旧合并被拆开），下游才能追踪。
        fault_hook 仅供测试：在版本指针切换前调用，用于模拟中途失败。
        """
        state = self._load_state()
        built, extras = rb.build_version(state)
        h = rb.content_hash(built)
        cur = self.current_version()
        if cur is not None:
            row = self.conn.execute(
                'SELECT content_hash FROM versions WHERE version=?', (cur,)).fetchone()
            if row and row[0] == h:
                return {'version': cur, 'published': False, 'content_hash': h,
                        'warnings': extras['warnings']}
        with self.conn:
            (v,) = self.conn.execute(
                'SELECT COALESCE(MAX(version),0)+1 FROM versions').fetchone()
            self.conn.execute(
                'INSERT INTO versions(version,content_hash,note,created_at) VALUES(?,?,?,?)',
                (v, h, note, _now()))
            for table, rows in built.items():
                cols = VERSIONED_COLUMNS[table]
                sql = f"INSERT INTO {table}(version,{','.join(cols)}) VALUES(?{',?' * len(cols)})"
                self.conn.executemany(sql, [(v, *r) for r in rows])
            for did, person in extras['baselines_to_set'].items():
                self.conn.execute(
                    'UPDATE deliveries SET baseline_person=?, baseline_version=?'
                    ' WHERE delivery_id=?',
                    (json.dumps(person, ensure_ascii=False), v, did))
            if fault_hook is not None:
                fault_hook()
            self.conn.execute(
                'INSERT INTO meta(key,value) VALUES(?,?)'
                ' ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('current_version', str(v)))
        return {'version': v, 'published': True, 'content_hash': h,
                'warnings': extras['warnings'],
                'counts': {t: len(r) for t, r in built.items()}}

    def _load_state(self):
        def rows(sql, args=()):
            return [dict(r) for r in self.conn.execute(sql, args)]

        state = {
            'identities': rows('SELECT * FROM identities'),
            'proofs': rows('SELECT * FROM proofs'),
            'confirmations': rows('SELECT * FROM confirmations'),
            'revocations': rows('SELECT * FROM revocations'),
            'rulings': rows('SELECT * FROM rulings'),
            'services': rows('SELECT * FROM services'),
            'households': rows('SELECT * FROM households'),
            'deliveries': rows('SELECT * FROM deliveries'),
            'permissions': rows('SELECT * FROM permissions'),
            'reactivations': rows('SELECT * FROM reactivations'),
        }
        prev_persons, prev_pending = {}, {}
        cur = self.current_version()
        if cur is not None:
            for r in self.conn.execute('SELECT * FROM dim_person WHERE version=?', (cur,)):
                prev_persons[r['person_id']] = {
                    'members': json.loads(r['members']), 'names': json.loads(r['names']),
                    'sources': json.loads(r['sources']), 'periods': {}}
            for r in self.conn.execute(
                    'SELECT * FROM dim_person_member WHERE version=?', (cur,)):
                prev_persons[r['person_id']]['periods'][r['identity_id']] = [
                    r['inst_from'], r['inst_until']]
            for r in self.conn.execute(
                    "SELECT * FROM pending_item WHERE version=? AND status='open'", (cur,)):
                prev_pending[r['pending_id']] = {
                    'kind': r['kind'], 'left': r['left_id'], 'right': r['right_id'],
                    'evidence': r['evidence']}
        state['prev_persons'] = prev_persons
        state['prev_open_pending'] = prev_pending
        baselines = {}
        for r in self.conn.execute(
                'SELECT delivery_id,baseline_person,baseline_version FROM deliveries'
                ' WHERE baseline_person IS NOT NULL'):
            baselines[r['delivery_id']] = {
                'person': json.loads(r['baseline_person']), 'version': r['baseline_version']}
        state['delivery_baselines'] = baselines
        return state

    # ---------------------------------------------------------------- 按版本查询

    def current_version(self):
        r = self.conn.execute("SELECT value FROM meta WHERE key='current_version'").fetchone()
        return int(r[0]) if r else None

    def _v(self, version):
        v = version if version is not None else self.current_version()
        if v is None:
            raise ValueError('尚未发布任何版本')
        return v

    def versions(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM versions ORDER BY version')]

    def persons(self, version=None):
        v = self._v(version)
        out = []
        for r in self.conn.execute(
                'SELECT * FROM dim_person WHERE version=? ORDER BY person_id', (v,)):
            out.append({'person_id': r['person_id'], 'names': json.loads(r['names']),
                        'sources': json.loads(r['sources']),
                        'members': json.loads(r['members'])})
        return out

    def person(self, person_id, version=None):
        v = self._v(version)
        r = self.conn.execute(
            'SELECT * FROM dim_person WHERE version=? AND person_id=?',
            (v, person_id)).fetchone()
        if r is None:
            raise ValueError(f'版本 {v} 中不存在人员 {person_id}')
        members = []
        for m in self.conn.execute(
                'SELECT * FROM dim_person_member WHERE version=? AND person_id=?'
                ' ORDER BY identity_id', (v, person_id)):
            members.append({'identity_id': m['identity_id'], 'link_basis': m['link_basis'],
                            'covered_segments': json.loads(m['covered_segments']),
                            'from_date': m['inst_from'], 'until_date': m['inst_until']})
        return {'person_id': person_id, 'names': json.loads(r['names']),
                'sources': json.loads(r['sources']), 'members': members}

    def services(self, version=None, person_id=None, ambiguous_only=False):
        v = self._v(version)
        sql = 'SELECT * FROM fact_service WHERE version=?'
        args = [v]
        if person_id is not None:
            sql += ' AND person_id=?'
            args.append(person_id)
        if ambiguous_only:
            sql += ' AND ambiguous=1'
        sql += ' ORDER BY service_id'
        out = []
        for r in self.conn.execute(sql, args):
            out.append({'service_id': r['service_id'], 'date': r['date'],
                        'source_identity_id': r['source_identity_id'],
                        'effective_identity_id': r['effective_identity_id'],
                        'person_id': r['person_id'], 'basis': r['basis'],
                        'ambiguous': bool(r['ambiguous']),
                        'reasons': json.loads(r['reasons']),
                        'candidates': json.loads(r['candidates']),
                        'dispute_id': r['dispute_id'],
                        'household_claim': r['household_claim']})
        return out

    def changes(self, version=None):
        v = self._v(version)
        out = []
        for r in self.conn.execute(
                'SELECT * FROM person_change WHERE version=? ORDER BY person_id', (v,)):
            out.append({'person_id': r['person_id'], 'change_kind': r['change_kind'],
                        'predecessors': json.loads(r['predecessors']),
                        'successors': json.loads(r['successors']), 'note': r['note']})
        return out

    def pending_items(self, version=None):
        v = self._v(version)
        out = []
        for r in self.conn.execute(
                'SELECT * FROM pending_item WHERE version=? ORDER BY pending_id', (v,)):
            out.append({'pending_id': r['pending_id'], 'kind': r['kind'],
                        'left': r['left_id'], 'right': r['right_id'],
                        'evidence': json.loads(r['evidence']), 'status': r['status'],
                        'resolved_by': r['resolved_by']})
        return out

    def partner_days(self, partner, version=None):
        v = self._v(version)
        out = []
        for r in self.conn.execute(
                'SELECT * FROM partner_day WHERE version=? AND partner=?'
                ' ORDER BY identity_id, service_id', (v, partner)):
            out.append({'partner': r['partner'], 'identity_id': r['identity_id'],
                        'service_id': r['service_id'], 'date': r['date'],
                        'visible': json.loads(r['visible']),
                        'permission_id': r['permission_id']})
        return out

    def settlements(self, version=None):
        v = self._v(version)
        deliveries, replacements = [], []
        for r in self.conn.execute(
                'SELECT * FROM settlement_delivery WHERE version=? ORDER BY delivery_id', (v,)):
            deliveries.append({'delivery_id': r['delivery_id'], 'service_id': r['service_id'],
                               'old_person_ref': r['old_person_ref'], 'partner': r['partner'],
                               'current_person_id': r['current_person_id'],
                               'baseline_person_id': r['baseline_person_id'],
                               'status': r['status']})
        for r in self.conn.execute(
                'SELECT * FROM settlement_replacement WHERE version=?'
                ' ORDER BY replacement_id', (v,)):
            replacements.append({'replacement_id': r['replacement_id'],
                                 'delivery_id': r['delivery_id'],
                                 'old_person_ref': r['old_person_ref'],
                                 'baseline_person_id': r['baseline_person_id'],
                                 'current_person_id': r['current_person_id'],
                                 'reason': r['reason']})
        return {'deliveries': deliveries, 'replacements': replacements}

    def candidates(self, version=None):
        v = self._v(version)
        return [dict(r) for r in self.conn.execute(
            'SELECT left_id,right_id,clue,detail,status FROM candidate_pair'
            ' WHERE version=? ORDER BY left_id,right_id,clue', (v,))]

    def households(self, version=None):
        v = self._v(version)
        out = []
        for r in self.conn.execute(
                'SELECT * FROM fact_household WHERE version=? ORDER BY evidence_id', (v,)):
            out.append({'evidence_id': r['evidence_id'], 'household_id': r['household_id'],
                        'person_id': r['person_id'], 'confirmed': bool(r['confirmed']),
                        'from_date': r['from_date'], 'until_date': r['until_date'],
                        'via_instance': r['via_instance']})
        return out
