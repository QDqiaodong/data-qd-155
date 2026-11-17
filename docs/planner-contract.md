# queue-planner 滚动派工工作台 v1

`queue-planner` 在保留既有 `queue-replay` 离线 FIFO 分析能力（命令、输入、
FIFO 结果完全兼容）的基础上，新增一个持久化的滚动派工工作台。它把
**待处理（pending）/ 运行中（running）/ 已完成（done）** 三类作业分开保存，
在同一 SQLite 快照上原子地接收现场观测与开工请求，并输出可撤销、可重排的
准时接单建议。仅使用 Python 标准库（含 SQLite），不连接外部服务。

## 命令

所有输入输出都是严格 JSON：未知/重复字段拒绝，整数拒绝布尔，失败退出码
一律为 2，成功为 0。源字节摘要由现有严格 JSON 读取器取得。

### init — 只创建新库

```
python -m queue_planner.cli init --db DB --source SOURCE
```

SOURCE 严格含 `servers`（1..3 的整数，台号 0..servers-1）与
`now`（0..10^12 的整数），初始 `revision=0`。已存在的库、DB 符号链接、
DB 与输入/报告同路径一律拒绝。除首次成功 init 外不输出任何数据库副本。

### apply — 原子请求

```
python -m queue_planner.cli apply --db DB --source SOURCE
```

信封严格含 `request_id`（沿用 ident 规则，<=128 非空白字符）、
`expected_revision`（非负整数）、`op`（`load`/`observe`/`start`）、
`payload`。成功在一个 `BEGIN IMMEDIATE` 事务内写入事实、revision+1 与请求
回执，返回 `{"request_id", "revision": 首次成功revision}`。

* 同 `request_id` 且**完全相同源字节**的重放先于 revision 检查，返回首次
  回执且不增加 revision；同 id 但字节不同冲突报错。
* revision 过期、任一非法开工/完工、批内一项非法都整批回滚，数据库内部
  不出现半次变更。
* 进程崩溃后用相同请求重试即可恢复首次回执。

### plan — 只读快照发布

```
python -m queue_planner.cli plan --db DB --output OUTPUT
```

以只读模式打开同一个 SQLite 快照，不改数据库；报告先写临时文件再原子
替换 OUTPUT，发布失败时保留旧报告。同一状态重跑产出字节确定的报告。

## 三类操作的现场职责

### load（只追加事实，不自动开工）

payload 严格含 `jobs`，1..12 项，每项严格含
`id,arrival,service,deadline,value,eligible`：

* `arrival`/`deadline` 为 0..10^12 整数且 `arrival<=deadline`；
  `service` 0..10^9（允许 0，表示本来就无法准时）；
  `value` 1..10^9；`eligible` 为非空、不重复的合法台号数组。
* `id` 全库唯一，已开始/完成的作业不再占待处理名额，也不得重用 id。
* 当前待处理总量 + 新作业数不得超过 12；全库作业累计 <=10000。

### observe（推进时钟与完工事实）

payload 严格含 `now` 与 `finishes`（0..3 项，项严格含
`id,actual_finish`）：

* `now` 单调不减，可只推进时钟、不带完工，且**不会**按预测补造完成。
* 完工 id 不得重复且必须当前是 running；尚未开工/已完成的不得再报完工。
* `actual_start <= actual_finish <= 新 now`，允许提前、延后或迟报到旧 now
  之前。只有 observe 中的实际完工才释放现场占用。
* 运行中作业所在台的**预测**释放时刻为
  `max(now+1, actual_start+service)`；它只是规划边界，不会改变状态。

### start（在当前 revision 的最优建议上提交）

payload 严格含 `jobs`（非空、不重复 id 数组）：

* 每个 id 必须是当前 revision 重求的最优建议中 `start==now` 的作业，且
  对应服务台当前无运行中作业。
* 同台同一 tick 可含若干零 service 单和至多一个正 service 单。零 service
  立即记为 done，`actual_start=actual_finish=now`；正 service 记为 running
  并永久保存台号与 `actual_start=now`。
* 不在建议中、未来时刻、已开工或台位超容都整批拒绝。成功后旧建议失效，
  后续按新 revision 重新规划。

## 精确规划的三级目标

规划器 `queue_planner.planner.optimize` 对至多 12 个待处理作业、1..3 台做
**联合**优化（同时决定接纳子集、台号与开工时刻），不是先贪心选子集再排程：

1. 最大化接纳 value 总和；
2. 最小化接纳作业的总等待（`start-arrival` 之和）；
3. 最小化按 id 字符串排序的 `(id,server,start)` 元组列表，列表按字典序、
   短前缀在前。

每条建议含 `id,server,start,finish`，满足 `finish=start+service<=deadline`、
`start>=max(now,arrival)`、同台正时长区间 `[start,finish)` 互不相交且不早于
该台运行中作业的预测释放。零 service 不占正长度区间，但其 start 不得落在
同台正时长服务区间的严格内部；同台同 tick 可先处理多个零单再开工一个正单，
也可在正单完工 tick 处理零单。允许为未来高价值作业故意空闲。没选中的作业
留在待处理，可在后续 revision 再选；三级目标只计算本次建议中的待处理作业，
已开工/完成作业的价值与等待视为常量不参与选择。所有时间都是整数，规划只
使用 release/arrival/完工事件，不按 tick 枚举，因此不依赖时间值很小。

### 求解策略与剪枝依据

* 逐服务台子集 DP 枚举每个正作业子集的全部可行左打包排程；无零作业时
  左打包时刻 `max(finish,arrival)` 唯一最优（延迟只增等待），存在同服务器
  资格的零作业时再加入零作业 arrival 作为开工候选，使正作业能与零作业共享
  开工边界。左打包不损失最优：等待随开工提前单调下降，而每个可行开工时刻
  都是 release/arrival/前序完工事件，全程不按 tick 枚举。
* 各服务台方案用不相交正作业子集在全局掩码 DP 中合并；零作业在每个存活
  全局状态上独立取字典序最小的可行 `(start,server)`。
* 剪枝只在可证明不改变三级目标时进行：
  * 逐服务台无零时，合并完全相同的 `(finish,wait)` 几何并保留最小元组；
    有零时保留全部几何，因为不同区间边界对应不同零作业可放置性。
  * 全局同一接纳掩码（value 固定）内只保留最小正等待的状态——等待是第二级
    目标，严格更大的等待无论元组如何都不可能赢；相等等待下不同 finish 向量
    与元组全部保留，因为台号是第三级元组的组成部分，不能用 finish 优势互相
    支配。有零时再以 `(finish 向量, 零 pair 向量)` 为键，键内取最小等待。
* 这是穷举式联合优化，规模受契约硬上限（12 待处理、3 台）约束；带紧 deadline
  的常见工作负载在亚秒到数秒内完成，全部正作业且 deadline 宽松的最坏角落因
  保留完整几何耗时较长，但仍为离线、人工触发的规划操作。
* 测试中的 `tests/planner_oracle.py` 是独立穷举实现（itertools 逐作业枚举、
  事件闭包候选、无 DP 无剪枝），用于小规模比对，包含贪心会输的自制 fixture。

## 持久化与并发

SQLite 保存 `meta(servers,now,revision)`、`jobs`（含不可撤销的台号、
actual_start、actual_finish 与状态）、`requests`（请求回执）。写事务用
`BEGIN IMMEDIATE` 串行化，配合 `expected_revision` 乐观锁，两个进程以同一
revision 提交时至多一个成功。只读 plan 走独立 URI 连接，发布失败不改库。
关闭进程再打开后，当前时刻、单调 revision、待处理/运行/已完成事实与开工
台号全部保留。

## 字段与报告

plan 报告稳定排序、同状态字节确定，包含：
`schema_version,revision,now,servers,jobs`（全部作业及现场事实，按 id 排序）、
`suggestions`（按 id 排序）、`unselected`（未选 id）、
`value_total,wait_total`。
