# queue-sla：营业时间口径 SLA 计时 v1

`queue-sla` 为服务中心提供**按营业日历口径**的 SLA 计时：给定周期营业日历、
有限的绝对开/闭店例外和每单暂停区间，输出每个检查时点实际消耗的营业 tick
（`elapsed`）、剩余预算（`remaining`）、是否达标（`met`）以及预算首次达到的
最早到期时刻（`due`）。仅用 Python 3.11 标准库，不连接网络、不使用数据库、
没有后台进程或持久化工单生命周期，每次输入都是一份完整独立的分析。

```sh
python -m queue_sla.cli --source SLA.json --output SLA-REPORT.json
# 安装后等价入口
queue-sla --source SLA.json --output SLA-REPORT.json
```

API（均可直接调用，参数、返回结构与异常边界如下）：

* `queue_sla.cli.run(source_path)`：读取并**严格校验**源文件，返回完整报告
  字典 `{"schema_version": 1, "source_sha256": ..., "cases": [...]}`；任何
  校验、解析或 IO 失败抛出 `ValueError`/`TypeError`/`KeyError`/`OSError` 等
  异常，CLI 会把它们转成退出码 2。
* `queue_sla.model.analyze(document)`：接收**已解析**的 JSON 文档（Python
  对象，不是字节），返回 `{"cases": [...]}`（不含 `schema_version` 与摘要，
  便于测试）。输入不满足契约时抛出 `ValueError`（结构/边界/引用/重复身份）
  或 `TypeError`（类型不符）。本函数是纯函数，不读写文件、不保留全局状态，
  因此同一日历被多单共享时，某单暂停不会污染其他单。

CLI 入口名 `queue-sla` 已在 `pyproject.toml` 注册。

## 安全边界（与既有命令共用）

* 输入、输出都是严格 JSON：重复键、NaN/Infinity、未定义（缺失或多余）字段
  一律拒绝；整数校验显式拒绝 `bool`（`True`/`False` 不是整数）。输入有
  8 MB **字节**上限（超限读取即停，不整份载入）。
* 输出先写同目录临时文件，再 `os.replace` 原子替换；写失败时临时文件在
  `finally` 中清除，不会留下半份新报告。输入错误、校验失败或发布失败退出码
  **2** 并**保留旧报告**；成功退出 **0**。
* 输入与输出不得是同一路径（按 `Path.resolve()` 比较）；输出是符号链接、或
  与输入共享 inode（硬链接/目录符号链接别名）时拒绝，发布前后各检查一次。
* `source_sha256` 按输入**原字节**计算 64 位小写十六进制；同字节重跑报告
  字节相同。除摘要外，所有字段只由输入语义决定：日历、窗口、例外、工单、
  暂停、检查点任意重排都不改变结果。

## 时间口径

* 周期原点固定为 tick 0；对任意整数 `u`，单位区间 `[u, u+1)` 要么营业要么
  不营业。**不处理**时区、节假日名称、真实日期或夏令时。
* 截至 `t` 的消耗只统计 `[opened_at, t)`，不把端点 `t` 所在的整 tick 计入。
  所有区间一律**左闭右开**。
* `elapsed` 允许超过 `budget`；`remaining = max(0, budget - elapsed)`；
  `met = (elapsed >= budget)` 只是“预算已达到”的 JSON 布尔，不代表服务质量
  合格或逾期处罚。

## 营业日历语义

每个日历恰含 `id, period, windows, exceptions`：

* `period` 为 1..10^9 整数；`windows` 每项是恰含两个整数的 `[a, b]`，
  `0 <= a < b <= period`，表示对所有非负整数 `k` 营业
  `[k*period+a, k*period+b)`。窗口允许重叠、相邻、重复，语义是**并集**；
  `a >= b` 拒绝；跨周期末端的营业必须显式拆成两个窗口。空 `windows` 合法。
* `exceptions` 每项恰含 `id, kind, from_tick, to_tick`，`kind` 仅为
  `open` 或 `closed`，`0 <= from_tick < to_tick <= horizon`。不同例外允许
  任意交叠，数组顺序不决定优先级。
* 有效营业集合 `E = (周期窗口并集 P ∪ open 例外并集 O) − closed 例外并集 C`，
  **closed 始终优先**，同时覆盖周期营业与 open 例外。重叠和相邻窗口按集合
  处理，不重复计时。

## 工单语义

每单恰含 `id, calendar, opened_at, budget, pauses, checkpoints`：

* `calendar` 引用已声明日历；`opened_at` 为 0..horizon 整数；`budget` 为
  0..10^12 整数。
* `pauses` 每项恰含 `id, from_tick, to_tick`，满足
  `opened_at <= from_tick < to_tick <= horizon`。不同暂停允许交叠、相邻或
  同边界，其**并集**只停表一次；暂停可以跨越非营业时段。
* 累计从 `opened_at` 起，计算 `E` 中**不属于本单暂停并集**的 tick。因为它是
  集合测度 `|E| − |E ∩ pauses|`，所以：累计值不会为负；闭店与暂停重叠的
  tick 最多扣一次（它本就不属于 `E`）；落在暂停内的特殊开门不能多计；不能
  把暂停总长度或例外总长度直接从墙钟时长里减。
* `checkpoints` 每项恰含 `id, at_tick`，`opened_at <= at_tick <= horizon`；
  允许空数组、乱序、同 tick 重复时点。即使没有检查点也必须计算 `due`。
* `due` 是 `[opened_at, horizon]` 内累计**首次达到** `budget` 的最小整数
  时刻，不存在则为 `null`。`budget = 0` 时 `due = opened_at`；恰好在营业段
  右端达到预算，就在该端点到期，不会跳过闭店或暂停平台去找下一次营业。
  暂停正好从 `due` 开始时，`due` 仍是该端点（达到即到期，之后才停表）。

## 身份与命名空间

* 所有 `id` 沿用本仓 `ident` 规则：非空、无首尾空白、≤128 字符、无控制
  字符，**保持原字符串**，不修剪、不拼接。
* 日历 id、工单 id 各自在其数组内唯一；例外 id 在所属日历内唯一；暂停 id、
  检查点 id 各在本单对应数组内唯一。重复身份**即使内容完全相同也拒绝**。
* 暂停与检查点 id 属于各自独立命名空间，可与其他工单、其他数组中的同文本
  id 相同；来源、日历、工单之间不靠拼接标识符建立身份。
* 未被任何工单使用的日历也必须完整校验（含其窗口与例外）。

## 数量上限

| 对象 | 上限 |
| --- | --- |
| 日历 | 64 |
| 工单 | 5000 |
| 周期窗口（所有日历合计，重复窗口计入） | 10000 |
| 例外（所有日历合计） | 30000 |
| 暂停（所有工单合计） | 30000 |
| 检查点（所有工单合计） | 30000 |
| `horizon`、绝对 tick、`budget` | 0..10^12 整数 |
| `period` | 1..10^9 整数 |

## 报告结构

顶层恰含 `schema_version`（整数 1）、`source_sha256`、`cases`。`cases` 按
`id` 的 Python 字符串顺序排列；每项恰含 `id, calendar, opened_at, budget,
due, checkpoints`，前四项保留源事实，`due` 为整数或 `null`。`checkpoints`
按 `id` 排序，每项恰含 `id, at_tick, elapsed, remaining, met`；时间与累计
均为整数，`met` 为 JSON 布尔。

## 算法：可累计营业量与最早到期的同一集合口径

**闭式计数（不逐 tick、不逐周期展开）。** 记周期窗口并集为 P、open 例外
并集为 O、closed 例外并集为 C。由集合容斥恒等式，有效营业集合
`E = (P ∪ O) − C` 在 `[0, t)` 内的测度为

```
|E ∩ [0,t)| = |P ∩ [0,t)| + |O ∩ [0,t)| − |P∩O ∩ [0,t)|
            − |C∩P ∩ [0,t)| − |C∩O ∩ [0,t)| + |C∩P∩O ∩ [0,t)|
```

* `|P ∩ [0,t)|` 是纯闭式：`whole, offset = divmod(t, period)`，整周期贡献
  `whole * 每周期营业 tick 数`，余数周期对合并后的窗口做一次二分前缀查询。
  因此 `period = 1` 且 `horizon = 10^12` 也只有常数次运算。
* 其余五项都是**有限**区间并集（例外或两个有限并集的交）上的前缀和；每个
  合并后的区间片段带一个权重——普通长度，或该跨度内的周期营业量
  `|P ∩ [a,b)|`（同样由上面的闭式算出）。前缀查询是一次二分加至多一个部分
  片段。P 只通过这个跨度权重出现，**从不被物化**，所以 10^12 宽的例外只值
  一个区间，而不是十亿个周期。
* 每单暂停采用**同一恒等式**作用于有限暂停并集，得到 `pauses ∩ E` 的测度
  时间线；`elapsed = |E ∩ [opened_at,t)| − |(pauses ∩ E) ∩ [opened_at,t)|`。
  这保证累计与反求始终建立在同一个有效营业集合上，闭店/暂停重叠不重复扣减。

**最早到期反求。** 累计量关于整数 tick 是非递减整数阶跃函数，且由与计数
完全相同的前缀表示计算。`budget > 0` 时先判定 `[opened_at, horizon]` 内
能否达到预算，再在该区间上二分查找使 `elapsed(t) >= budget` 的最小 `t`；
`budget = 0` 直接返回 `opened_at`。计数与反求因此不可能口径漂移。

**复杂度。** 设窗口数 W、例外数 X、暂停数 Z、检查点数 Q（均为合并前输入
规模）。日历预处理为 O((W+X) log(W+X))，每单暂停时间线 O(Z log Z)；每个
检查点或每步二分都是 O(log(W+X+Z)) 的前缀查询，`due` 二分 O(log horizon)
步。整体时间约
`O((W+X)log(W+X) + Σ_cases (Z log Z + (Q + log horizon) log(W+X+Z)))`，
空间 `O(W + X + Σ Z)`，与 horizon 和 period 的数值大小无关。

## 测试与 oracle

* `tests/sla_oracle.py` 是**独立**小整数逐 tick oracle：直接按契约文本逐
  tick 判定营业/闭店与暂停/运行，逐时点枚举最早达标，不导入任何生产日历、
  前缀或反求辅助函数。
* `tests/test_sla.py` 用 oracle 对随机小例与手工边界例交叉核验 `elapsed`、
  `remaining`、`met` 与最早 `due`，覆盖闭店与暂停部分/完全重叠、特殊开门
  被暂停覆盖、同日历两单不同暂停的隔离；另含可证明的大例（多周期稀疏营业、
  重叠开/闭例外、同日历暂停差异、大量不同检查时点）、period=1 与 10^12
  跨度的不展开压力测试，以及全部校验、重复身份、bool/非法未使用例外、超
  8MB、同路径/硬链接/符号链接与发布失败保留旧报告等安全边界。
* `fixtures/sla/` 为原创虚构合成样例，`expected-report.json` 与 oracle 结果
  一致；无真实门店、人员或 SLA 数据。
