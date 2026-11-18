本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

`fixtures/observe/` 同样是原创虚构合成数据：`replay-source.json` 为回放输入，
`baseline.json` 由本仓 `queue-replay` 生成，`observation-log.json` 为手工构造的
跨设备日志，`expected-report.json` 为对应期望报告；无真实设备、时钟或人员数据。

`fixtures/cost/` 为原创虚构合成费率与工作单：`source.json` 是 `queue-cost`
源文档，`expected-report.json` 由本仓 `queue-cost` 生成，覆盖跨费率边界切分、
零时长工作、零费率与账单恰半向上舍入；无真实合同、账单或金额数据。

`fixtures/reconcile/` 为原创虚构合成对账样例：`source.json` 是
`queue-reconcile` 源文档（内嵌一份 queue-cost 源与独立收款导出），
`expected-report.json` 由本仓 `queue-reconcile` 生成，覆盖账单级恰半向上
舍入、容差边界、收款争用、零时长 null 币种账单、空准入列表与
fixed/ambiguous/unmatched 三态分类；无真实账单、收款或金额数据。

`fixtures/identity/` 为原创虚构合成客户主数据样例：`source.json` 是
`queue-identity` 源文档（三个虚构来源、五条虚构客户记录、四条带有效期
的同人关联与十二个乱序时点查询），`expected-report.json` 由本仓
`queue-identity` 生成，覆盖桥接隔离与恢复、边界生效/失效、平行关联、
孤立记录与空字段；无真实个人、联系方式或客户数据。
