# 来源资料约定

独立机构身份、同人及排斥证明、服务与结算引用资料。

所有人员、机构、地址、业务事件及数值均为合成，不来自真实企业或个人。UTF-8 编码，日期为 ISO 8601，时间附明确时区。有效区间左闭右开，空结束值为尚无结束记录，不据此推断未来。金额使用整数分，非整数数量用十进制字符串；未知不是零。

独立身份证明优先于一般同人链。互斥证明触及的传递关系进入待裁定，不能按记录先后取胜；其余独立证据有效。来源实例在适用期内延续，无证明不得跨复用期连接。家庭证据适用于整个来源实例有效段，但未确认成员不能借同人关系自动确认。人工裁定需保留证据与适用时段；结算替代关联解释旧引用，不重新产生服务。

## identities

本地编号可能复用；identity_id是使用实例。

- identity_id：实例
- source：来源
- local_id：本地号
- from_date：开始
- until_date：结束
- name：候选姓名

## proofs

same 是同人确认，different 是排斥证明。

- proof_id：证据
- left：左实例
- right：右实例
- relation：关系
- from_date：开始
- until_date：结束
- received_at：到达

## revocations

撤销证据，不删来源历史。

- revocation_id：事件
- proof_id：证据
- received_at：到达

## services

independent_identity 是服务独立身份证明，无则保留来源候选。

- service_id：服务
- identity_id：登记实例
- date：服务日
- independent_identity：独立证明锚点
- household_claim：未确认家庭声明

## households

家庭确认独立于同人证明。

- evidence_id：证据
- household_id：家庭
- member：来源实例
- confirmed：是否明确确认

## deliveries

已经送出结算的旧引用。

- delivery_id：交付
- service_id：服务
- old_person_ref：原人员引用
- partner：合作方

## permissions

授权不可经同人关系扩张。

- permission_id：授权
- identity_id：来源实例
- partner：受众
- fields：允许字段
- from_date：开始
- until_date：结束
