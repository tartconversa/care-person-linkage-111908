# 跨机构照护身份拆合后的事实归属资料包

独立机构身份、同人及排斥证明、服务与结算引用资料，以及把来源装入可持续
更新的人员维表与服务事实表的业务程序（carelink）。

## 内容

- docs/source-contract.md：来源身份、适用范围、业务含义，以及确认级证据
  与候选线索的区分。
- docs/workflow.md：接收、裁定、重建、按版本查询的操作说明与关键语义。
- data/catalog.json：登记来源文件格式、条数、字段与内容摘要。
- support/check_inputs.py：只检查资料可读性和引用，不生成仓表或业务答案。
- carelink/：业务程序。持久化接收、同人确认/撤销/编号重新启用、待裁定与
  人工裁定、确定性重建与原子发布、按版本查询。
- tests/：资料自检、检查器负向测试与端到端工作流测试。

## 环境与使用

Python 3.14 标准库，无需在线安装依赖。Dockerfile 锁定基础镜像完整摘要；
首次构建需要本地镜像或拉取该摘要。

    python support/check_inputs.py
    python -m unittest discover -s tests -v

业务程序入口（状态库为单文件 SQLite，默认 carelink.db）：

    python -m carelink --db state.db intake --root .
    python -m carelink --db state.db rebuild
    python -m carelink --db state.db pending
    python -m carelink --db state.db persons --version 1

完整工作流与裁定、撤销、拆分、结算替代关联的操作见 docs/workflow.md。

    docker build -t care-person-linkage-inputs .
    docker run --rm --network none care-person-linkage-inputs
    docker run --rm --network none care-person-linkage-inputs python -m unittest discover -s tests -v

容器默认进行资料自检；来源自检只报告输入完整性，业务能力的验收以
tests/test_workflow.py 的端到端测试为准。
