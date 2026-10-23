# Phase 1 修复后代码复审

日期：2026-10-15。
审阅对象：`489155b` 之后的当前工作区修复，包含尚未跟踪的
`observation_queue.py`、`retry_stats_wrapper.py` 及其测试。
前次报告：`2026-10-15-story-enactment-phase1-acceptance.md`。

**结论：本轮代码审核暂不放行，先修复下面五项，再进行三场景各三次的
真实模型人工评审。** 人工评审尚未执行是用户明确安排的下一阶段，本次
不将它列为代码审核失败原因。本轮没有调用真实模型或修改实现。

## 1. 本轮发现

### R1 — README 的真实模型用法保存了错误模型标识（P1）

位置：`examples/story_enactment/run.py:1039`；
`examples/story_enactment/README.md:155`、`:187`。

README 的单场景和九次批量示例都将真实模型配置传给 `run(model_config=...)`，
却没有同时传给 `make_run_config()`。因此运行配置记录的是默认 stub。
`run()` 用外层配置构造实际模型，但保存 checkpoint 和 summary 时仍使用
未经更新的 `config['model_config']`。

已用替代 provider 和固定 embedder 跑通真实模拟路径，完全不访问 API：

```text
传给 run() 的身份：openai / live-model-probe
checkpoint 保存身份：stub / prompt_stub
以原 openai 身份恢复：ValueError: model_config mismatch
```

该缺陷还会使九次人工评审的归档错误标注模型，影响成本和质量结果的归因。
即使不执行恢复，也不能将当前 README 产生的记录作为正确模型标识的证据。

修复要求：首次运行确定实际生效模型后，构造一份归一化运行配置用于
构建、保存和汇总；其模型标识必须与实际 provider 一致，并剔除 API key。
也可以明确拒绝互相冲突的配置，但 README 的标准调用必须可运行。
不能只修改恢复校验，让真实模型身份与 stub 身份被视为相同。

回归测试：原样执行 README 的参数组合，检查 checkpoint 与 summary 的
模型身份；同一身份配新凭据可以恢复，不同身份被拒绝；序列化文件不含
凭据。另覆盖 `disable_language_model=True` 时实际使用 stub 的标识。

### R2 — 新事件编码会吞掉段首数字并扩大换行（P1，前次 F2 未关闭）

位置：`concordia/contrib/components/game_master/observation_queue.py:59`、`:78`。

编码把换行长度直接拼在 U+2028 后，没有结束符；解码用 `\d+` 贪婪
读取数字。因此，原事件下一段以年份、金额或编号开头时，正文数字会被
并入换行数量。

已执行的最小复现：

```python
item = 'Event: 第一段。\n\n\n2026年，Bob 承认伪造。'
decoded = decode_observation(encode_queue_item(item))[0]
```

结果：原文 29 个字符，解码后 32048 个字符；三次换行变成 32026 次，
`2026年` 不再存在。原来的字母/汉字段首测试通过，不能证明编码可逆。

修复要求：使用有明确结束边界的转义格式，正文数字不能进入长度字段。
保留队列项边界、事件原文与字面转义字符的可逆性。

回归测试：段首分别是 `0`、`3`、`2026`、金额和连续编号；组合真实
观察队列、两个事件和保存恢复，断言事件内容逐字一致且计数准确。
测试输入不能只用非数字段首。

### R3 — 并发调用被误算成 API 重试（P1，前次 F5 未关闭）

位置：`concordia/contrib/language_models/retry_stats_wrapper.py:105`。

`RetryStatsLanguageModel` 用共享 BudgetGuard 总数的前后差减一来计算
当前请求的重试数。请求重叠时，该差值包含其他请求的尝试；给最终累加
加锁不能隔离请求归属。

已做两种复现，provider 均始终成功：

1. 两个线程通过 Barrier 保持请求重叠：总尝试 2，预期重试 0，实际重试 1。
2. 实际 `character_depth` 单步运行，stub 的 `sample_text` 延迟 20ms，
   不注入任何失败：总尝试 8，预期重试 0，实际报告 2。

第二项不是假设外部并发：`EntityAgent` 本来就并行执行组件的 `pre_act`，
角色深度模式的独立组件会共享模型。快速 stub 的顺序测试掩盖了问题。

修复要求：统计当前逻辑请求自身的尝试，不能用共享全局计数器的区间差
推断归属。继续保持每次真实尝试经过预算检查、预算阻止的请求不计重试。

回归测试：并发零失败必须为 0；一个请求失败一次而另一个首次成功，累计
必须为 1；覆盖 text/choice 混合并发、预算停止和保存恢复。必须包含实际
角色深度路径，不能只测同步 wrapper 调用。

### R4 — 恢复前读取评分补问基线，导致本进程指标重复计入历史（P2）

位置：`examples/story_enactment/run.py:1061`、`:1072`、`:1196`。

`parse_retries_at_start` 在 `load_from_checkpoint()` 之前读取，因此始终
来自新建 director，通常为 0。恢复后再做累计值减该基线，会把旧进程的
评分补问重复计入本进程。

已执行：张力观察模式完成一步并产生一次解析补问，在 checkpoint 提交
后中断；恢复时剧情步上限已到，不再调用模型。结果：

```text
attempts_used_this_process = 0
used_scoring_parse_retries = 1
scoring_parse_retries_used_this_process = 1  # 应为 0
```

修复要求：在组件状态恢复后、继续执行前读取评分补问基线；首次运行的
基线仍为 0。保留累计值与本进程值的不同口径。

回归测试：恢复后零调用时本进程值为 0；恢复后新发生一次补问时本进程
值为 1，累计值为历史值加 1。现有恢复测试仅覆盖 API 重试，未覆盖这一项。

### R5 — Fountain 仍未处理对白中的字面语法标记（P2，前次 F4 部分修复）

位置：`examples/story_enactment/renderer.py:126`、`:335`、`:343`。

多段对白和误似场景标题的舞台指示已有修复，但 `_encode_dialogue()`
只处理空行，不处理内容与格式语法的冲突。项目自己的解析器仍产生：

| speech 输入 | 当前解析结果 | 应有类型 |
|---|---|---|
| `!我不同意。` | action，且丢失 `!` | dialogue，保留字面内容 |
| `(这不是舞台提示，是我说的原话。)` | parenthetical | dialogue，保留字面内容 |
| `第一句。\n!第二句。` | dialogue + action | 两行均为 dialogue |

官方语法确实赋予 `!` 强制动作、角色/对白后的括号行以 parenthetical
语义；仅将原始正文拼入输出不保证其为对白。
参考：[Fountain 官方语法](https://fountain.io/syntax/)。

修复要求：对白正文中的字面标记必须与渲染器生成的格式标记区分；补充
转义或其他符合 Fountain 的编码，并让测试解析器识别。不能删除字面字符
或把它改成舞台指示。现有单换行及多段测试应继续通过。

回归验证：上述三例逐项断言元素类型和解析后的字面内容；使用独立
Fountain 解析器或导入工具验证代表性输出。目前未发现独立解析验证记录。

## 2. 前次问题的关闭状态

| 前次项目 | 本轮状态 |
|---|---|
| F1：场景专属节拍 | 三份工厂及 README 显式传入节拍已落实，新增真实提示测试通过 |
| F2：完整事件传输 | 原始汉字段首复现通过；数字段首编码损坏，见 R2 |
| F3：允许结局接线 | GM 专属约束、场景末拍工厂和真实提示测试已落实 |
| F4：Fountain 元素边界 | 多段对白/舞台指示修复通过；字面对白标记仍有 R5 |
| F5：重试统计 | 增加 API 重试/解析补问及持久化，但存在 R3、R4 |
| 格式及许可证头部 | 新增源文件 pyink/isort 通过，renderer 头部已修正 |
| Task 14 / 附录 D-4 | 已同步描述发送端编码和接收端解码 |

## 3. 本轮检查证据

| 检查 | 结果 |
|---|---|
| 故事专项及相关 contrib 测试 | 228 passed，54 subtests passed |
| `pytest -q concordia` | 1356 passed，33 skipped，56 subtests passed |
| `pytest -q examples` | 259 passed，38 subtests passed |
| `pylint --errors-only concordia` | 通过，无错误输出 |
| 全部 32 个新增 Python 文件 pyink/isort | 通过 |
| `pyrefly check concordia` | 仍有原先四项依赖错误，未见新增文件错误 |

pyrefly 四项仍为 vllm 的三处缺失导入和 `fastmcp.FastMCP` 的属性错误；
沿用前次报告的基线问题处理，不将其伪报为全绿。

上表的通过项是工程师已写测试的结果。R1–R5 来自本次另行执行的确定性
探针，说明现有测试没有覆盖相应输入，不代表测试数量增加即可关闭问题。

临时日志：`/tmp/story-phase1-recheck-targeted.log`、
`/tmp/story-phase1-recheck-concordia.log`、
`/tmp/story-phase1-recheck-examples.log`、
`/tmp/story-phase1-recheck-pylint.log`、
`/tmp/story-phase1-recheck-pyink.log`、
`/tmp/story-phase1-recheck-pyrefly.log`。

## 4. 放行条件

修复 R1–R5，增加对应回归测试并复跑规定检查后，再决定代码是否可进入
人工评审。用户安排的九次真实模型评审保持为代码审核通过后的独立步骤。
本轮不要求提前消耗真实模型预算验证可由确定性测试发现的缺陷。
