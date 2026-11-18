# queue-observe：跨设备时钟日志重建 v1

`queue-observe` 在既有 `queue-replay`（离线 FIFO 分析）与 `queue-planner`
（持久化滚动派工）保持不变的前提下，新增一条只读的运营复盘链路：把同一次
运营中**实际到达 / 开工 / 完工**日志与一份已发布的 `queue-replay` 报告对齐，
输出每单 `wait` / `service` / `sojourn` 在**全部约束共同可行域**上的紧整数
区间、达到区间端点的完整 clock→offset 赋值见证，以及与 baseline 预测的偏差。

它仅用 Python 标准库，不读写 queue-planner 的 SQLite 数据库，也不连接任何
外部服务。

```sh
python -m queue_observe.cli \
    --source LOG.json \
    --baseline REPLAY.json \
    --output REPORT.json
```

API：`queue_observe.cli.run(source_path, baseline_path)` 返回报告字典；
`queue_observe.report.observe(source_data, log_sha256, baseline_data,
baseline_sha256)` 接收已解析的 JSON 文档。CLI 入口名 `queue-observe` 已在
`pyproject.toml` 注册。

## 安全边界（与既有命令共用）

* 输入 / 输出都是严格 JSON：重复键、NaN/Infinity、未定义字段一律拒绝。
  两份输入各自有 8MB **字节**上限（超限读取即停，不整份载入）。
* 输出先写同目录临时文件再 `os.replace` 原子替换；输入错误、计算不可行以外
  的任何失败退出码 2 并**保留旧报告**。成功 0。
* 输出不得覆盖任一输入——按 `Path.resolve()` 后比较，符号链接别名、相同
  物理路径都会拒绝；输出本身是符号链接时拒绝。
* 摘要都按实际输入原字节计算：`log_sha256` 是 LOG 字节的 SHA-256，
  `baseline_sha256` 是 REPLAY 字节的 SHA-256。

## LOG 结构

顶层**恰含** `reference_clock, clocks, events, calibrations, queries`。

| 字段 | 要求 |
| --- | --- |
| `clocks` | 1..64 项，每项恰含 `id, min_offset, max_offset`；`id` 唯一且不能等于固定节点 `@reference`；offset 界为 [-10^12, 10^12] 的**整数**（拒绝 bool）；`min_offset <= max_offset`。 |
| `reference_clock` | 引用已声明的 clock，且它的界必须恰为 `[0, 0]`。该 clock 的 offset 恒为 0。 |
| `events` | 最多 10000 行；**去重后**最多 6000 条。允许为空。 |
| `calibrations` | 最多 512 项，允许为空。 |
| `queries` | 最多 128 项，允许为空。 |

### event

每行恰含 `event_id, job, kind, clock, tick, server, service_seq`：

* 所有 id 沿用既有 `ident` 规则（非空白、≤128 字符、无控制字符）。
* `kind` 只能是 `arrival` / `start` / `finish`；`clock` 必须已声明；
  `tick` 为 [-10^12, 10^12] 整数。
* `arrival` 的 `server` 与 `service_seq` 必须都是 `null`；`start` / `finish`
  的 `server` 为 `0..baseline 服务台数-1` 整数、`service_seq` 为
  0..10^9 整数（均可含缺口）。
* 相同 `event_id` 且**逐字段相同**（对象键序无关）视为同一次事件；任一字段
  不同即冲突，**不能最后写入胜出**。
* 不同 `event_id` 不能重复同一 `(job, kind)`。
* 每个出现的 job 必须恰有 `arrival` / `start` / `finish` 三事件，
  `start` 与 `finish` 的台号、`service_seq` 必须相同；不同 job 不能共用
  `(server, service_seq)`。
* `server` / `service_seq` 是日志事实，**不要求**等于 baseline 分配。
* 同台作业按 `service_seq` 递增产生相邻占用次序；不依赖文件行序，也不依赖
  各设备本地 tick 排序。允许零时长与等时边界。

### calibration

每项恰含 `id, from_event, to_event, min_delta, max_delta`，`id` 唯一，事件引用
必须存在，上下界为 [-10^12, 10^12] 整数且 `min_delta <= max_delta`，语义是

```
min_delta <= true(to_event) - true(from_event) <= max_delta
```

允许引用同一事件（此时区间必须包含 0，否则构成矛盾）。

### query

每项恰含 `id, job, metric`，`id` 唯一，`job` 必须在日志中存在，`metric` 只能是
`wait` / `service` / `sojourn`；即使某 job 没有被查询，它的全部约束依然有效。

## 时钟模型与求解

每个 clock 共享一个未知**整数** offset；该 clock 上事件的真实时刻是
`tick + offset`。所有变量必须在同一次全局赋值中满足：

1. clock 的 offset 落在其 `[min_offset, max_offset]`；
2. 每条事件 `0 <= tick + offset <= 10^12`；
3. 每条 calibration 的 min/max delta；
4. 每单 `arrival <= start <= finish`；
5. 同台按 `service_seq` 相邻的前单 `finish <= 后单 start`。

这些全部是**闭区间**整数约束，无浮点容差，也不以某个校准点替代全部解释。
实现把它们编码成差分约束（difference constraints）：固定节点 `@reference`
表示真实时间 0（即 `offset[@reference] = 0`），每条不等式写成一条
`from -> to`、权重 `bound` 的边，表示 `offset[to] - offset[from] <= bound`。

* 规模：节点数 = clock 数 + 1 ≤ 65，边数约为若干倍约束数，与最大时间
  跨度无关；用 Floyd–Warshall 求全点对最短路径（O(V³)），**不逐 tick、也不
  枚举 offset 组合**。
* 紧区间：对 `wait = base + off[start] - off[arrival]`，
  `max = base + dist(arrival→start)`、`min = base - dist(start→arrival)`；
  `service`/`sojourn` 同理。这是共同可行域上的精确整数上下界，而不是对
  各事件分别取界再拼接。
* 端点见证：`offset[v] = dist(root, v) - dist(root, @reference)` 总是一个满足
  全部边的可行整数赋值，且在 `root = start` 时达到 `max`、`root = arrival` 时
  达到 `min`。见证覆盖**全部 clock**（`@reference` 自身不列出），因此可以
  **独立核验**：代入即可检查所有 clock 界、事件时间界、标定、生命周期与台
  序边。不同 query 的端点各自有各自见证，绝不拼成一个共同赋值。
* 确定性：边按 `(from, to, bound, 规范化 constraint_id JSON)` 排序，节点按
  名字排序；因此输入行重排、相同事件重复投递（在结构校验去重后）不会改变
  所选见证或报告字节（除输入摘要外）。对同一规范事件/标定集合，负环的选取
  也稳定。

### 不可行与矛盾环

结构合法但全局无解时**正常退出 0**，报告 `feasible=false` 与一条
`conflict_cycle`：由原始或明确派生约束构成的闭合环，每条边恰为
`{constraint_id, from, to, bound}`，`bound` 表示
`offset[to] - offset[from] <= bound`；边首尾衔接（`edge[i].to == edge[i+1].from`
并闭合），且 `sum(bound) < 0`。不输出伪造的 `min/max`。环由带固定边序的
Bellman–Ford 松弛产生，不要求全局最短环。

结构缺失 / 引用错 / 重复冲突属**输入错误**，退出 2，绝不包装成业务不可行
的 0。

### constraint_id

每类边有确定的 `constraint_id`，都是 JSON 数组，组件保持独立类型，不用分隔符
拼接（避免含冒号标识符碰撞）：

| 来源 | constraint_id |
| --- | --- |
| clock 下界 | `["clock", clock_id, "min"]` |
| clock 上界 | `["clock", clock_id, "max"]` |
| 事件真实时间下界 0 | `["event", event_id, "min"]` |
| 事件真实时间上界 10^12 | `["event", event_id, "max"]` |
| calibration 下界 | `["cal", calibration_id, "min"]` |
| calibration 上界 | `["cal", calibration_id, "max"]` |
| job 生命周期 `arrival<=start` | `["job", job_id, "arrival_start"]` |
| job 生命周期 `start<=finish` | `["job", job_id, "start_finish"]` |
| 同台相邻作业 | `["server", server号, 前job_id, 后job_id]` |

## REPLAY baseline 校验

`--baseline` 必须是当前 FIFO 契约的自洽 `queue-replay` 报告：

* 顶层恰含 `schema_version=1, source_sha256, result`；`source_sha256` 为 64 位
  小写十六进制，**仅**继承为溯源声明，不要求源文件存在，也不声称已验证其
  字节。
* `result` 先按当前报告的精确字段/类型做结构校验（唯一 job/server ID、边界、
  `wait = start-arrival`、`finish = start+service`、FIFO arrival/id 顺序、
  规范 `Fraction` 字符串、拒绝 bool 冒充整数、不允许多余字段），然后从
  `result.jobs` 的 `id/arrival/service` 与 `result.servers` 数量**重建输入**
  并调用既有 `analyze`（另再经 `simulate` 复核一次）与整个 `result` 精确比较：
  job 列表顺序、每单 server/start/finish/wait、平均/最长等待、观测范围、
  每台 busy/utilization 都必须一致。任一事实或汇总不自洽即退出 2。
* baseline 允许有未出现在 LOG 的 job，不为它们推造实际日志。所有观测 job 都
  必须存在于 baseline。
* 预测仅用于对照：`wait` 取 baseline 的 `wait`，`service` 取 `service`，
  `sojourn` 取 `finish-arrival`。**预测时刻绝不加入实际时钟约束**——若观测
  区间偏离预测，报告保留偏差（`delta_min/delta_max` 可为负），不反向强迫
  观测等于预测。

## 报告

可行：

```json
{
  "schema_version": 1,
  "log_sha256": "...",
  "baseline_sha256": "...",
  "baseline_source_sha256": "...",
  "feasible": true,
  "queries": [
    {
      "id": "q", "job": "A", "metric": "wait",
      "min": 0, "max": 3,
      "min_offsets": {"REF": 0, "D": 0},
      "max_offsets": {"REF": 0, "D": 3},
      "predicted": 0, "delta_min": 0, "delta_max": 3
    }
  ]
}
```

`queries` 按 `id` 排序。`min`/`max` 为非负整数（由真实时间界限导出）；
`min_offsets`/`max_offsets` 都包含**全部** clock 且值为合法整数；
`predicted` 是自洽 baseline 的同类时延；`delta_min = min - predicted`，
`delta_max = max - predicted`。没有 query 时仍判定全局可行并输出
`"queries": []`。

不可行：

```json
{
  "schema_version": 1,
  "log_sha256": "...",
  "baseline_sha256": "...",
  "baseline_source_sha256": "...",
  "feasible": false,
  "conflict_cycle": [
    {"constraint_id": ["clock", "REF", "max"], "from": "@reference",
     "to": "REF", "bound": 0}
  ]
}
```

可行报告不输出 `conflict_cycle`；不可行报告不输出 `queries` 与 `min/max`。

## 正确性论证（差分约束）

* 把每条原始不等式写成边后，整数可行性 ⇔ 图中无负环。这是因为
  `offset` 存在当且仅当所有边可被一个势函数满足；而对整数权重，Bellman–Ford
  的整数距离本身就是整数解。
* 对任意两个节点 `p→q`，紧上界 `offset[q] - offset[p] <= dist(p, q)`；
  取 `offset[v] = dist(root, v)` 可同时满足所有边（三角不等式），故该界可达。
  下界由反向最短路径对称给出。
* 因此报告的 `min`/`max` 是**全局**可行域上的紧界，达到端点的赋值见证同时
  满足所有作业、标定、台序与 clock 界。

## 测试与独立验收

* `tests/observe_oracle.py` 是独立 oracle：直接按原始 LOG 语义重写全部不等式，
  小整数范围内**穷举所有 clock 赋值**，独立计算查询 min/max、核验端点见证
  （逐条原始不等式）、并从未知环的 `constraint_id` 重算 (from,to,bound) 以
  核验矛盾环。它**不导入**生产可行性代码，也不调用生产判定生成预期。
* `tests/test_observe.py` 覆盖：跨 clock offset 抵消、各 clock 单独看均合法但
  加入跨作业同台次序后区间收紧、各条标定单独合法但合在一起矛盾的三环、
  未查询 job 收紧另一 job、跨三 clock 传递界、参考 clock 非首项、负 tick /
  负 offset、真实时间上界 10^12、零时长与同台相邻等时、空 events / queries、
  clock 范围巨大但事件极少、端点多解与输入重排、结构校验（bool、未知字段、
  clock/calibration/query 引用、重复事件冲突、共享 (server, service_seq)）、
  baseline 必须精确自洽、输出路径/符号链接/8MB 上限/失败保留旧报告、完整
  CLI 链路的打乱与重复投递字节稳定、以及观测偏离预测时保留偏差。
* `fixtures/observe/` 提供自制样例：`replay-source.json`（回放输入）、
  `baseline.json`（由 `queue-replay` 生成）、`observation-log.json`（日志）、
  `expected-report.json`（期望报告）。
* 回归：`PYTHONPATH=src python3 -m unittest discover -s tests -v`，既有
  queue-replay / queue-planner 命令与结果不受影响。
