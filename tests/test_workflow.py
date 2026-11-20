"""端到端工作流测试：持久化接收、裁定、重建与按版本查询。"""
from pathlib import Path
import hashlib
import json
import shutil
import tempfile
import unittest

from carelink import CareLink
from carelink.resolve import person_id_for

ROOT = Path(__file__).resolve().parents[1]
ALL_STEMS = ['identities', 'proofs', 'services', 'households',
             'deliveries', 'permissions', 'revocations']
NO_REVOCATIONS = [s for s in ALL_STEMS if s != 'revocations']
NO_PROOFS = [s for s in NO_REVOCATIONS if s != 'proofs']


def pid(*members):
    return person_id_for(list(members))


class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cl = CareLink(Path(self.tmp.name) / 'state.db')
        self.addCleanup(self.cl.close)

    def intake(self, stems):
        return self.cl.intake(ROOT, only=stems)

    def make_root(self, mutate):
        """复制资料包、修改内容并重算目录指纹，用于模拟来源更新或损坏。"""
        d = Path(tempfile.mkdtemp(dir=self.tmp.name))
        shutil.copytree(ROOT / 'data', d / 'data')
        mutate(d)
        cat_p = d / 'data/catalog.json'
        cat = json.loads(cat_p.read_text(encoding='utf-8'))
        for e in cat['files']:
            e['sha256'] = hashlib.sha256((d / e['path']).read_bytes()).hexdigest()
        cat_p.write_text(json.dumps(cat, ensure_ascii=False), encoding='utf-8')
        return d

    # ---------------------------------------------------------------- 接收与重放

    def test_intake_replay_adds_nothing(self):
        r1 = self.intake(ALL_STEMS)
        self.assertEqual(len(r1['applied']), 7)
        r2 = self.intake(ALL_STEMS)
        self.assertEqual(r2['applied'], [])
        self.assertEqual(len(r2['replayed']), 7)
        v1 = self.cl.rebuild()
        self.assertTrue(v1['published'])
        persons = self.cl.persons()
        services = self.cl.services()
        v2 = self.cl.rebuild()
        self.assertFalse(v2['published'], '证据未变化不应产生新版本')
        self.assertEqual(v1['version'], v2['version'])
        self.assertEqual(self.cl.persons(), persons)
        self.assertEqual(self.cl.services(), services)

    def test_intake_invalid_reference_rolls_back(self):
        def mutate(d):
            p = d / 'data/proofs.json'
            rows = json.loads(p.read_text(encoding='utf-8'))
            rows[0]['right'] = 'ZZ'
            p.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
        bad = self.make_root(mutate)
        with self.assertRaises(Exception):
            self.cl.intake(bad)
        n = self.cl.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        self.assertEqual(n, 0, '接收失败不能留下部分事件')

    # ---------------------------------------------------------------- 候选线索

    def test_candidates_never_auto_confirm(self):
        self.intake(NO_PROOFS)
        self.cl.rebuild()
        clues = {(c['left_id'], c['right_id'], c['clue']) for c in self.cl.candidates()}
        self.assertIn(('A1', 'B7', 'name_match'), clues)
        self.assertIn(('A1N', 'C4', 'name_match'), clues)
        self.assertIn(('A1', 'A1N', 'local_id_reuse'), clues)
        for c in self.cl.candidates():
            self.assertEqual(c['status'], 'clue_only')
        # 姓名相同只是线索：A1 与 B7 仍是各自独立的人员
        self.assertIn(pid('A1'), {p['person_id'] for p in self.cl.persons()})
        self.assertIn(pid('B7'), {p['person_id'] for p in self.cl.persons()})

    # ---------------------------------------------------------------- 冲突与待裁定

    def test_conflict_goes_pending_not_merged(self):
        self.intake(NO_REVOCATIONS)
        self.cl.rebuild()
        pending = self.cl.pending()
        self.assertEqual(len(pending), 1)
        item = pending[0]
        self.assertEqual({item['left'], item['right']}, {'A1', 'C4'})
        self.assertEqual(item['different_edge'], 'E3')
        self.assertEqual(item['same_path_edges'], ['E1', 'E2'])
        # 争议边不合并：A1/B7/C4 各自独立，不存在大合并人员
        persons = {p['person_id'] for p in self.cl.persons()}
        self.assertIn(pid('A1'), persons)
        self.assertIn(pid('B7'), persons)
        self.assertIn(pid('C4'), persons)
        self.assertNotIn(pid('A1', 'B7', 'C4'), persons)
        # 受影响服务保留原来源身份与候选解释，不随便指派人员
        ambiguous = {s['service_id']: s for s in self.cl.services(ambiguous_only=True)}
        self.assertIn('S1', ambiguous)
        self.assertIsNone(ambiguous['S1']['person_id'])
        self.assertEqual(ambiguous['S1']['source_identity_id'], 'B7')
        interp = {c['interpretation'] for c in ambiguous['S1']['candidates']}
        self.assertEqual(interp, {'confirmed_evidence_only', 'all_same_evidence_unconfirmed'})
        self.assertEqual(ambiguous['S1']['dispute_id'], item['pending_id'])
        # 未受影响的服务正常归属
        by_id = {s['service_id']: s for s in self.cl.services()}
        self.assertEqual(by_id['S3']['person_id'], pid('B8'))
        self.assertEqual(by_id['S17']['person_id'], pid('A1N'))

    def test_revocation_reviews_only_dependents(self):
        self.intake(NO_REVOCATIONS)
        self.cl.rebuild()
        adj = self.cl.pending()[0]['pending_id']
        self.intake(['revocations'])  # R1 撤销 E2
        self.cl.rebuild()
        # 靠 E2 成立的传递关系消失；E1、E3 独立有效
        persons = {p['person_id'] for p in self.cl.persons()}
        self.assertIn(pid('A1', 'B7'), persons)
        self.assertIn(pid('C4'), persons)
        self.assertEqual(self.cl.pending(), [])
        resolved = [p for p in self.cl.pending_items() if p['pending_id'] == adj]
        self.assertEqual(resolved[0]['status'], 'resolved')
        self.assertEqual(resolved[0]['resolved_by'], 'evidence')
        by_id = {s['service_id']: s for s in self.cl.services()}
        self.assertEqual(by_id['S1']['person_id'], pid('A1', 'B7'))
        self.assertEqual(by_id['S1']['basis'], 'independent_anchor')
        self.assertEqual(by_id['S2']['person_id'], pid('C4'))
        self.assertFalse(any(s['ambiguous'] for s in by_id.values()))

    # ---------------------------------------------------------------- 裁定

    def test_ruling_confirm_same_merges(self):
        self.intake(NO_REVOCATIONS)
        adj = self.cl.pending()[0]['pending_id']
        self.cl.rule(adj, 'confirm_same', '2026-01-01', by='clerk')
        self.cl.rebuild()
        persons = {p['person_id'] for p in self.cl.persons()}
        self.assertIn(pid('A1', 'B7', 'C4'), persons)
        self.assertEqual(self.cl.pending(), [])
        with self.assertRaises(ValueError):
            self.cl.rule(adj, 'confirm_same', '2026-01-01')

    def test_ruling_confirm_different_keeps_independent_proof(self):
        self.intake(NO_REVOCATIONS)
        self.cl.rebuild()  # 先发布，待裁定关联进入版本化记录
        adj = self.cl.pending()[0]['pending_id']
        with self.assertRaises(ValueError):
            self.cl.rule(adj, 'confirm_different', '2026-01-01', faulty=['E9'])
        self.cl.rule(adj, 'confirm_different', '2026-01-01', faulty=['E2'], by='clerk')
        self.cl.rebuild()
        persons = {p['person_id'] for p in self.cl.persons()}
        self.assertIn(pid('A1', 'B7'), persons, 'E1 独立有效')
        self.assertIn(pid('C4'), persons)
        resolved = [p for p in self.cl.pending_items() if p['pending_id'] == adj]
        self.assertEqual(resolved[0]['resolved_by'], 'ruling')

    def test_staff_confirmation_with_period(self):
        self.intake(NO_PROOFS)
        self.cl.rebuild()
        cid = self.cl.confirm('A1', 'B7', '2026-01-01', by='clerk',
                              received_at='2026-11-05T10:00:00+08:00')
        again = self.cl.confirm('A1', 'B7', '2026-01-01', by='clerk',
                                received_at='2026-11-06T10:00:00+08:00')
        self.assertEqual(cid, again, '重复提交同一确认应幂等')
        self.cl.rebuild()
        self.assertIn(pid('A1', 'B7'), {p['person_id'] for p in self.cl.persons()})
        # 撤销确认后回到各自独立
        self.cl.revoke('confirmation', cid)
        self.cl.rebuild()
        self.assertIn(pid('A1'), {p['person_id'] for p in self.cl.persons()})
        with self.assertRaises(ValueError):
            self.cl.confirm('A1', 'ZZ', '2026-01-01')
        with self.assertRaises(ValueError):
            self.cl.confirm('A1', 'B7', '2026-01-01', until_date='2026-01-01')

    # ---------------------------------------------------------------- 编号复用

    def test_id_reuse_does_not_carry_services(self):
        self.intake(ALL_STEMS)
        self.cl.rebuild()
        person_a1n = self.cl.person(pid('A1N'))
        self.assertEqual([m['identity_id'] for m in person_a1n['members']], ['A1N'])
        by_id = {s['service_id']: s for s in self.cl.services()}
        self.assertEqual(by_id['S17']['person_id'], pid('A1N'))
        old_services = [s for s in self.cl.services(person_id=pid('A1', 'B7'))]
        self.assertNotIn('S17', {s['service_id'] for s in old_services})

    def test_reactivation_record(self):
        self.intake(ALL_STEMS)
        with self.assertRaises(ValueError):
            self.cl.reactivate('A', '1', 'A1N', '2026-10-01')  # 日期不符
        with self.assertRaises(ValueError):
            self.cl.reactivate('A', '9', 'A1N', '2026-11-01')  # 编号不符
        self.cl.reactivate('A', '1', 'A1N', '2026-11-01', by='clerk')
        self.cl.rebuild()
        clues = {(c['left_id'], c['right_id']) for c in self.cl.candidates()
                 if c['clue'] == 'local_id_reuse'}
        self.assertNotIn(('A1', 'A1N'), clues, '已有重新启用记录的复用边界不再是线索')

    # ---------------------------------------------------------------- 拆分与归属

    def test_split_attributes_per_service_not_by_date(self):
        self.intake(ALL_STEMS)
        self.cl.rebuild()
        self.assertIn(pid('A1', 'B7'), {p['person_id'] for p in self.cl.persons()})
        self.cl.revoke('proof', 'E1')  # 拆开旧合并
        self.cl.rebuild()
        by_id = {s['service_id']: s for s in self.cl.services()}
        # 带独立证件证明的 S1 跟随锚点 A1，而不是登记实例 B7
        self.assertEqual(by_id['S1']['person_id'], pid('A1'))
        self.assertEqual(by_id['S1']['basis'], 'independent_anchor')
        # 只能依据当时机构编号的各自回到登记实例
        self.assertEqual(by_id['S5']['person_id'], pid('B7'))
        self.assertEqual(by_id['S4']['person_id'], pid('A1'))
        # 不是按服务日期一刀切：同一旧人员 10 月的服务按证据分属两人
        kinds = {c['person_id']: c['change_kind'] for c in self.cl.changes()}
        self.assertEqual(kinds[pid('A1')], 'split')
        self.assertEqual(kinds[pid('B7')], 'split')
        self.assertEqual(kinds[pid('A1', 'B7')], 'retired')

    # ---------------------------------------------------------------- 家庭与授权

    def test_household_not_auto_confirmed_by_merge(self):
        self.intake(ALL_STEMS)
        self.cl.rebuild()
        rows = self.cl.households()
        confirmed = [r for r in rows if r['confirmed']]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]['person_id'], pid('A1', 'B7'))
        self.assertEqual(confirmed[0]['via_instance'], 'A1')
        self.assertEqual((confirmed[0]['from_date'], confirmed[0]['until_date']),
                         ('2026-01-01', '2026-11-01'))
        claim = [r for r in rows if not r['confirmed']]
        self.assertEqual(len(claim), 1)
        self.assertEqual(claim[0]['via_instance'], 'B8')
        self.assertFalse(claim[0]['confirmed'], '未确认成员不能借同人关系自动确认')

    def test_permission_not_expanded_by_merge(self):
        self.intake(ALL_STEMS)
        self.cl.rebuild()
        days = self.cl.partner_days('A')
        self.assertEqual({d['service_id'] for d in days}, {'S4', 'S8', 'S12', 'S16'})
        self.assertTrue(all(d['identity_id'] == 'A1' for d in days))
        self.assertTrue(all(set(d['visible']) == {'date'} for d in days))
        # 拆开后授权仍然只看授权实例，不随人员变化扩张或收缩
        self.cl.revoke('proof', 'E1')
        self.cl.rebuild()
        self.assertEqual(self.cl.partner_days('A'), days)

    # ---------------------------------------------------------------- 结算引用

    def test_delivery_keeps_old_ref_and_issues_replacement(self):
        self.intake(NO_REVOCATIONS)
        self.cl.rebuild()
        d1 = self.cl.settlements()['deliveries'][0]
        self.assertEqual(d1['old_person_ref'], 'legacy-P9')
        self.assertIsNone(d1['baseline_person_id'], '当时解释为无法确认归属')
        self.assertEqual(d1['status'], 'unresolved')
        self.intake(['revocations'])
        self.cl.rebuild()
        s = self.cl.settlements()
        d1 = s['deliveries'][0]
        self.assertEqual(d1['old_person_ref'], 'legacy-P9', '原编号保留')
        self.assertEqual(d1['current_person_id'], pid('A1', 'B7'))
        self.assertEqual(d1['status'], 'diverged')
        self.assertEqual(len(s['replacements']), 1)
        repl = s['replacements'][0]
        self.assertEqual(repl['old_person_ref'], 'legacy-P9')
        self.assertIsNone(repl['baseline_person_id'])
        self.assertEqual(repl['current_person_id'], pid('A1', 'B7'))
        self.assertEqual(repl['reason'], 'resolved_after_unresolved')
        # 替代关联不重新产生服务
        self.assertEqual(len(self.cl.services()), 17)

    # ---------------------------------------------------------------- 原子发布

    def test_failed_rebuild_leaves_no_mixed_state(self):
        self.intake(ALL_STEMS)
        v1 = self.cl.rebuild()['version']
        persons_v1 = self.cl.persons()

        def boom():
            raise RuntimeError('模拟中途失败')
        self.cl.revoke('proof', 'E1')
        with self.assertRaises(RuntimeError):
            self.cl.rebuild(fault_hook=boom)
        self.assertEqual(self.cl.current_version(), v1)
        self.assertEqual(self.cl.persons(), persons_v1, '失败不能留下新维表配旧外键')
        n = self.cl.conn.execute(
            'SELECT COUNT(*) FROM dim_person WHERE version>?', (v1,)).fetchone()[0]
        self.assertEqual(n, 0)
        v2 = self.cl.rebuild()
        self.assertTrue(v2['published'], '回滚后同一证据状态应能正常发布')

    # ---------------------------------------------------------------- 变化分类与版本查询

    def test_change_feed_distinguishes_merge_split_info(self):
        self.intake(NO_REVOCATIONS)
        self.cl.rebuild()
        self.assertTrue(all(c['change_kind'] == 'new' for c in self.cl.changes()))
        self.intake(['revocations'])
        self.cl.rebuild()
        kinds = {c['person_id']: c for c in self.cl.changes()}
        merge = kinds[pid('A1', 'B7')]
        self.assertEqual(merge['change_kind'], 'merge')
        self.assertEqual(set(merge['predecessors']), {pid('A1'), pid('B7')})
        self.assertEqual(kinds[pid('A1')]['change_kind'], 'retired')
        self.assertEqual(kinds[pid('A1')]['successors'], [pid('A1', 'B7')])

        def mutate(d):
            p = d / 'data/identities.json'
            rows = json.loads(p.read_text(encoding='utf-8'))
            rows[0]['name'] = '同名甲改'
            p.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
        updated = self.make_root(mutate)
        self.cl.intake(updated, only=['identities'])
        self.cl.rebuild()
        kinds = {c['person_id']: c['change_kind'] for c in self.cl.changes()}
        self.assertEqual(kinds[pid('A1', 'B7')], 'info_completion')
        self.assertIn('同名甲改', self.cl.person(pid('A1', 'B7'))['names'])

    def test_versions_are_queryable_and_stable(self):
        self.intake(NO_REVOCATIONS)
        v1 = self.cl.rebuild()['version']
        snap1 = self.cl.persons(version=v1)
        self.intake(['revocations'])
        v2 = self.cl.rebuild()['version']
        self.cl.revoke('proof', 'E1')
        v3 = self.cl.rebuild()['version']
        self.assertEqual((v1, v2, v3), (1, 2, 3))
        self.assertEqual(self.cl.persons(version=v1), snap1, '旧版本状态永久可查')
        self.assertEqual(len(self.cl.versions()), 3)
        self.assertIn(pid('A1', 'B7'),
                      {p['person_id'] for p in self.cl.persons(version=v2)})
        self.assertNotIn(pid('A1', 'B7'),
                         {p['person_id'] for p in self.cl.persons(version=v3)})
        with self.assertRaises(ValueError):
            self.cl.person('P-000000000000', version=v3)


if __name__ == '__main__':
    unittest.main()
