# 真实模型问题修复交付记录

日期：2026-10-15。方案：
`../plans/2026-10-15-story-enactment-live-fixes.md`。
按用户授权，由 resolution、narrative、director 三个 subagent 分工完成，
主代理审阅、整合归档与文档并执行跨模块回归。未提交 git；保留已有修改
及两份历史真实运行产物；没有发起新的真实模型调用。

## 已交付

- GM 收到每个角色的底线、全部玩家名单、环境白名单、不可变事实和
  GM 私密约束，并单列初始公开事实。白名单在所有四种模式下接线。
- 最终双轨输出先通过严格 JSON 校验，再通过一次独立结构化语义复核。
  复核只读公开背景、已提交事件和本次对白，并使用 GM 约束判断越权、
  私隐传播、事实矛盾及两轨不一致。无效或拒绝时明确 error，不广播、
  不提交、不消费指令；API/预算异常走既有路径。
- 通过后程序使用原始对白和旁白构造正式事件，确保剧本与角色观察的
  旁白来自同一字符串。任意换行和空白的对白不改写。
- 节拍以公开、已发生的事实判断，允许结局的每项条件均须成立；请求
  核验不算核验完成，要求签字不算众人已经签字。保留超时退出。
- 保留角色硬底线，修复老宅、面试、婚礼配置中的强制坦白结局冲突，
  匹配末拍工厂与实际 GM 生成/复核/完成提示同步。
- 对白提示强调自身目标、风格与新信息；张力量表区分重复争吵和新风险。
  导演建议带近期公开历史，禁止以角色放弃立场来降张力；无法找到合法
  新事件时允许不生成指令。
- StoryTurn 新增 archive-only director_diagnostics，包含轮次开始、裁决后、
  整轮后的指令状态以及执行/失效/失败轮次、冷却信息。旧记录仍可加载。
- run_manifest.json 在执行前保存脱敏配置与相关源码/提示快照及 SHA-256；
  恢复追加调用记录，不覆盖旧快照。旧运行原始版本不可恢复时明确标记
  history_complete=false。JSONL 与 checkpoint 的既有提交边界保持不变。

## 验证

| 检查 | 结果 |
|---|---|
| 故事及相关 contrib 跨模块专项 | 222 passed，81 subtests passed |
| pytest concordia | 1380 passed，33 skipped，75 subtests passed |
| pytest examples（含补充三场景配置修复） | 300 passed，64 subtests passed |
| 归档与四模式复核白名单定向 | 8 passed，4 subtests passed |
| pylint --errors-only concordia | 通过；使用 -j 1，避免沙箱多进程套接字限制 |
| 本次归档/入口/采集模块及测试局部 pyrefly | 0 errors |
| 三个 subagent 的相关模块 pyrefly | 均通过 |
| pyrefly check concordia | 仍有原先 4 项依赖错误，无新增源码错误 |
| 修改文件 pyink、isort、git diff --check | 通过 |

全量 concordia 类型基线为 vllm 三处缺失导入和 FastMCP 属性问题。
额外检查整个 examples/story_enactment 时，已有 renderer_test.py 的三个
minidom 类型错误仍在，未修改该文件或添加豁免。本次新增模块和修改入口
的类型问题已修正，不能将全仓类型检查记为全绿。

回归包含 Luna T7 代认罪/泄密/集体同意、Gemini T18 旁白单独签字的
缩减输入；同时覆盖合法自我披露、提议、未证实指控、旁白一致性、复核
输出无效、API 失败、预算耗尽、旧 checkpoint 和导演指令恢复。
测试使用 MockModel 和确定性 stub，脚本化复核结论只证明拒绝后的系统
行为，不能证明真实模型一定判断正确。

日志位于 /tmp/story-live-fixes-*.log；本文件保存关键计数，临时日志不作为
长期归档。真实运行应完整保存 manifest、checkpoint、JSONL、summary 和
剧本，不能仅保留导出文本。

## 真实模型复测条件

1. 使用新目录，不改写旧运行；保存当前源码快照、模型身份与配置。
2. 先在 tension_observe 模式比较人工判断和张力曲线，再评估 director
   是否改善节奏；每轮多一次语义复核请求，计入现有 400 次默认预算。
3. 按三场景各三次的原验收要求逐轮评审角色区分、因果连续、秘密泄漏、
   重复、GM 越权、结局；同时统计复核拒绝及误拒绝，不只统计完成率。
4. 本次交付是工程修复和确定性验证完成，剧情质量的真实模型复验尚未
   执行；不能提前宣布 Phase 1 人工验收通过。
