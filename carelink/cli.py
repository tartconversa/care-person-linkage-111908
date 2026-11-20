"""命令行入口：python -m carelink --db state.db <子命令>。

子命令对应四条工作流：
  接收  intake
  证据  confirm / revoke / reactivate
  裁定  pending / rule
  重建  rebuild
  查询  versions / persons / person / services / changes / pending-items /
        partner-days / settlements / candidates / households
"""
from __future__ import annotations

import argparse
import json
import sys


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='carelink', description=__doc__)
    parser.add_argument('--db', default='carelink.db', help='状态库路径（SQLite）')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('intake', help='接收来源文件（重放安全）')
    p.add_argument('--root', default='.', help='资料包根目录')
    p.add_argument('--only', help='只接收指定文件主名，逗号分隔')

    sub.add_parser('rebuild', help='重建并原子发布新版本').add_argument('--note', default='')

    p = sub.add_parser('confirm', help='提交带适用时段的同人确认')
    p.add_argument('left'); p.add_argument('right')
    p.add_argument('--from', dest='from_date', required=True)
    p.add_argument('--until', dest='until_date')
    p.add_argument('--by', default='system'); p.add_argument('--note', default='')

    p = sub.add_parser('revoke', help='撤销确认级证据（proof|confirmation）')
    p.add_argument('target_kind', choices=['proof', 'confirmation'])
    p.add_argument('target_id'); p.add_argument('--by', default='system')

    p = sub.add_parser('reactivate', help='编号重新启用记录')
    p.add_argument('source'); p.add_argument('local_id'); p.add_argument('identity_id')
    p.add_argument('--from', dest='from_date', required=True)
    p.add_argument('--by', default='system'); p.add_argument('--note', default='')

    sub.add_parser('pending', help='查看当前待裁定关联')

    p = sub.add_parser('rule', help='提交裁定')
    p.add_argument('pending_id')
    p.add_argument('decision', choices=['confirm_same', 'confirm_different'])
    p.add_argument('--from', dest='from_date', required=True)
    p.add_argument('--until', dest='until_date')
    p.add_argument('--faulty', help='confirm_different 时指定错误证据，逗号分隔')
    p.add_argument('--by', default='system'); p.add_argument('--note', default='')

    sub.add_parser('versions', help='列出版本')

    def query(name, help_text):
        q = sub.add_parser(name, help=help_text)
        q.add_argument('--version', type=int)
        return q

    query('persons', '人员维表')
    p = query('person', '单个人员及成员')
    p.add_argument('person_id')
    p = query('services', '服务事实表')
    p.add_argument('--person'); p.add_argument('--ambiguous', action='store_true')
    query('changes', '人员变化（信息补全/两人合一/拆开/退役）')
    query('pending-items', '版本化的待裁定记录')
    p = query('partner-days', '合作方可用的人员日数据')
    p.add_argument('partner')
    query('settlements', '结算交付与替代关联')
    query('candidates', '候选线索（仅线索，不构成确认）')
    query('households', '家庭关系在各时段的适用性')

    args = parser.parse_args(argv)
    from .store import CareLink
    with CareLink(args.db) as cl:
        if args.cmd == 'intake':
            only = set(args.only.split(',')) if args.only else None
            _emit(cl.intake(args.root, only=only))
        elif args.cmd == 'rebuild':
            _emit(cl.rebuild(note=args.note))
        elif args.cmd == 'confirm':
            _emit({'confirmation_id': cl.confirm(
                args.left, args.right, args.from_date, args.until_date,
                by=args.by, note=args.note)})
        elif args.cmd == 'revoke':
            _emit({'revocation_id': cl.revoke(args.target_kind, args.target_id, by=args.by)})
        elif args.cmd == 'reactivate':
            _emit({'reactivation_id': cl.reactivate(
                args.source, args.local_id, args.identity_id, args.from_date,
                by=args.by, note=args.note)})
        elif args.cmd == 'pending':
            _emit(cl.pending())
        elif args.cmd == 'rule':
            faulty = args.faulty.split(',') if args.faulty else None
            _emit({'ruling_id': cl.rule(
                args.pending_id, args.decision, args.from_date, args.until_date,
                faulty=faulty, by=args.by, note=args.note)})
        elif args.cmd == 'versions':
            _emit(cl.versions())
        elif args.cmd == 'persons':
            _emit(cl.persons(version=args.version))
        elif args.cmd == 'person':
            _emit(cl.person(args.person_id, version=args.version))
        elif args.cmd == 'services':
            _emit(cl.services(version=args.version, person_id=args.person,
                              ambiguous_only=args.ambiguous))
        elif args.cmd == 'changes':
            _emit(cl.changes(version=args.version))
        elif args.cmd == 'pending-items':
            _emit(cl.pending_items(version=args.version))
        elif args.cmd == 'partner-days':
            _emit(cl.partner_days(args.partner, version=args.version))
        elif args.cmd == 'settlements':
            _emit(cl.settlements(version=args.version))
        elif args.cmd == 'candidates':
            _emit(cl.candidates(version=args.version))
        elif args.cmd == 'households':
            _emit(cl.households(version=args.version))
    return 0


if __name__ == '__main__':
    sys.exit(main())
