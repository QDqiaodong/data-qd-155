# queue-cost：工时费用核算 v1

`queue-cost` 为服务中心财务人员提供**精确**的工时费用核算：把每个已完成
服务区间按其合同有效费率切分到费率边界，以有理数计算每一段的费用，仅在
**账单级**做一次舍入，输出逐工作单明细与按账单汇总的应付金额。仅用 Python
标准库，不连接网络，也不读写 queue-planner 的 SQLite 数据库。

```sh
python -m queue_cost.cli --source COST.json --output COST-REPORT.json
# 安装后等价入口
queue-cost --source COST.json --output COST-REPORT.json
```

API：

* `queue_cost.cli.run(source_path)`：读取并校验源文件，返回报告字典（含
  `schema_version`、`source_sha256`）；
* `queue_cost.model.analyze(document)`：接收**已解析**的 JSON 文档，返回
  `{"work_orders": [...], "invoices": [...]}`（不含摘要，便于测试）。

CLI 入口名 `queue-cost` 已在 `pyproject.toml` 注册。

## 安全边界（与既有命令共用）

* 输入、输出都是严格 JSON：重复键、NaN/Infinity、未定义字段一律拒绝。
  输入有 8 MB **字节**上限（超限读取即停，不整份载入）。
* 输出先写同目录临时文件再 `os.replace` 原子替换。输入错误、校验失败或
  核算失败退出码 **2** 并**保留旧报告**；成功退出 **0**。
* 输入与输出不得是同一路径（按 `Path.resolve()` 比较）；输出是符号链接、
  或与输入共享 inode（硬链接/目录符号链接别名）时拒绝。
* `source_sha256` 按输入**原字节**计算。其余所有字段只由输入语义决定：
  按身份与边界稳定排序，行重排不会改变报告（摘要本身必然不同，因为字节
  不同）。

## 源文档结构

顶层**恰含** `servers, work_intervals, rates`。

| 字段 | 要求 |
| --- | --- |
| `servers` | 1..100 整数（拒绝 bool）。 |
| `work_intervals` | 最多 20000 项，可为空。 |
| `rates` | 最多 100000 项，可为空。 |

### work_interval

每项恰含 `work_id, service_code, server, start_tick, finish_tick, invoice_id`：

* 所有标识符（`work_id`、`rate_id`、`service_code`、`invoice_id`）沿用本仓
  `ident` 规则：非空字符串、无首尾空白、≤128 字符且不含控制字符。
* `work_id` 在全部工作单中唯一。
* `server` 为 0..`servers-1` 的整数（拒绝 bool）。
* `start_tick`、`finish_tick` 为 0..10^12 的整数，且
  `start_tick <= finish_tick`。
* 区间语义为**左闭右开** `[start_tick, finish_tick)`。
* 零时长工作合法，费用为 0，且**无需**费率覆盖。
* `invoice_id` 只表达账单身份，不接受额外账单元数据。

### rate

每项恰含 `rate_id, service_code, server, from_tick, to_tick, currency,
minor_units_num, minor_units_den`：

* `rate_id` 唯一。
* `server` 范围同上；`from_tick`、`to_tick` 为 0..10^12 的整数，
  且 `from_tick < to_tick`；时段为左闭右开 `[from_tick, to_tick)`。
* `currency` 恰为三个大写 ASCII 字母（如 `USD`）。
* `minor_units_num` 为 0..10^18 的整数，`minor_units_den` 为
  1..10^18 的整数，表示**每 tick 的最小货币单位数**。
* 不接受的量：浮点数、汇率换算、税费、折扣、负费率。

对同一 `(service_code, server)`，任何两条费率时段**不得重叠**（首尾相接
不算重叠）。不同 `service_code` 或不同 `server` 的费率互不替代。有效期之间
允许有空档，但工作区间落在空档即为输入错误，**绝不用邻近费率外推**。

## 费率切分与精确计算

对每个正时长工作区间：

1. 从 `start_tick` 出发，找到唯一覆盖该 tick 的左闭右开费率时段；这说明
   该 tick 恰好属于一条费率（不重不漏）。
2. 子段为 `[cursor, min(rate.to_tick, finish_tick))`，随后 `cursor` 前移，
   直到 `finish_tick`。
3. 子段费用为 `(to_tick - from_tick) * minor_units_num / minor_units_den`，
   以 :class:`fractions.Fraction` 精确表示；**不枚举 tick**。
4. 工作单精确合计为各子段之和（仍为 `Fraction`）。

边界规则：工作开始恰在费率切换点（`start_tick == rate.from_tick`）只使用
**新**费率；工作结束恰在切换点（`finish_tick == rate.to_tick`）**不**使用
该切换后的费率。零时长工作的子段数组为空、合计为 `0`、`currency` 为
`null`。

### Fraction 规范形式

所有精确金额都序列化为 Python `str(Fraction(value))` 的规范拼写：

* 已约分到最简；
* 整数没有分母（`"5"`、`"0"`，而不是 `"5/1"`）；
* 分数形如 `"25/3"`、`"1/2"`，分母恒为正，无前导 `+`、无前导零；
* 不接受浮点或科学计数法。

输出示例：`"exact_minor_units": "25/3"`。

## 账单汇总与舍入

按 `invoice_id` 汇总其全部工作单的**精确**合计。一个账单必须只含一种
`currency`（全部工作单的币种一致；账单内零时长、总额为 0 时币种可为
`null`）。账单的 `exact_minor_units` 必须**严格等于**所属工作单精确合计
之和。

只有账单总额做一次舍入：

```
rounded_minor_units = floor(exact + 1/2)
```

即“最接近的最小货币单位，恰半向上”（非负金额）。禁止逐子段或逐工作单先
舍入：输入中两支各为 `1/2` 的工作单，账单精确额为 `1`、应舍入为 `1`；
若先逐单舍入会得到 `1 + 1 = 2`，这是错误的。

## 报告结构

```json
{
  "schema_version": 1,
  "source_sha256": "…64 位小写十六进制…",
  "work_orders": [
    {
      "work_id": "W1",
      "invoice_id": "INV1",
      "currency": "USD",
      "segments": [
        {"rate_id": "R-A", "from_tick": 0, "to_tick": 10,
         "exact_minor_units": "10/3"},
        {"rate_id": "R-B", "from_tick": 10, "to_tick": 20,
         "exact_minor_units": "5"}
      ],
      "exact_minor_units": "25/3"
    }
  ],
  "invoices": [
    {
      "invoice_id": "INV1",
      "currency": "USD",
      "exact_minor_units": "25/3",
      "rounded_minor_units": 8
    }
  ]
}
```

* `work_orders` 按 `work_id` 稳定排序；`segments` 按 `from_tick` 升序，
  字段恰为 `rate_id, from_tick, to_tick, exact_minor_units`。
* `invoices` 按 `invoice_id` 稳定排序。
* 空输入（无工作单、无费率）输出两个空数组。
* 报告总子段数上限为 1,000,000（防止 8MB 内的对抗性输入在解析后膨胀为无界
  报告）；超限属计算失败，退出 2 并保留旧输出。

## 手算例子

费率：`R-A = [0,10)` USD 1/3 每 tick，`R-B = [10,20)` USD 1/2 每 tick。

| 工作单 | 区间 | 子段 | 精确费用 |
| --- | --- | --- | --- |
| W1 | [0,20) | R-A [0,10) → `10*1/3 = 10/3`；R-B [10,20) → `10*1/2 = 5` | `25/3` |
| W2 | [5,15) | R-A [5,10) → `5/3`；R-B [10,15) → `5/2` | `25/6` |
| W3 | [10,10) | 空 | `0` |

账单 INV1 精确额 `25/3 + 25/6 = 25/2 = 12.5`，恰在半分处向上，得
`rounded_minor_units = 13`。若先逐单舍入：`round(25/3)=8`、
`round(25/6)=4`，合计 `12`，与正确的 `13` 不同。

## 错误行为

以下均为输入错误，退出码 2、不发布新报告：

* 顶层或对象字段缺失/多余、数组超限、非对象文档；
* 标识符违规、`work_id`/`rate_id` 重复；
* 整数越界、bool 冒充整数、浮点数值；
* `start_tick > finish_tick`、`from_tick >= to_tick`；
* 币种不是三个大写 ASCII 字母；
* 同一 `(service_code, server)` 费率时段重叠；
* 工作区间存在费率空档、跨币种、或 `service_code`/`server` 无对应费率；
* 同一账单混用币种；
* 重复 JSON 键、NaN/Infinity、超过 8 MB、输入输出同路径、输出为符号链接。

## 确定性

* 校验与核算只依赖输入语义，不依赖行序。
* 排序键：工作单按 `work_id`，账单按 `invoice_id`，子段按
  `from_tick`（同起点再按 `to_tick`、`rate_id`，但合法输入不会出现同起点的
  两条覆盖率）。
* 同一输入字节 → 报告字节完全一致（含 `source_sha256`）。

## 测试

* `tests/cost_oracle.py` 是独立 oracle：按契约直接做区间求交、`Fraction`
  求和与单次半向上舍入，**不导入** `queue_cost.model`。
* `tests/test_cost.py` 用 oracle 核验跨费率边界、恰好落在边界、零时长、
  零费率、大分子/分母、10^12 跨度、账单舍入（含恰半向上、提前逐单舍入会
  得到不同总额的反例），并覆盖空档、重叠、错误服务码/服务台、身份冲突、
  混币、空输入、源字段错误、bool 冒充整数、重复键、超 8 MB、同路径/符号
  链接/硬链接、失败保留旧输出、输入重排与 CLI 字节稳定性、以及 fixture。
* `fixtures/cost/source.json` 与 `fixtures/cost/expected-report.json` 为
  自制合成样例。
