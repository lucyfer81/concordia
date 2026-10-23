# Phase 1 实现验收审阅

日期：2026-10-15。审阅版本：`489155b`。
设计基线：`docs/superpowers/specs/2026-10-13-story-enactment-phase1-design.md` v3。
实施基线：`docs/superpowers/plans/2026-10-13-story-enactment-phase1.md` 修订版。
实现前基线：`110157d`。

结论：**暂不通过 Phase 1 整体验收**。规定的两组 pytest 和 pylint 已通过，
但自动化测试未覆盖下述实际缺陷；真实模型人工验收结果也尚未提供。
本次只增加审阅笔记，未修改实现、测试、设计或计划。

## 1. 必须修复的实现问题

### F1 — 面试、婚礼场景错误复用老宅遗嘱节拍（P1）

位置：`examples/story_enactment/run.py:426`；
`examples/story_enactment/README.md:151`。

`make_run_config(story)` 无论接收哪份故事配置，都调用
`make_example_beats()`。README 的场景二运行示例因此也使用老宅节拍。
实际构造场景二、三得到的第一拍均为：

> 宣读遗嘱后的第一轮争执：各方表面立场浮现，可供质疑的疑点（封印、立遗嘱时间或程序问题）被摆上台面

面试和婚礼没有宣读遗嘱；第二拍仍指定旧信、录音残片、烧过的纸灰。
这些内容进入 GM 裁决上下文和节拍完成判定，会引入错误剧情要求。
三份 `StoryConfig` 存在不等于三份可运行验收场景已完成。

修复要求：让运行配置显式接收节拍序列；为三份场景分别提供匹配其冲突、
证据和结尾的节拍工厂。默认老宅示例可保留默认节拍。README 必须使用
场景与节拍一起构造运行配置的实际接口。

回归验证：通过 README 所示接口构造三份配置，检查各自节拍的描述、
完成问题、轮数边界及序列化恢复；使用确定性模型驱动每份配置的第一拍，
捕获真实裁决和完成判定提示，确认没有其他场景的节拍要求。

### F2 — 关系组件丢失事件内部的后续段落（P1）

位置：`concordia/contrib/components/agent/relationships.py:125`。

`pre_observe()` 按 `\n\n\n` 拆分整批观察，然后仅保留以 `Event: `
开头的片段。队列项目分隔符与事件内容没有隔离；事件本身包含三换行时，
后续段落被当作独立项目丢弃。

通过真实 `MakeObservation` 队列和角色 `observe()` 生命周期复现：

```python
event = 'Bob 放下信封。\n\n\nBob 承认信封里的信是自己伪造的。'
```

角色收到完整事件后，关系组件保存的 `recent_events` 只有：

```python
['Event: Bob 放下信封。']
```

“承认伪造”正是改变信任的证据，却不会进入下一次关系刷新提示。
如果后续段落恰好也以 `Event: ` 开头，还可能把一个事件计为两个事件。

修复要求：在 contrib 发送端建立可以还原事件内容和边界的明确编码，再由
关系组件解码；继续复用既有观察队列，不修改核心引擎。不能靠删除换行、
仅保留首段或禁止模型输出多段事件解决。

回归验证：真实队列一次投递含三换行的一个事件，计数必须为 1，关系提示
必须包含全部内容；一次投递两个事件，计数必须为 2；覆盖事件内出现
`Event: `、普通背景和 premise 混在批次中、保存恢复后的计数与内容。

工程师的“计划要求丢失片段”裁定不接受。计划 Task 14 和附录 D-4
确实指定了三换行拆分，这一细节存在歧义，应同步修订；其文字要求识别
“完整事件块”，并未授权丢弃事件内容。该计划细节也是我此前审阅未发现
的问题，需要连同实现修正。

### F3 — `allowed_endings` 是未接线的配置（P1）

位置：`examples/story_enactment/run.py:288`、`:426`。

允许结局仅出现在 `StoryConfig` 及其序列化结果中，没有进入
`make_gm_params()`、公开 premise 或当前节拍构造。末拍使用硬编码的
四类结尾，完成问题仅问“结局走向是否已经清晰并开始收束”。

实际验证：将允许结局替换为唯一标记
`ENDING_CONSTRAINT_PROBE_7309: only end with a signed settlement.`，
构建真实模拟并执行一步。配置保存了该标记，产生了一个最终裁决提示，
但所有模型提示均没有该标记。当前实现无法依据输入的结局范围收束剧情。

修复要求：将该故事的允许结局范围加入 GM 专属上下文，末拍的描述和
完成问题应依据该范围构造。保持“未发生事件不算完成”的判定约束；不将
未来节拍或结局指导广播给角色。

回归验证：更换允许结局后，真实 GM 裁决和末拍判定提示必须随之变化；
尚未发生任何允许结尾的夹具不能标记完成。结局是否语义上符合范围仍需
真实模型人工评审，不能用字符串包含断言替代。

### F4 — Fountain 对原始内容缺少元素边界处理（P2，验收前修复）

位置：`examples/story_enactment/renderer.py:104`。

渲染器直接拼接 `speech` 和 `stage_directions`，没有处理内容中的空段或
格式标记。对以下允许的字符串输入：

```python
speech = '第一段对白。\n\n第二段对白。'
stage_directions = 'INT. 灯箱上的字亮了。'
```

当前输出经项目自己的 `parse_fountain()` 解析后，第二段对白被标为
`action`，舞台指示被标为 `scene_heading`。这违反设计 §10 的字段到
元素映射；单行 fixture 全绿未覆盖此输入。

此外，`parse_fountain()` 在处理第一行对白后就退出对白状态，普通单换行
的第二行也会被它判成 action。需区分测试解析器缺陷和导出本身的格式
边界问题，不能仅调整解析器让测试接受错误输出。

修复要求：保留原始对白内容，按 Fountain 元素规则编码含空段的对白和
可能被误识别的舞台指示；修正解析器对完整对白块的处理。

回归验证：单换行、多段对白、以 `INT.` / `@` / `(` 开头的内容均覆盖；
对白各段仍属于同一发言者的 dialogue，舞台指示仍属于 action，只有真实
场景信息产生 scene heading。至少用独立 Fountain 解析器核对代表性输出。

### F5 — 成本记录缺少重试次数（P2，契约缺项）

位置：`examples/story_enactment/run.py:161`、`:1093`；设计 §13.3。

`RunResult` 和 `summary.json` 记录模型尝试次数，但没有重试次数。
所有调用共用预算，失败请求也扣减，这部分正确；总尝试次数不能说明其中
多少次是 API 重试。

修复要求：独立记录 API 重试次数，明确其是否包含输出解析后的第二次
评分调用；建议将解析补问单列，避免两者混为同一指标。恢复后的累计值和
本次进程值应有明确口径。不修改既有核心 wrapper。

回归验证：首次成功为 0 次重试；先失败一次后成功为 1 次；三次请求均
失败为 2 次；预算阻止的请求不计为已发起重试。token 不可获得时保持
`null`，当前这一行为正确。

## 2. 自动化检查结果

以下为本次复跑结果，不引用工程师口头结论。

| 检查 | 结果 |
|---|---|
| 故事示例及新增贡献组件专项 pytest | 191 passed，39 subtests passed |
| `pytest -q concordia` | 1336 passed，33 skipped，56 subtests passed |
| `pytest -q examples` | 242 passed，23 subtests passed |
| `pylint --errors-only concordia` | 退出码 0，无错误输出 |
| `pyrefly check concordia` | 退出码 1，4 个已有依赖错误；涉及文件与 `110157d` 完全相同 |
| 新增 28 个 Python 文件的 isort 检查 | 通过 |
| 新增 28 个 Python 文件的 pyink 检查 | 2 个测试文件需要格式化，其余 26 个通过 |

pyrefly 的具体问题：`vllm_model.py` 的三处 vllm 导入，以及
`file_reader_server.py:28` 的 `fastmcp.FastMCP` 属性。没有发现本次新增
文件的 pyrefly 报错，但不能将全量 pyrefly 记为绿色。应记录为基线问题，
由验收负责人明确豁免，或补齐匹配的依赖后重新通过。

pyink 未通过文件：

- `concordia/contrib/language_models/budget_wrapper_test.py`
- `concordia/contrib/components/game_master/story_resolution_test.py`

AGENTS.md 要求遵循 pyink；“CI 没检查”不能替代格式要求。修复这两项
属于机械格式整理，不需要保留所谓逐字测试代码例外。另需将
`renderer.py` 文件头的 `WITHOUT WARRANTIES OR ANY KIND` 修正为
仓库使用的完整 Apache 2.0 头部文本。

检查环境说明：HTTP 测试与 pylint/pyink 多进程需要本地套接字；沙箱
禁止套接字造成的失败已在获准环境复跑，以上表格使用复跑结果。

额外执行 `pytest -q concordia examples` 合并收集时，20 个旧 examples
失败：既有 `get_package_classes()` 扫描到已导入的
`concordia.prefabs.game_master.interrupt_driven_test.ScriptedByEntityModel`，
尝试无参实例化。相关核心文件与基线完全相同；设计规定的两项独立命令
均通过，因此本项不作为故事实现新增回归。

本次日志保存在 `/tmp/story-phase1-targeted-pytest.log`、
`/tmp/story-phase1-concordia-pytest.log`、
`/tmp/story-phase1-examples-pytest.log`、
`/tmp/story-phase1-pylint.log` 和 `/tmp/story-phase1-pyrefly.log`。
这些是本机临时证据；正式验收时需归档，不能依赖 `/tmp` 长期留存。

## 3. 工程师裁定核对

下列裁定可接受：使用真实框架要求的 Yes/No options；基于
`dialogic_and_dramaturgic` 装配 GM；使用 Constant 分离角色资料和秘密；
依据实际 StepData 字段及路径；修复一次确定性 putative 前缀；
`run()` 的 None 预算参数、额外 `run_config` 参数与局部 checkpoint
适配器；评分窗口按 `scoring_window` 接线；保留临时验证字段；
`character_depth` 累积导演能力；情绪键统一从 prefab 导入。

从 `110157d` 到审阅版本的 diff 仅有 contrib 与 examples 新源码以及
所属 README，未修改既有核心 Python 文件。当前工作区只有预先存在的
`.gitignore` 修改。直接在 main 提交及未提交 .gitignore 不构成运行契约
问题；本次未变更该文件。

打包检查只在开发会话运行可以接受，但交付记录应保留实际构建、安装后
导入的命令与退出结果。本次没有独立复跑安装包导入，不将它计为已验证。

不能接受的裁定：事件片段丢失（F2）、用“无 CI gate”豁免格式要求。
已有依赖错误只能作为明确记录的基线例外，不能写成全量检查通过。

## 4. Phase 1 剩余验收门槛

1. 修复 F1–F5，并加入上面列出的输入与预期结果对应的回归测试；同步
   修订 Task 14 / 附录 D-4 与 README 的场景运行示例。
2. 整理两个测试文件格式和 renderer 许可证头部。复跑规定的两组 pytest、
   pylint、isort、pyink；pyrefly 补齐依赖通过，或由验收负责人记录基线
   豁免及具体四项错误。
3. 提供设计 §13.2 要求的三场景各三次真实模型结果。目前仓库中未见实际
   人工评审结果，也尚未收到外部结果路径，状态是“未验证”，不是“通过”。
   每次必须保存配置、匹配的节拍、模型标识、提示版本和输出；按 turn_id
   记录角色区分、因果连续、秘密泄漏、对白重复、导演越权、结尾状态。
   在运行前固定通过阈值，并完成张力观察与人工判断的比较，再启用反馈。

现有结果足以说明主要框架接线、提交边界、预算停止和恢复机制已具备
可测试实现；不足以证明三场景剧情控制、完整关系证据、合法导出和真实
模型剧情质量均已达到 Phase 1 标准。
