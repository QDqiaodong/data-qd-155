# queue-reconcile：跨源收款对账 v1

`queue-reconcile` 为服务中心财务人员提供**跨源收款对账**：账单侧是既有
`queue-cost` 源文档（工作单、费率、混币校验完全沿用，**不接受**预先算
好的账单金额），收款侧是银行导出的独立收款（只带收款身份与有限的账单
准入信息）。命令算出**全局可解释**的一对一配对：先最大化配对数，再
在最大配对数下最小化金额差总和（两层目标严格优先、任意精度整数），
并对每条账单与每笔收款给出它在**全部**两层最优方案中的可能对端集合
及是否可能未配对，把必然成立的配对（`fixed`）与仍可换配、漏配的记录
（`ambiguous`）区别展示，供人工核销。仅用 Python 标准库，不连接网络，
也不读写 queue-planner 的 SQLite 数据库。

```sh
python -m queue_reconcile.cli --source RECONCILE.json --output REPORT.json
# 安装后等价入口
queue-reconcile --source RECONCILE.json --output REPORT.json
```

API：

* `queue_reconcile.cli.run(source_path)`：读取并校验源文件，返回报告字典
  （含 `schema_version`、`source_sha256`）；
* `queue_reconcile.model.reconcile(document)`：接收**已解析**的 JSON 文
  档，返回 `{"objective": ..., "matching": ..., "invoices": ...,
  "receipts": ...}`（不含摘要，便于测试）。

CLI 入口名 `queue-reconcile` 已在 `pyproject.toml` 注册。

## 安全边界（与既有命令共用）

* 输入、输出都是严格 JSON：重复键、NaN/Infinity、未定义字段一律拒绝。
  输入有 8 MB **字节**上限（超限读取即停，不整份载入）。
* 输出先写同目录临时文件再 `os.replace` 原子替换。输入错误、校验失败或
  计算失败退出码 **2** 并**保留旧报告**；成功退出 **0**。
* 输入与输出不得是同一路径（按 `Path.resolve()` 比较）；输出是符号链接、
  或与输入共享 inode（硬链接/目录符号链接别名）时拒绝。
* `source_sha256` 按输入**原字节**计算，是唯一允许随原字节重排变化的
  报告字段；其余所有字段只由输入语义决定，行重排不会改变报告。

## 源文档结构

顶层**恰含** `cost_source, receipts, tolerance_minor_units`。

| 字段 | 要求 |
| --- | --- |
| `cost_source` | 一份完整的 `queue-cost` 源文档（见 [queue-cost 契约](cost-contract.md)），其 `servers, work_intervals, rates` 字段、数值边界、费率覆盖及混币校验全部严格沿用；其中任何错误都使整份输入被拒绝。不接受预先计算的账单金额。 |
| `receipts` | 最多 300 项的数组，可为空。 |
| `tolerance_minor_units` | 0..10^36 的整数（拒绝 bool）。 |

账单侧必须调用现有 `queue_cost.model.analyze` 处理 `cost_source`；计算后
不同 `invoice_id` 最多 300 个，超限整份拒绝。

### receipt

每项恰含 `receipt_id, currency, minor_units, eligible_invoices`：

* `receipt_id` 沿用本仓 `ident` 规则（非空、无首尾空白、≤128 字符、无
  控制字符），且在收款侧唯一；允许与某个 `invoice_id` 文本相同——两者
  属于不同命名空间，互不解析。
* `currency` 恰为三个大写 ASCII 字母（如 `USD`）。
* `minor_units` 为 0..10^36 的整数（拒绝 bool）。
* `eligible_invoices` 为 `null` 或至多 300 个**无重复**的现有
  `invoice_id` 数组：`null` 表示所有账单均获准；`[]` 表示没有账单获
  准；引用不存在的账单整份拒绝。

无准入、币种不符或金额差超容差**不是**输入错误，只是不构成候选配对。

## 应收记录与候选配对

* 以 `analyze` 给出的 `invoice_id`、`currency`、`rounded_minor_units`
  建立应收记录；不重新按工作单或子段舍入，也不以 `exact_minor_units`
  代替最终账单金额。
* `currency = null` 的账单（既有核算无法从零时长工作确定币种）**始终
  无候选、可以未配对**；不从收款反填币种。
* 具有明确 `currency` 的零金额账单可以与同币种合法零金额收款配对。
* 单笔收款不拆分、多笔收款不合并、一份账单不接受分次清偿；不引入汇
  率、手续费或已核销持久状态。

候选配对 `(i, r)` 当且仅当同时满足：

1. 币种相同（`null` 币种账单无候选）；
2. `|rounded_minor_units(i) − minor_units(r)| ≤ tolerance_minor_units`，
   以任意精度整数比较；
3. `r.eligible_invoices` 为 `null` 或包含 `i.invoice_id`。

## 两层目标

在所有由候选边构成的一对一配对（每条账单、每笔收款至多出现一次）中：

1. 先最大化配对数 `matched_count`；
2. 再在达到最大配对数的配对中，最小化金额差总和
   `total_difference_minor_units`（各配对 `rounded_minor_units` 与
   `minor_units` 的绝对差之和，任意精度整数求和）。

两层目标**严格字典序优先**：不允许用未经证明的固定权重把两层折叠成一
个标量，不允许浮点近似。允许空配对，未配对记录不另计罚金；只要候选
关系结构合法，无法全部配对是正常业务结果（退出码 0）。

## 全最优支持集与分类

对每条账单与每笔收款，报告精确列出它在**所有**达到两层目标的配对方案
中可能配对的对端 ID（逐记录分别取并集），以及它是否可能在某个最优方
案中未配对：

* 单份最优方案、局部候选集合、仅满足最大基数（未最小化费用）的边都
  **不等于**全最优支持集；
* 同一最优方案中的配对必须相容；可能对端列表是逐记录并集，**不表示**
  列表中的候选能够同时成立——即使某份见证里的每条边都可被其他最优方
  案替换，也不能把两侧支持集任意拼接成另一份完整方案；
* 单个记录只有一个可能对端但也可能未配对时仍是歧义；只看局部候选数量
  不能认定确定配对。

`status` 完全由全最优支持决定：

| 条件 | `status` |
| --- | --- |
| 可能对端列表为空 | `unmatched`（此时 `can_be_unmatched` 必为 `true`） |
| 列表恰一项且 `can_be_unmatched = false` | `fixed` |
| 其余 | `ambiguous` |

两侧支持互相一致（`r` 在 `i` 的列表中 ⟺ `i` 在 `r` 的列表中）；`fixed`
配对两端必然互为唯一可能对端且都不可未配对。

## 报告结构

顶层恰含 `schema_version, source_sha256, objective, matching, invoices,
receipts`：

* `schema_version` 恒为 1；`source_sha256` 覆盖源文件实际字节。
* `objective` 恰含 `matched_count, total_difference_minor_units`，即两
  层最优目标值。
* `matching` 是一份达到两层目标的完整配对见证，每项恰含 `invoice_id,
  receipt_id, difference_minor_units`，按 `(invoice_id, receipt_id)`
  排序。有多份最优见证时任选其一，但选择对规范后的业务内容确定（见
  “确定性”），不承诺字典序最小。见证中的每条边都属于双方支持集，但
  不能用该见证替代完整支持结果。
* `invoices` 按 `invoice_id` 排序，每项恰含 `invoice_id, currency,
  rounded_minor_units, possible_receipt_ids, can_be_unmatched, status`。
* `receipts` 按 `receipt_id` 排序，每项恰含 `receipt_id, currency,
  minor_units, possible_invoice_ids, can_be_unmatched, status`。
* 两个 `possible` 列表均为无重复、按 Python 字符串顺序排列的 ID 数组；
  布尔字段为 JSON 布尔。

## 算法与正确性论据

建模为单位容量二分图最小费用最大流：源→每条账单（容量 1，费用 0），
账单→收款每条候选边（容量 1，费用 = 金额差），收款→汇（容量 1，费用
0）。账单、收款各至多 300 条，候选边至多 9 万条。

1. **两层目标**：逐最短路增广（SSP）。每次沿约化费用最短路径增广 1 单
   位，保持“当前流量值下费用最小”的不变式；当不存在增广路时流量达到
   最大（即最大配对数），且费用在所有最大流中最小。两层目标由算法结
   构保证，全程任意精度整数，无权重折叠、无浮点。
2. **全最优支持**：增广结束后残余图所有弧约化费用非负（对偶可行）。
   任一其他同基数配对是最优 ⟺ 它与计算出的最优解之差是零费用循环流
   ⟺ 在零约化费用残余图中可分解为有向环（环上各弧约化费用之和等于实
   际费用之和，而各项均非负，故全为零）。因此：
   * 未使用的候选边 `(i, r)` 出现在某个最优方案中 ⟺ 其弧约化费用为 0
     且 `i`、`r` 在零约化费用残余图的同一强连通分量中（弧落在某个零
     费用环上，沿环增广即得包含该边的最优方案）；
   * 已配对记录可以未配对 ⟺ 它到源（账单侧）或自汇（收款侧）的反向
     弧约化费用为 0 且与源/汇同强连通分量；
   * 计算出的最优解本身见证其每条边，故已配对的边必在支持集中。

   故支持集**不漏报**（任何最优方案的边都满足上述条件）也**不虚报**
   （满足条件的边都能由一次零费用环增广得到一份最优方案）。
3. **复杂度**：`O(K · E log V)`，`K ≤ 300` 次增广、`E ≤ 90,000` 条候选
   弧；强连通分量用迭代 Kosaraju，`O(V + E)`。不枚举配对方案、不按金
   额单位展开。

## 确定性

* 账单按 `invoice_id`、收款按 `receipt_id` 排序后建立索引；候选边集、
  图结构、最短路堆比较（距离相同按节点编号）都只依赖规范后的业务内
  容，不依赖输入行序。
* 同一输入字节 → 报告字节完全一致（含 `source_sha256`）；输入行重排
  后除 `source_sha256` 外报告逐字节一致。

## 错误行为

以下均为输入错误，退出码 2、不发布新报告（保留旧报告）：

* 顶层或对象字段缺失/多余、文档非对象、`receipts` 非数组或超过 300 项；
* `receipt_id` 重复或非法、收款币种非法、`minor_units` 或
  `tolerance_minor_units` 越界、为 bool 或浮点；
* `eligible_invoices` 非数组/超过 300 项/有重复/引用不存在的账单；
* 计算后不同 `invoice_id` 超过 300 个；
* `cost_source` 内部任何错误（沿用 queue-cost 的全部校验：费率空档、
  时段重叠、混币、越界等）；
* 重复 JSON 键、NaN/Infinity、超过 8 MB、输入输出同路径或硬链接、输
  出为符号链接。

## 测试

* `tests/reconcile_oracle.py` 是独立 oracle：对小规模实例枚举全部合法
  一对一配对，先筛出两层最优集合，再给出双侧支持集、可未配对性与分
  类；账单金额由既有的独立 `cost_oracle` 计算，不导入 `queue_reconcile`
  生产代码。oracle 只用于小例。
* `tests/test_reconcile.py` 用 oracle 比对全部小例与两组随机小实例
  （含金额 {0,1}、零容差的退化争用），并覆盖：局部合法却不属于任何
  最优方案的边、同一边只在较差费用方案出现、交替换配导致的关联歧义、
  固定配对与歧义共存、收款争用唯一账单、单侧多余记录、零金额与零容
  差、空侧、币种不符、同名跨命名空间身份、10^36 巨大金额与容差边界、
  账单级舍入改变最优配对、每条记录都有局部候选但无法全部匹配的争用、
  单份见证的配对全都可交换但支持集不可任意拼接的例子、300×300 最大
  规模非唯一最优（交替环与全连通零费用，完整最优支持由对称构造手工
  给出）、输入重排稳定、非法字段与旧报告保留。
* `fixtures/reconcile/source.json` 与
  `fixtures/reconcile/expected-report.json` 为自制合成样例：覆盖账单级
  恰半向上舍入（12.5 → 13 决定候选）、容差边界（差恰为容差）、收款争
  用、零时长 `null` 币种账单、空准入列表及 fixed/ambiguous/unmatched
  三态共存。
