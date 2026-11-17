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

