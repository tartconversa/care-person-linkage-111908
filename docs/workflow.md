# 业务工作流：接收、裁定、重建、按版本查询

carelink 包把合成来源装入可持续更新的人员维表与服务事实表。核心原则：
错误合并比遗漏合并更严重，因此只有确认级证据连接人员（证据等级见
docs/source-contract.md），含互斥证明的组合一律进入待裁定。

## 状态库结构

单文件 SQLite（默认 carelink.db），四层：

1. events：追加式证据事件日志（接收、同人确认、撤销、编号重新启用、裁定）。
   按内容指纹幂等，重放同一文件或重复提交同一确认都是空操作。
2. 当前来源状态表：identities/proofs/services/households/deliveries/
   permissions 等，由接收事务维护，是重建的输入。
3. 版本化仓表：dim_person、dim_person_member、fact_service、fact_household、
   partner_day、settlement_delivery、settlement_replacement、person_change、
   pending_item、candidate_pair，全部按版本整体写入。
4. meta.current_version：当前版本指针，与版本化表在同一事务内切换。

## 工作流

接收（持久化、幂等）：

    python -m carelink --db state.db intake --root . [--only identities,proofs,...]

先校验资料指纹与引用（support/check_inputs.py），再在同一事务内写事件并
更新来源状态；任一文件无效则整体回滚，不留半个批次。

证据（数据人员）：

    python -m carelink --db state.db confirm A1 B7 --from 2026-01-01 [--until D] --by 工号
    python -m carelink --db state.db revoke proof E1
    python -m carelink --db state.db revoke confirmation CF-xxxx
    python -m carelink --db state.db reactivate A 1 A1N --from 2026-11-01

同人确认必须带适用时段。编号重新启用记录声明本地编号的新使用实例：
新实例不带走上一人的服务，复用边界也不再生成候选线索。

裁定（业务人员）：

    python -m carelink --db state.db pending
    python -m carelink --db state.db rule ADJ-xxxx confirm_same --from 2026-01-01 --by 工号
    python -m carelink --db state.db rule ADJ-xxxx confirm_different --from 2026-01-01 --faulty E2

pending 实时列出待裁定关联（互斥证明组合）。confirm_same 中和该组合中的
互斥证明；confirm_different 否决争议路径上的 same 证据，可用 --faulty
指明具体错误证据，否则否决整条争议路径。裁定只中和它针对的证据，其余
独立证据继续有效。裁定提交即生效于证据状态，重建后进入版本。

重建与发布（原子）：

    python -m carelink --db state.db rebuild [--note 说明]

从证据状态确定性地构建新版本：同一证据状态必然得到相同的人员引用与
内容指纹。与当前版本相比证据状态未变化时不产生新版本（重放安全）。
全部版本化表与版本指针在同一事务内提交；中途失败整体回滚，不会留下
新人员维表配旧事实外键。

按版本查询：

    python -m carelink --db state.db versions
    python -m carelink --db state.db persons [--version N]
    python -m carelink --db state.db person P-xxxx [--version N]
    python -m carelink --db state.db services [--version N] [--person P-xxxx] [--ambiguous]
    python -m carelink --db state.db changes [--version N]
    python -m carelink --db state.db pending-items [--version N]
    python -m carelink --db state.db partner-days A [--version N]
    python -m carelink --db state.db settlements [--version N]
    python -m carelink --db state.db candidates [--version N]
    python -m carelink --db state.db households [--version N]

旧版本状态永久可查；不带 --version 时读当前版本。

## 关键语义

- 人员引用 P-xxxx 由成员实例集合决定：成员不变则引用稳定，可核对、可
  重放。changes 区分 new / info_completion（信息补全）/ merge（两人合一）/
  split（旧合并被拆开）/ retired，并列出前驱与后继。
- 服务事实逐条归属：带独立身份证明的跟随锚点（independent_anchor，优先于
  同人链），其余跟随登记实例（registered_instance）；归属有歧义的保留
  原来源身份与候选解释（person_id 为空），不按日期一刀切，也不为平衡
  总人数随便指派。
- 拆开旧合并时，服务事实的人员外键、家庭关系在各时段的适用性、合作方
  人员日数据在同一版本内一起重新判断；未确认家庭成员不借同人关系自动
  确认。
- 授权按来源实例计算，不经同人关系扩张：确认跨机构同人后，合作方仍只能
  看到原授权实例的字段。
- 已送出的结算引用保留原编号及当时解释（baseline）；当前解释偏离基线时
  发出可核对的替代关联（settlement_replacement），旧引用不被悄悄改指，
  替代关联不重新产生服务。
- 撤销一条证据后，只有靠它成立的传递关系被重审；其余独立证据继续有效。
- candidate_pair 只列线索（姓名相同、编号复用），永不自动确认。

## 示例（基于本包合成数据）

    intake（不含 revocations）→ rebuild        # v1：A1~C4 传递关系与 E3 互斥，
                                              #     进入待裁定，相关服务保留候选解释
    pending                                    # ADJ-xxxx：E3 与 E1,E2 路径冲突
    intake revocations（R1 撤销 E2）→ rebuild  # v2：E1 独立有效，A1=B7 合并；
                                              #     待裁定关闭（resolved_by=evidence）；
                                              #     D1 发出替代关联 legacy-P9 → P(A1,B7)
    revoke proof E1 → rebuild                  # v3：旧合并被拆开；S1 依独立证明归
                                              #     A1，S5 依登记实例归 B7
    reactivate A 1 A1N --from 2026-11-01       # 编号复用边界入档，A1N 不带走 A1 的服务
