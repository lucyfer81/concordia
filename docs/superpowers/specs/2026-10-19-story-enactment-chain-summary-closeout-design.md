# Story enactment：链路探针汇总修复与批次收尾开发指令

日期：2026-10-19。
状态：供工程师编写实施步骤；本文不代表修复已实施。

## 1. 目标与本轮边界

修复 E 阶段汇总漏验：人工已标注正式事件包含非故事性文字时，工具必须
判定相应模型、E 阶段及整个批次失败。补回归测试，修正批次 README 的
两处计数，再使用既有真实产物离线重新汇总。

目标批次：

`run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z/`

本轮新增真实模型调用必须为 **0**。不重新裁决、生成、复核或标注；不运行
完整故事对照；不修改生产提示、生产解析器、核心裁决器、模型适配器或
生产接线。保留现有工作区，不 commit、stash、clean、回滚或创建 worktree。

真实实验已完成 120 次调用：A 24、FINAL 48、E 48。此次修改的是汇总工具，
不应把旧调用记为新调用，也不把离线回归测试计入真实实验调用数。

## 2. 已确认的问题与事实

相关源码：

- [探针](../../../examples/story_enactment/adjudication_chain_probe.py)：
  `_summarize` 内部的 `e_metrics`。
- [共址测试](../../../examples/story_enactment/adjudication_chain_probe_test.py)：
  `SummarizeAccountingTest` 及其既有构造辅助方法。

当前 E 汇总会统计 `canonical_known_nonstory` 对已知片段的命中，却没有
读取人工标签 `canonical_nonstory_text`。在临时副本中，仅把
`E03__m4__r1` 的该人工字段改为 `true`，现有汇总仍给出
`stage_e.m4.pass=true` 和 `overall_pass=true`。

原批次的 24 条 E 人工标签均明确为 `canonical_nonstory_text=false`，
独立阅读原始输出也支持正式事件无此污染。因此修复后的原批次预计仍为
PASS；必须由修复后的工具重新计算，不能为了维持 PASS 调整任何标签。

## 3. 修改范围

实施计划原则上只需涉及：

1. `examples/story_enactment/adjudication_chain_probe.py`。
2. `examples/story_enactment/adjudication_chain_probe_test.py`。
3. `examples/story_enactment/README.md`：简要说明新增的离线重汇总用法及
   原始实验版本与汇总工具版本的区别。
4. 目标批次 `README.md` 的指定勘误及收尾说明。
5. 新增的离线重汇总产物与 docs 下的实施计划、交付记录。

复用既有汇总逻辑，不复制一份 E 计分器，不引入通用标签框架或新的模型
评审。工程师在实施计划中说明必要的小范围重构和实际 CLI 接口即可。

## 4. E 阶段通过条件

### 4.1 将人工污染标注接入 gate

在每模型 E 汇总中增加 `human_canonical_nonstory` 计数，与 FINAL 阶段
相同字段保持命名一致：统计该模型 E 标签中
`canonical_nonstory_text is True` 的条目数。

必须满足以下关系：

```text
任意一条 E 人工标签 canonical_nonstory_text=true
  → 对应模型 human_canonical_nonstory >= 1
  → summary.stage_e[model].pass=false
  → summary.stage_e_pass=false
  → summary.overall_pass=false
  → 顶层 overall_pass=false
```

该人工检查适用于 E 阶段所有用例，不限于 E03。保留已有的已知片段扫描、
分类、完整性、实质保留、英文误拒绝和违规点名检查；人工标签和字符串
扫描互相补充，不能用其中一项覆盖另一项。

### 4.2 标签含义与缺失处理

`canonical_nonstory_text` 表示获准正式事件的非故事性文字污染。被拒候选
中的拒绝说明仍记录在 notes 中；不要把本批 Sol 的六条被拒候选重新标成
已提交污染，也不要为此改写现有标签。

该字段必须是明确的布尔值。字段缺失、null 或非布尔值不能默认为 false
而通过；应明确报告标签不完整并阻止通过，或抛出清晰的标签校验错误。
只要求处理此次相关字段，不扩大为全量标签 schema 重写。

## 5. 回归测试要求

使用确定性 stub、临时目录和既有辅助方法，不依赖 API key、网络或真实
`run_results/` 目录的可写副本。先确认关键回归在原实现下 RED，再修复至
GREEN。

至少覆盖：

1. **关键漏验反例**：其余数据均合法、机器扫描
   `canonical_known_nonstory=false`，仅将一条 E 人工标签的
   `canonical_nonstory_text` 改为 true。必须检查新增计数、模型 gate、
   阶段 gate、两个层级的 overall_pass 均相应失败。
2. 使用参数化或 subtests 覆盖 m1/m4 与 E01–E04，确认逻辑不是只对
   E03 或某一模型生效。污染只属于一个模型时，另一个模型可保持通过；
   A/FINAL 的通过状态不应被改写。
3. 全部明确为 false、其他条件满足时仍 PASS，新增计数为 0。
4. 字段缺失、null、非布尔值不能通过。
5. 既有已知片段扫描仍能独立阻止通过。
6. 离线重汇总不构造真实模型客户端、不调用模型或运行阶段；相关工厂
   和调用路径用会立即报错的 spy 阻断，不能只检查一个计数变量为 0。

## 6. 解决修复后源码与冻结快照不一致的问题

### 6.1 必须处理的现有约束

当前 `_summarize` 会调用 `_verify_frozen`，后者要求当前所有源码哈希与
批次 `source_snapshot.json` 完全相同。探针和测试本身也在快照中。
因此修改汇总后，原有命令直接处理旧批次会因源码变化而失败。

不能重写原 `source_snapshot.json` 来假装新汇总器就是实验时使用的版本，
也不能通过 mock 掉校验、全局关闭哈希检查或临时还原代码完成正式交付。

### 6.2 推荐的最小实现契约

提供**显式的离线重汇总入口**，读取原批次，向新的独立目录输出修正报告。
具体参数名由实施计划确定，交付时给出真实可执行命令。该入口：

- 复用修复后的同一套汇总逻辑，不维护另一套计算规则。
- 不读取凭据、不要求 `.env`、不构造客户端、不进入 run_a/run_final/run_e。
- 校验原冻结文件的哈希与原报告中的记录一致；输入来自原批次的固定
  文件，不调用当前 fixture 工厂重新生成或替换实验输入。
- 开工前保存原批次输入文件的哈希清单，重汇总时复核。清单覆盖原始
  `report.json`、protocol/fixtures/source_snapshot、三阶段请求与响应及
  result/started 文件、人工标签和工作表等实际输入。指定勘误的
  `README.md` 及新增的重汇总输出不属于只读输入。
- 单独记录当前汇总工具源码快照及相对实验快照的差异。已快照文件中的
  源码差异仅允许此次获准的工具和测试修改；生产文件必须保持一致。
- 输出关联原批次路径、原报告哈希、输入清单哈希、当前汇总工具快照、
  重新汇总时间及 `new_real_api_calls=0`。
- 保留原 `report.json`，修正报告放在新输出目录中。批次 README 链接新
  报告，并明确旧报告为原汇总版本，避免把新工具版本混同为实验版本。
- 输出目录不得覆盖既有重汇总报告。实际实验调用数仍为 120，阶段分项
  仍为 24/48/48，不将其重置为 0。

冻结、真实执行及原有严格汇总路径的源码校验继续有效。新增离线入口
不能成为真实执行路径可使用的通用“忽略源码差异”开关。

离线测试还须证明：冻结输入或响应发生未登记变化时拒绝重汇总；生产
源码变化不能被当成本次汇总修复放行；旧批次文件在处理前后保持不变。
这些测试使用临时数据，不修改真实产物。

## 7. README 指定勘误

仅修改目标批次 README 中以下事实，并追加本次收尾说明：

1. Gemini provider 返回标识：`72/72` 改为 **60/60**。
   来源为每模型 A 12 + FINAL 24 + E 24 个真实响应。
2. 描述 m1 在 E02/E04 中省略越权、被复核拦截的那一句，`12/12` 改为
   **6/6**，因为两例各重复三次。可以另注明两模型负例合计为 12/12。
   不要替换表中各模型整个 E 阶段正确的 `12/12`。

继续披露 Gemini 请求名与返回名不一致，不能将本次计数勘误写成路由
身份已经查明。正文引用新离线报告和工具版本，说明新增真实调用为 0。

## 8. 验证与交付

所有终端操作使用 Bash 并先激活 `venv`。实施计划应包含关键回归 RED/
GREEN、相关探针测试及 AGENTS.md 要求的适用检查，例如：

```bash
source venv/bin/activate
pytest examples/story_enactment/adjudication_chain_probe_test.py
pytest examples
pytest concordia
pylint --errors-only concordia
pyrefly check concordia
```

对实际改动的 example 工具和测试另跑 pylint、pyrefly 适用检查及
pyink/isort。记录命令、退出码和错误详情；若仍有此前已知的四项依赖
类型错误，需核实并与基线区分，不能无条件宣称全部静态检查通过。

交付须包含：

- 实施计划、改动文件清单、测试结果和实际离线重汇总命令。
- 关键反例：只改一个人工污染标注，机器扫描仍为 false，批次由 PASS
  变 FAIL；恢复 false 后重新 PASS。只在临时数据中执行。
- 原始输入前后哈希一致的证明及新工具源码差异清单。
- 新报告路径与来源记录。原数据预计仍为 overall_pass=true，各模型
  E 阶段 human_canonical_nonstory=0，调用总数 120，新增真实调用 0。
- README 两处计数勘误和新报告链接。

完成后停在工程审阅点。本轮不启动 Gemini/Sol 的四组完整故事运行，
不把此次离线汇总修复描述为新增真实模型验证。
