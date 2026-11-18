本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

`fixtures/observe/` 同样是原创虚构合成数据：`replay-source.json` 为回放输入，
`baseline.json` 由本仓 `queue-replay` 生成，`observation-log.json` 为手工构造的
跨设备日志，`expected-report.json` 为对应期望报告；无真实设备、时钟或人员数据。

`fixtures/cost/` 为原创虚构合成费率与工作单：`source.json` 是 `queue-cost`
源文档，`expected-report.json` 由本仓 `queue-cost` 生成，覆盖跨费率边界切分、
零时长工作、零费率与账单恰半向上舍入；无真实合同、账单或金额数据。
