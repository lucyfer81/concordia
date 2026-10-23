# S3 runner 外部审阅修复记录

日期：2026-10-23。用户授权直接修复三个阻塞；本轮零真实模型调用，未
commit、未清理旧工作区、未修改旧运行产物，停在工程复核/执行授权点。

依据：[S3 诊断协议](../plans/2026-10-23-sol-s3-diagnostic-story-protocol.md)
§3/§4/§5/§8。本次变更使实验请求归属、预算与停批边界可信，服务于后续
故事发展诊断，不增加故事机制，也不代表叙事能力已获得验证。

## 修复

1. **并发请求串写。** `_ObservationState` 改为线程本地状态，避免 A 的
   响应写入 B 的 slot，以及 B 随后被误判为无 active slot。SDK 与
   Concordia 两层账本共享可重入的短锁，保护编号、预扣、写入和审计；
   请求等待期间释放锁，保留原有并发能力。编号和 attempts 只在 started
   持久化成功后递增；额度阻断不虚增 wrapper 消费。
2. **基础设施错误被当作故事终态继续。** 检查生产返回的原始
   `reason/error_message`，只有明确的 `StoryOutputError: Semantic review
   rejected output:` 错误允许继续下一运行；认证、路由、归档、脚本或
   其他未分类错误保存为批次 stop，R2 不启动。SDK 归档错误不再被吞掉；
   已发出的请求仍占额度、保留错误终态，不退款或改判为成功。
3. **冻结核验只覆盖批次入口。** 每次运行开始，以及每个模型/SDK 转发
   前重新核验冻结哈希、两层账本结构、started 旁侧文件、prompt 哈希与
   writer 内存计数。审计在同一短锁下执行，不读取写入一半的记录；
   正常并发中的 pending 仅代表在途请求。批次重入则同时检查两层未决，
   发现中断后停批，不提供跳过或自动恢复路径。

全量回归还暴露了生产 fail-fast executor 在首个错误后不等待其他组件
future 的行为。runner 在生产退出时关闭模型接纳并等待已接纳调用完成
留证；关闭后的迟到调用不得写账本或转发。这解决停批返回后后台仍写文件
的竞态，不修改核心 executor、API 重试、提示或故事选择。

## 验证

- 三个原始反例先 RED：并发串写、真实 `run.run()` 将 401 转换为 error
  后错误启动 R2、R1 后协议漂移仍启动 R2。
- 修复后的 runner 测试：**51 passed + 2 subtests**（原 41 项，新增
  10 项方法）。新增覆盖交错响应归属、并发临界额度及网络等待不持锁、
  调用关闭/排空、401 停批、归档故障停批、语义拒绝仍继续、逐运行与
  SDK 转发前的协议/prompt 漂移；使用真实 BaseGPT 解析器的假客户端
  验证选择题重问 **1 Concordia attempt / 2 SDK create**，以及生产
  RetryStats/BudgetGuard 链的 API 重试仍正常累计。
- `pytest examples -q`：**637 passed + 140 subtests**。
- `pytest concordia -q`：**1487 passed / 33 skipped + 126 subtests**。
- 两个变更 Python 文件 pylint / pyrefly / pyink / isort 通过；
  `pylint --errors-only concordia` 通过。全库 pyrefly 保留既有 4 项：
  vllm 可选依赖缺失 3 项、FastMCP 属性 1 项，未修改相关文件。
- 新离线证据：`/tmp/opencode/s3_dry_run_review_fixes_20261023_final/`。
  冻结哈希复验通过，8/8 不变量成立；R1/R2 均 `beat_timeout`，每运行
  Concordia 账本与生产计数 **81=81**；SDK 账本为 **0**，未决与审计
  warning 均为空，显式导演配置与 checkpoint 一致。SDK 客户端行为由
  上述假客户端测试覆盖，不将零 SDK 的 CLI dry-run 当作真实 SDK 证明。
- 生产及既有示例的 **338 个 Python 文件哈希全部不变**。本轮源码改动
  限于 `s3_diagnostic_story.py`、colocated tests 和 README；旧 SDD、旧
  dry-run、历史真实批次均保留。修复前快照与文件哈希清单位于
  `/tmp/concordia-s3-review-fix-baseline/`。

## 当前结论与边界

三个阻塞的确定性回归已通过；未增加第三次运行、未放宽 24 turns 或
双层 400/run 预算、未关闭生产重试。未宣布 MVP 验收或真实故事推进效果。
本轮没有在 run_results 下冻结 S3 批次，没有发起真实请求；后续真实
freeze/run 仍须按协议 §8 在工程复核后另获用户授权。
