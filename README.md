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
