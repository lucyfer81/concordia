# 故事演绎 Phase 1 设计审阅笔记

日期：2026-10-13  
审阅对象：[Phase 1 设计文档](../specs/2026-10-13-story-enactment-phase1-design.md)  
依据：本目录的 Concordia 源码、README、CONTRIBUTING、CHEATSHEET 和组件文档。  
状态：修订建议，尚未合并到设计文档；未实现功能或运行功能测试。

## 1. 审阅结论

保留以下决策：单场景、4–6 个角色、自动演绎、Concordia 组件架构、
平台层独立仓库、确定性剧本渲染、不修改核心引擎。

实施前应修订六项内容：剧情步的触发条件、裁决上下文接线、剧本数据采集、
角色输出格式、节拍超时与终止原因、配置序列化与恢复。现稿的 MockModel
验收只能验证程序流程，还需增加真实模型的剧情质量验收。

下文区分源码已经提供的行为与建议新增的行为。建议中的字段名是候选契约，
不是本仓库已经存在的 API。

## 2. 必须修订的接口与行为

### 2.1 以已裁决事件计数，不以 update() 调用次数计数

对应设计：第 4.1 节 BeatTracker、TensionMonitor；第 5 节数据流。

源码事实：

- [EntityAgent](../../../concordia/agents/entity_agent.py) 在每次 `act()`
  和 `observe()` 后调用组件的 `update()`，各组件的方法并行执行。
- [Sequential](../../../concordia/environment/engines/sequential.py) 在一个
  引擎步中多次调用 GM，包括终止查询、观察生成、角色选择和事件裁决。
- [Memory](../../../concordia/components/agent/memory.py) 禁止在 UPDATE
  阶段调用 `scan()`、`retrieve_recent()` 和 `add()` 等记忆操作。

修改建议：

1. 定义 `story_turn`：一个角色动作完成一次 GM 裁决后产生的剧情步。
2. 节拍判断和张力评估只处理新的已裁决事件。初始化 premise、
   `[putative_event]` 和 GM 管理查询不增加剧情步计数。
3. 在允许读取记忆的阶段准备事件快照，或直接从已裁决事件观察构建快照。
   `update()` 只提交本地已计算状态和清理生命周期缓存。
4. 使用一个明确的顺序入口协调节拍判断与张力评估，不依赖两个组件的
   `update()` 执行顺序。
5. 每个事件记录演绎该事件时的 `beat_id_at_action`，以及处理后得到的
   `beat_id_after_event`。张力分数与该事件演绎时的目标区间比较。
6. 节拍转换使旧节拍的待执行导演指令失效；新节拍的反馈基于新事件生成。

验收条件：重复执行 GM 管理查询不增加剧情计数、不调用剧情评估 LLM；
一个已裁决事件只评估一次；并行组件调用不改变节拍和评分所属的节拍。

### 2.2 显式声明 EventResolution 的上下文依赖

对应设计：第 4.1 节 BeatTracker；第 4.3 节 prefab；第 5 节数据流。

源码事实：

- [SwitchAct._resolve()](../../../concordia/components/game_master/switch_act.py)
  在存在裁决组件时直接返回该组件的结果。`component_order` 只控制
  SwitchAct 的上下文拼接，不将上下文传入裁决组件的 LLM 提示。
- [EventResolution](../../../concordia/components/game_master/event_resolution.py)
  通过自己的 `components` 参数读取依赖组件的预动作值。
- [dialogic_and_dramaturgic](../../../concordia/prefabs/game_master/dialogic_and_dramaturgic.py)
  默认裁决链只有 `RemoveSpecificText`，不生成额外的动作结果或舞台事件。

修改建议：

- 在 `EventResolution(components=...)` 中明确配置最近事件、当前节拍、
  世界约束等依赖键。
- 提供 EventResolution 所要求的 `get_pre_act_value()` 和
  `get_pre_act_label()` 接口；可复用 `ActionSpecIgnored`。
- 明确导演步骤如何访问待执行指令：通过绑定组件的 callable 或工厂组装，
  保持现有裁决步骤的三参数签名，不使用模块级全局状态。
- 定义普通裁决步骤：处理角色行动意图、生成实际结果、输出舞台事件。
  导演步骤只增加受约束的环境或 NPC 事件。
- “无指令零额外 LLM 开销”仅适用于导演步骤；普通裁决是否调用 LLM
  应单独写明。

验收条件：裁决模型收到的提示确实包含当前节拍和世界约束；无导演指令时
仍产生符合输出契约的结果；导演步骤不能改变其他玩家角色的主动选择。

### 2.3 增加 StoryTurn 采集适配器

对应设计：第 6 节剧本渲染器。

源码事实：[StepData](../../../concordia/environment/step_controller.py) 包含
步号、当前角色、角色动作、角色日志和 GM 名称；不包含 GM 裁决结果、
GM 日志、节拍或张力。因此 `step_callback` 不能单独提供全部渲染数据。

修改建议：在 `examples/story_enactment/` 增加采集适配器。适配器接受
StepData，并通过明确的 GM 数据接口或约定日志通道补齐字段。完成后保存
普通 JSON 记录；渲染器只读取这些记录，不访问正在运行的 Simulation。

建议的最小 StoryTurn 契约：

| 字段 | 含义 |
|---|---|
| `turn_id` | 整个演绎的剧情步序号；恢复后继续递增 |
| `scene_id` | 场景标识 |
| `actor` | 本步唯一的行动角色，由运行数据确定 |
| `speech` | 本步实际说出的对白 |
| `resolved_event` | GM 确认发生的事件 |
| `emotion` | 当前发言角色的表演提示；未启用时为空 |
| `beat_id_at_action` | 本步动作生成时所属的节拍 |
| `beat_id_after_event` | 本步处理完成后的节拍；全部完成时为空 |
| `tension` | 本步有效评分；未评估或评分失败时为空 |

场景显示信息单独配置 `interior/exterior`、`location`、`time_label`。
现有 SceneSpec 没有独立的内外景或地点字段，不应假定可以直接读取。

验收条件：JSON 保存与加载后，渲染结果保持一致；没有重复对白；没有把
其他角色上一轮的日志当成本轮台词或情绪；停止后仍可导出已完成的剧情步。

### 2.4 明确角色对白与 GM 舞台事件的分工

对应设计：第 5、6 节。

源码事实：[ConcatActComponent](../../../concordia/components/agent/concat_act_component.py)
默认添加 `"角色名 "` 前缀，而不是 `"角色名: "`。Sequential 还会检查并
添加角色名前缀。FREE 输出也不会验证文本是否仅含对白。

推荐的首版契约：

- 角色配置 `prefix_entity_name=False`。
- 角色只输出自己的对白正文，不输出角色标签、他人对白、旁白或推理过程。
- 采集器从 StepData 获取发言者，并只剥离引擎添加的一次确定前缀。
- GM 舞台事件描述可观察的环境变化、NPC 行为和行动结果，不复述对白。
- 未发生的行动意图不能进入 `resolved_event` 或舞台指示。

如果首版需要角色执行非言语动作，则改为明确的 `speech` 与
`action_intent` 结构化输出，并定义验证和裁决路径。纯文本前缀解析不能
可靠地区分这两种内容。

Fountain 导出应明确使用角色行、对白行、括号提示和场景标题的标记规则。
中文角色名使用 `@角色名` 强制角色标记；场景标题使用 `INT.`、`EXT.`
或明确的强制场景标记。解析测试应检查元素类型与内容，不能只断言文本非空。
参考：[Fountain 官方语法](https://fountain.io/syntax/)。

### 2.5 定义节拍预算、超时策略与终止原因

对应设计：第 4.1 节 BeatTracker、终止组件；第 9 节验收标准。

当前设计只定义节拍完成判断和全局上限，未定义某拍始终不完成时的行为。
建议每拍增加稳定的 `id`、`min_turns` 和 `max_turns`，并保存已消耗步数。
完成判断必须引用已发生事件，不能把角色意图或未来大纲当作完成证据。

推荐首版默认行为：

- 达到 `min_turns` 后才允许按完成判定推进；每个剧情步最多推进一拍。
- 达到 `max_turns` 仍未完成时，终止并记录 `beat_timeout`；不默默跳拍，
  不强制角色作出预定选择。
- `SceneOrBeatTerminator` 保留 SceneTracker 所需的 `terminate()` 行为，
  并公开可读取的终止原因。
- 运行结果区分 `beats_completed`、`beat_timeout`、`scene_limit`、
  `step_limit`、`budget_limit` 和 `error`。只有全部节拍完成才标记故事完成。

[SceneTracker](../../../concordia/components/game_master/scene_tracker.py) 当前
在每次 RESOLVE 时增加一次场景计数。在本方案的 Sequential 引擎中，
`num_rounds` 应解释为角色动作裁决次数，不是所有角色各行动一次的轮数。
场景预算与节拍预算必须使用同一计数单位。

验收条件：覆盖正常收束、节拍超时、场景上限和全局上限；所有路径都返回
明确原因，且不把截断的剧情标记为已完成。

### 2.6 将发现、编辑、序列化和恢复分别验收

对应设计：第 3 节“自动获得”能力；第 8、9 节 checkpoint 验收。

源码事实：

- [prefab_catalog](../../../concordia/utils/prefab_catalog.py) 可以发现
  contrib 下的 prefab，但不会补齐构建所需的复杂参数。
- [Simulation 的序列化](../../../concordia/prefabs/simulation/generic.py)
  会跳过不支持的对象值；对象列表中的项可能被转换为 `None`。
- GUI 动态编辑还需要组件声明 `get_dynamic_state()`，并在 `set_state()`
  验证输入。实现 `get_state()` 不等于支持全部 GUI 编辑能力。
- 当前 checkpoint 恢复不承诺恢复引擎局部游标或使真实模型输出确定。

修改建议：

- 明确 Beat、SceneSpec 与组件构建参数的 JSON 编解码方式。
- 运行时注入的 `extra_components` 不能作为可独立重建的 JSON 配置；
  提供明确工厂或 prefab 配置，由每次构建创建新组件实例。
- 保存节拍配置与游标、剧情计数、评估计数、导演指令状态、关系缓存。
- 如不实现完整 GUI 编辑，将承诺改成“支持 catalog 发现与状态检查；
  复杂配置由 Python 示例提供”。
- 将“读取已保存事件得到相同剧本”与“恢复后调用 LLM 继续生成”分开。
  前者应确定；后者不承诺真实模型续写完全相同。

验收条件：checkpoint 经过 `json.dumps/loads` 后，在新的 Simulation 中
恢复并继续运行；恢复后的新剧情步不重复、不丢失，已消费指令不再次执行。
不能只在原有组件实例上测试 `get_state/set_state`。

## 3. 需要补充的剧情机制

### 3.1 明确故事输入与角色知情范围

增加一份人工编写的示例配置，包含：

- 世界事实：地点、时间、在场人物、已有证据、允许的环境事件。
- 每个角色的身份、目标、底线、秘密、初始关系和表达风格。
- 信息分配：哪些事实公开，哪些仅 GM 知道，哪些仅指定角色知道。
- 冲突起点、允许的结局范围，以及本场景不允许改变的事实。

角色只接收自己的信息。GM 全量设定、未来节拍和导演指令不能直接作为
所有角色的观察广播。至少增加一个“角色尚未获知秘密”的场景测试。

### 3.2 节拍描述叙事条件，不预定角色选择

将“角色甲认罪”改成“出现可供角色质疑的证据”；将“角色乙原谅甲”改成
“角色有机会回应已经揭示的事实”。完成条件允许角色作出不同回应。

导演仅使用配置允许的 NPC 或环境事件；不能改写既有事实、凭空修改
玩家角色记忆，或替角色生成主动选择。保留玩家能动性的分工可参考
[Concordia Design Pattern](https://arxiv.org/abs/2507.08892)。

### 3.3 给张力评分和导演指令定义操作规则

增加以下配置与记录：

- 评分窗口：最近多少个已裁决事件。
- 评分量表：明确 1、5、10 分对应的可观察叙事特征。
- 比较规则：低于 `lower - tolerance` 或高于 `upper + tolerance`
  才判定偏离；等于阈值时不触发。
- 冷却时间：执行一条导演指令后，多少剧情步内不生成下一条。
- 指令字段：所属节拍、生成步号、内容、待执行或已消费状态。
- 指令生命周期：只执行一次；节拍转换后失效；执行失败记录原因。

评分提示只接收事件和统一量表，评分完成后由程序与目标区间比较。
张力分数是启发式指标，不作为故事质量已经提升的证明。

评分重试仍失败时，本次评分为空且不触发新指令。上一有效值可用于展示，
但不得作为本次有效测量参与控制。API 重试耗尽应明确报告失败，不用
“未完成”或“无指令”掩盖模型服务持续不可用。

### 3.4 明确关系缓存与情绪解释的语义

[ActionSpecIgnored](../../../concordia/components/agent/action_spec_ignored.py)
在每次 `update()` 清空预动作缓存，不能自动实现“每 K 轮刷新”。
RelationshipsToOthers 仍需要持有跨步派生结果与刷新计数，并写入 checkpoint。
刷新单位建议定义为该角色收到的已裁决事件数。

关系感知只从该角色观察到的事件派生；不维护新的权威关系事实。
这种缓存不等于新增一套需要与 GM 同步的关系状态。

[EmotionalStance](../../../concordia/contrib/components/agent/emotional_stance.py)
选择一个情绪，并允许为情绪语境细微改写观察内容。改写结果只能作为
角色主观解释，不能回写成 GM 世界事实。渲染时只读取当前发言角色的
情绪标签；是否显示括号提示由导出选项控制。

第 11 节应补齐参考文献的作者、年份、标题与所支持的具体机制。
节拍弧线和 LLM 情绪选择应表述为设计启发，不能仅凭参考文献将实现描述为
已验证的心理学模型。

## 4. 补充验收与成本记录

### 4.1 确定性测试

CI 继续使用 MockModel 或按提示类型返回结果的确定性 stub，不调用真实 API。
完整场景测试不能仅靠固定回答让每拍自动完成。

新增测试至少覆盖：

1. GM 管理查询不触发剧情计数或评估；一个已裁决事件只处理一次。
2. 裁决提示包含节拍与世界约束。
3. 完成、不完成、超时、评分无效和各类上限终止。
4. 导演指令只执行一次，跳拍后失效，恢复后不重复消费。
5. 角色信息隔离、对白与舞台事件分离、中文 Fountain 元素解析。
6. 新建 Simulation 的 JSON checkpoint 恢复，以及恢复后的连续 turn_id。

测试使用固定 embedder；如果选择题随机化会影响 stub 的结果，则显式关闭
随机化或让 stub 按答案语义选择，避免断言依赖线程调用顺序。

### 4.2 真实模型的人工验收

作为 CI 外的单独验收，建议固定三份冲突场景配置，每份运行三次，保存配置、
模型标识、提示版本和输出。逐项记录角色区分、因果连续、信息泄漏、对白
重复、导演越权和结尾状态，并为每个问题标注对应 turn_id。

发布前确定各项通过标准。节拍按序推进或张力落入目标区间不能替代这些检查。

### 4.3 运行成本与中断结果

记录模型调用数、可获得的 token 用量、单步与总耗时、重试次数和终止原因。
模型不提供 token 用量时记录为空，不能记作零。预算统计需覆盖重试请求。

[CallLimitLanguageModel](../../../concordia/language_model/call_limit_wrapper.py)
达到上限后返回空文本或第一项选择，不会自动终止模拟。设计中应另行明确
预算耗尽的停止路径，并保存已经完成的 StoryTurn，避免把空响应当作正常剧情。

## 5. 建议的实施顺序与后续复用

建议将 Phase 1 分成三个验证阶段；这是范围调整建议，尚未替换原设计目标。

1. 固定角色与信息分配、单场景、节拍推进、输出契约、剧本导出、恢复测试。
2. 启用张力评分，仅记录分数；与人工评审比较后，再启用导演反馈。
3. 增加动态关系感知与可选情绪提示，验证其对对白与角色区分的影响。

每阶段保留相同的示例配置和评价表，以便判断新增机制是否改善结果。

本目录已经存在以下后续能力，第 10 节应标注为复用对象：

- [StepController](../../../concordia/environment/step_controller.py)：
  play、pause、step、stop 与 paused_boundary。
- [HumanActComponent](../../../concordia/components/agent/human_act_component.py)：
  人类输入的行动组件。
- [HumanInput](../../../concordia/typing/human_input.py)：人类输入协议。

Phase 2/3 的新增工作应集中在导演指令协议、实时通道与产品集成。
checkpoint 分支续写与已保存事件回放需要分别设计和验收。
