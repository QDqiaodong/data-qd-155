# data-qd-155 · 多服务台排队回放

Deterministic FIFO discrete-event service queue analysis。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/queue-replay --source fixtures/input.json --output run/report.json
.venv/bin/queue-cost --source fixtures/cost/source.json --output run/cost.json
.venv/bin/queue-reconcile --source fixtures/reconcile/source.json --output run/reconcile.json
.venv/bin/queue-sla --source fixtures/sla/source.json --output run/sla.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/queue_replay/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 当前能力和样例
# FIFO multi-server queue replay v1

服务运营分析者回放最多20000个唯一作业id、arrival(0..10^12整数tick)、service(0..10^9整数tick)，servers=1..100。按arrival/id排列为明确FIFO到达顺序；同刻按ID打破，不隐含输入行顺序。
每单分给可用时刻最早的服务台，同可用时刻按server编号；start=max(arrival,server_ready)，非抢占服务。若多个服务台在到达前已经空闲，选择空闲最早者。零时长作业合法且仍按上述顺序处理。
输出每单start/finish/wait/server及平均/最长等待，server busy和利用率。观测范围从首到达到最后完成，不把此前空闲计入；无作业/零范围利用率为null。样例A[0,5)台0、B[0,2)台1、C[2,5)台1、D[5,6)台0；平均等待1、最长3，利用率1和5/6。
无随机到达推断、放弃、优先抢占、路由网络、排班窗口或真实服务控制。这是有明确到达数据的离线运营分析，不以泊松或稳态假设代替观测。

## 滚动派工工作台 queue-planner

在 FIFO 回放之外新增持久化的滚动派工工作台，命令为
`python -m queue_planner.cli {init,apply,plan}`，仅用 Python 标准库与 SQLite。
它把待处理/运行中/已完成作业分开保存，原子接收 `load`/`observe`/`start`
请求，并发布可撤销重排的准时接单建议。请求按 `request_id` 幂等、按
`expected_revision` 做乐观并发控制，错误退出码 2；规划器对最多 12 个待处理
作业、1..3 台做精确三级联合优化（value、等待、id 排序元组），不复用 FIFO
最早空闲台算法。完整字段、事务边界、现场职责（预测释放 vs 实际完工）与求解/
剪枝依据见 [queue-planner 契约](docs/planner-contract.md)。

```sh
PYTHONPATH=src python3 -m queue_planner.cli init   --db run/wb.sqlite --source init.json
PYTHONPATH=src python3 -m queue_planner.cli apply  --db run/wb.sqlite --source request.json
PYTHONPATH=src python3 -m queue_planner.cli plan   --db run/wb.sqlite --output run/plan.json
```

`tests/planner_oracle.py` 是独立于生产 DP 的穷举 oracle；
`tests/test_exact_planner.py` 用它做小规模三级目标比对，覆盖同价值不同子集、
稀缺台抢占、为未来高价值作业故意空闲、零时长边界、贪心会输的自制 fixture 以及
平移到 10^12 附近后相对方案不变。`tests/test_lifecycle.py` 覆盖重开持久化、
预测到点不自动完工、实际完工后重规划、零单同 tick 续开工、过期 start 拒绝与
两进程写竞争。

## 跨设备时钟重建 queue-observe

只读运营复盘命令 `python -m queue_observe.cli --source LOG.json --baseline
REPLAY.json --output REPORT.json`，在 FIFO 回放报告之外读取实际到达/开工/完工
日志，输出每单 wait/service/sojourn 在全部约束共同可行域上的紧整数区间、达到
区间端点的完整 clock→offset 赋值见证，以及与 baseline 预测的偏差。跨设备日志
共享未知整数 offset，约束包括 clock 界、事件真实时间 [0,10^12]、跨时钟标定、
作业生命周期和同台 service_seq 相邻次序；求解用差分约束最短路（不逐 tick、
不枚举 offset 组合），无解时给出闭合负环证据。

```sh
PYTHONPATH=src python3 -m queue_observe.cli \
    --source fixtures/observe/observation-log.json \
    --baseline fixtures/observe/baseline.json \
    --output run/observe.json
```

完整字段、constraint_id、见证与负环定义见
[queue-observe 契约](docs/observe-contract.md)。

`tests/observe_oracle.py` 是独立小整数穷举 oracle（不导入生产可行性代码），
`tests/test_observe.py` 用它核验查询区间、端点见证与矛盾环，并覆盖跨 clock
抵消、同台次序收紧、三环矛盾、未查询作业约束、参考 clock 非首项、负 tick/
负 offset、真实时间上界、零时长/等时边界、空事件与巨大 clock 范围。既有
`queue-replay`、`queue-planner` 命令与结果保持不变。

## 工时费用核算 queue-cost

面向服务中心财务的精确核算命令 `python -m queue_cost.cli --source COST.json
--output REPORT.json`：把每个已完成服务区间按合同有效费率边界切成左闭右开
子段，以 `Fraction` 精确计算每段与每单费用，仅在**账单总额**处做一次
“最接近、恰半向上”的舍入。同一 `(service_code, server)` 的费率时段不得重叠；
工作区间必须被连续覆盖，空档拒绝而不外推；工作开始恰在切换点只用新费率，
结束恰在切换点不用新费率。报告含 `schema_version=1`、输入原字节 SHA256、
逐工作单 `rate_id/from_tick/to_tick/exact_minor_units` 子段与精确合计，以及按
`invoice_id` 汇总（含 `rounded_minor_units`）的账单。标识符、整数、币种与
8MB/重复键/NaN/同路径/符号链接等安全边界沿用既有命令。

```sh
PYTHONPATH=src python3 -m queue_cost.cli \
    --source fixtures/cost/source.json --output run/cost.json
```

字段、Fraction 规范形式、半开区间、舍入规则与手算例子见
[queue-cost 契约](docs/cost-contract.md)。`tests/cost_oracle.py` 是独立
有理数 oracle（不导入生产核算代码）；`tests/test_cost.py` 覆盖跨边界切分、
恰好落在边界、零时长、零费率、大分子/分母、超大跨度、账单恰半向上与
“提前逐单舍入会改变总额”的反例、空档/重叠/错台/错码/身份冲突/混币、
空输入、字段错误、重复键、超 8MB、路径与符号链接安全、旧输出保留及输入
重排字节稳定。既有 `queue-replay`、`queue-planner`、`queue-observe` 命令与
结果保持不变。

## 跨源收款对账 queue-reconcile

面向服务中心财务的对账命令 `python -m queue_reconcile.cli --source
RECONCILE.json --output REPORT.json`：账单侧直接核算既有 queue-cost 源
文档（不接受预计算金额），收款侧是最多 300 笔独立收款（独立命名空间、
有限账单准入列表）。在币种一致、金额差不超过容差且准入允许的候选边
上做一对一配对，先最大化配对数、再最小化金额差总和（任意精度整数，
两层目标严格优先，不做权重折叠或浮点近似）。报告给出一份最优见证、
目标值，以及每条账单/收款在**全部**两层最优方案中的可能对端集合与
可未配对性，据此分为 fixed/ambiguous/unmatched 供人工核销；
currency=null 的账单永远无候选、可未配对。算法为单位容量最小费用最
大流（逐最短路增广）加零约化费用残余图强连通分量，多项式时间，不枚
举配对方案、不按金额单位展开。

```sh
PYTHONPATH=src python3 -m queue_reconcile.cli \
    --source fixtures/reconcile/source.json --output run/reconcile.json
```

字段、支持集语义、算法正确性论据与错误行为见
[queue-reconcile 契约](docs/reconcile-contract.md)。
`tests/reconcile_oracle.py` 是独立小规模枚举 oracle（不导入生产匹配代
码）；`tests/test_reconcile.py` 覆盖非最优候选边剔除、交替换配关联歧
义、固定与歧义共存、收款争用、零金额零容差、空侧、币种不符、同名跨
命名空间、10^36 巨大金额、账单级舍入改变配对、300×300 最大规模非唯
一最优、输入重排稳定与旧报告保留。既有 `queue-replay`、`queue-planner`、
`queue-observe`、`queue-cost` 命令与结果保持不变。

## 多来源客户主数据历史身份解析 queue-identity

面向服务中心 CRM、预约和热线导出的时点身份解析命令 `python -m
queue_identity.cli --source IDENTITY.json --output REPORT.json`：静态
客户记录保留 `(source, key)` 双分量身份（最多 64 来源、10000 记录），
人工确认的无向同人关联带 `[from_tick, to_tick)` 有效期（最多 30000
条），时点查询（最多 30000 个，tick 0..10^12）取该时刻有效关联的连
通分量，输出分量最小客户键（纯字符串元组序，与优先级无关）、成员
数、同源冲突隔离状态（`conflict_sources` 全量列出）与逐字段主数据血
缘（`display_name/email/phone` 各自按 `(priority, source, key)` 升序
从非空成员中择一）。算法为离线时间段树加分组完整治理状态的可回滚
并查集：不逐 tick 展开、不逐查询重扫关联或遍历分量，分裂与相邻时
间分支间完整恢复成员数、最小键、来源计数、冲突掩码与字段血缘，而
非只恢复连通关系。

```sh
PYTHONPATH=src python3 -m queue_identity.cli \
    --source fixtures/identity/source.json --output run/identity.json
```

字段、隔离与血缘语义、状态保存/恢复论据、复杂度与错误行为见
[queue-identity 契约](docs/identity-contract.md)。
`tests/identity_oracle.py` 是独立逐查询图遍历 oracle（不导入生产身份
引擎）；`tests/test_identity.py` 覆盖桥接前干净、桥接期间同源冲突、
桥接失效后恢复、边界生效/失效与同 tick 交接、平行关联与环中替代路
径、跨源同键与分隔符碰撞、优先级并列、三字段分属三源、多空字段与孤
立记录、多冲突来源、零关联/零查询/空 records、非法未查询关联，以及
64 来源、10000 记录、29996 关联、30000 查询的可证明构造大例（多次
分裂/再合并、巨大时间跨度、交错失效）。既有 `queue-replay`、
`queue-planner`、`queue-observe`、`queue-cost`、`queue-reconcile` 命令
与结果保持不变。

## 营业时间口径 SLA 计时 queue-sla

面向服务中心 SLA 核算的命令 `python -m queue_sla.cli --source SLA.json
--output REPORT.json`：给定周期营业日历（周期原点固定为 tick 0，窗口可
重叠/相邻/重复并按并集处理）、绝对时间的 `open`/`closed` 例外（closed
始终优先）与每单可交叠、可跨非营业时段的暂停并集，输出每个检查时点实际
消耗的营业 tick、`remaining=max(0,budget-elapsed)`、布尔 `met` 与预算首次
达到的最早整数 `due`（不存在为 `null`，`budget=0` 时为 `opened_at`）。
有效营业集合为 `(P ∪ O) − C`；工单累计是集合测度
`|E| − |E ∩ pauses|`，累计不会为负，闭店与暂停重叠只扣一次，暂停内的
特殊开门不计时间，绝不用暂停或例外总长度直接冲减墙钟时长。

```sh
PYTHONPATH=src python3 -m queue_sla.cli \
    --source fixtures/sla/source.json --output run/sla.json
```

计数采用集合容斥的**闭式**前缀：周期部分按整周期加余数窗口二分，例外与
暂停只作为有限加权区间并集，周期集合 P 仅通过跨度权重出现而从不物化，
因此 `period=1`、`horizon=10^12` 也不逐 tick、不逐周期展开；`due` 在同一
前缀表示上二分反求，计数与到期共用同一集合口径。字段、身份命名空间、
容斥恒等式、复杂度与错误行为见 [queue-sla 契约](docs/sla-contract.md)。
`tests/sla_oracle.py` 是独立逐 tick oracle（不导入生产代码）；
`tests/test_sla.py` 覆盖闭店/暂停部分与完全重叠、特殊开门被暂停覆盖、
同日历两单暂停隔离、预算恰在营业段右端耗尽、空营业集合、budget=0、
horizon 处到期、周期跨界、乱序重叠窗口与例外、period=1 与 10^12 跨度的
不展开压力测试、可证明构造大例，以及重复身份、bool/非法未使用例外、
超 8MB、同路径/硬链接/符号链接与发布失败旧报告保留。既有
`queue-replay`、`queue-planner`、`queue-observe`、`queue-cost`、
`queue-reconcile`、`queue-identity` 命令与结果保持不变。
