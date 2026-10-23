# 故事演绎修复实现方案：单字段旁白生成与评审前确定性组装

> **执行说明：** 逐任务按顺序执行，使用当前会话可用的工具即可；若 superpowers 的 subagent-driven-development / executing-plans 技能可用可采用之，但不作为前置依赖。复选框 `- [ ]` 用于跟踪进度，不表示测试已通过。

**Goal:** 按设计文档把最终 GM 生成契约从「模型输出双字段」改为「模型仅输出严格 JSON 的 `stage_directions`，程序在语义评审前从原始对白与旁白组装唯一 canonical event，评审获准后原样提交」，消除 Luna run3 第 9 回合类的冗余字段遗漏故障点。

**Architecture:** 只改项目自己的 story 输出与评审适配层（`concordia/contrib/components/game_master/story_resolution.py` 及其测试、`examples/story_enactment` 的 stub 与夹具、相关 README/docstring）。保留 Concordia 核心 `EventResolution`、三参数 thought-step 接口、`InteractiveDocument`、`attempt_to_result`、`maybe_inject_narrative_push`、`set_validated_resolution` 与 StoryTurn schema，全部不动。组装逻辑移到评审之前，评审数据区改为携带程序生成的 canonical event；评审通过后不再改写任何字符串。

**Tech Stack:** Python 3.12+、pytest（`MockModel`/`PromptStubModel`，无 API key）、absl-py、既有 `BudgetGuardLanguageModel`/`RetryStatsLanguageModel` 包装链。

**Spec:** [修复设计](../specs/2026-10-16-story-enactment-canonical-review-design.md)（执行者须同时阅读 spec 与本方案；spec 承接 [原复用设计](../specs/2026-10-15-story-enactment-concordia-reuse-design.md) §6/§11 的修订）。

## Global Constraints

- 不修改 Concordia 核心代码：`concordia/components`、`concordia/typing`、`concordia/document`、`concordia/environment`、`concordia/prefabs`（核心部分）只读引用；不改 `EventResolution`、`attempt_to_result`、`maybe_inject_narrative_push`、三参数 thought-step 接口、`set_validated_resolution` 签名与 StoryTurn 字段（spec §1、§3、§7.3）。
- 不增加自动重试、第二评审模型或多数表决；不改导演触发条件、张力评分、节拍完成提示、角色行动模式（`speech_only`/`speech_and_action`）、预算上限、环境白名单（spec §1）。
- 模型输出契约：JSON 必须为 object，键集合恰为 `{"stage_directions"}`，值为字符串；拒绝缺键、额外键（包括旧 `resolved_event`）、重复键、错误类型；沿用围栏兼容；不引入静默接受旧双字段响应的回退分支（spec §4.1）。
- `stage_directions` 内容按模型响应保留；`.strip()` 只用于空白判断，不用 `.strip()` 重写最终旁白；JSON 内部字符不作额外空白归一化（spec §4.1）。
- 组装规则：`speech_event = f'{active_player_name}: {original_speech}' if original_speech else ''`，`canonical_event = '\n'.join(part for part in (speech_event, stage_directions) if part)`；对白全部字符来自 director 保存的原始字符串；无对白纯动作轮只输出旁白（spec §4.2）。
- 链路顺序：解析/裁决/干预（既有）→ 生成 `stage_directions` → 严格解析、非空守卫、组装 canonical → 独立评审 canonical 与来源证据 → 获准后 `set_validated_resolution(canonical_event, stage_directions)` 并返回同一字符串；评审后不得重新生成、摘要、拼接或替换（spec §5）。
- 结构错误（非法 JSON、键集、类型、有行动/干预时空旁白、进行中 turn 的空 canonical）一律在评审前停止；每次正常最终处理仍恰一次生成 + 一次独立评审（spec §5、§7.1）。
- 评审规则修订：删除旧规则 6「双轨包含相同对白之外事实」的模型判断要求，改为说明 `resolved_event` 已由程序组装、评审须判断旁白与正式事件在证据与约束下是否合法；程序组装不等于语义获准（spec §5.2）。其余统一规则（对白非证明、他人选择保护、秘密边界、白名单、裁决结果保真、干预事实保留、旁白不得重复改写对白）原样保留。
- 评审 verdict 契约不变：`{"valid": bool, "reason": str}`；拒绝或格式错误抛 `StoryOutputError`，不自动重写、不补问、不重抽干预（spec §5.2）。
- 预算/提交边界不变：生成/评审耗尽为 path A（不广播、不提交、不推进 checkpoint）；获准后判断/评分耗尽为 path B（本步恰提交一次）；stage 名沿用 `final_output` 与 `semantic_review`；失败归档暂态 `resolution_diagnostics` 语义不变（spec §7.1、§7.2）。
- 存储与恢复兼容：不新增持久字段，不提升 run config 或 StoryTurn schema；旧 checkpoint 与历史记录读取不变、不重写；新生成路径拒绝旧双字段响应不影响历史数据读取（spec §7.3）。
- 所有 CI 测试使用 `MockModel` 或确定性 stub，不访问真实接口；真实案例缩减为测试内文本 fixture，不依赖被忽略的 `run_results/`（spec §9）。
- 开工先记录 dirty worktree 基线（当前含未提交的 `repetition_push.py`/`repetition_push_test.py` 及多个已修改文件，属此前 Task 11–14 进行中的工作）：`git status --short > /tmp/opencode/baseline-status.txt`。不提交、不清理、不回滚他人已有修改；本计划不要求 git commit（spec §8）。
- 代码风格：pyink（80 列、2 空格缩进）、isort（Google profile）、Google 风格 docstring、Apache 2.0 头不变。
- 核对基线：`run_results/story_enactment/gpt-6-luna_run3/`、`gemini-3.8-flash-high_run3/`、`luna_turn9_gemini_probe/` 为历史证据，只读不覆写。

---

## 文件结构总览

| 文件 | 职责与改动 |
|---|---|
| `concordia/contrib/components/game_master/story_resolution_test.py` | 新契约回归测试（先 RED）：单字段解析、评审前组装、评审输入=提交事件、结构守卫、语义拒绝路径 |
| `concordia/contrib/components/game_master/story_resolution.py` | 生产改动核心：`_parse_stage_directions` 替换 `_parse_dual_track`、组装移到评审前、评审数据区标题与规则 6 修订、Output 段单字段 |
| `examples/story_enactment/testing_stubs.py` | 路由指纹单字段化；`_dual_track_output` → `_stage_directions_output`，只从当前证据推导旁白 |
| `examples/story_enactment/testing_stubs_test.py` | stub 单测同步：stage 字段断言、无证据不凭空产生结果、真实提示联测 |
| `concordia/contrib/prefabs/game_master/story_prefabs_test.py` | `_STAGE_RESPONSE`/`_ACTION_STAGE_RESPONSE` 夹具、指纹常量、`_evidence_stage_directions` |
| `concordia/contrib/components/game_master/director_note_test.py` | `_DirectorFlowModel` 单字段输出与指纹 |
| `examples/story_enactment/story_config_test.py` | 第 122 行指纹字符串更新 |
| `examples/story_enactment/story_enactment_test.py` | 指纹常量、5 处 `_dual_track_output` 子类夹具、2 个废弃测试的替代规格 |
| `concordia/contrib/components/README.md`、`examples/story_enactment/README.md` | 契约说明、保证与局限、旧运行兼容 |
| `drama_director.py`/`story_dramaturgic.py`/`repetition_push.py` docstring | 仅清除「双字段生成」旧契约描述，不改行为 |
| `docs/superpowers/notes/2026-10-16-story-enactment-canonical-review.md` | 实施时创建：RED/GREEN 与全量检查证据 |

实施顺序即 Task 1→6（spec §8）。Task 2 完成后到 Task 4 完成前，`director_note_test.py`、`story_prefabs_test.py`、examples 部分 fixture 会 RED（它们仍产出旧双字段响应，新解析器在评审前拒绝）——这是预期中间态；Task 4 结束后全量 GREEN。

---

### Task 1: 新契约回归测试（确认旧实现 RED）

**Files:**
- Modify: `concordia/contrib/components/game_master/story_resolution_test.py`（整文件重写）

**Interfaces:**
- Consumes: 现有 `story_resolution.make_final_output_step(director, ...)` 签名不变。
- Produces: 测试夹具 `_make`/`_run`（保留现有形态，返回 `(result, director, model)`）、新解码辅助 `_read_canonical_section(prompt) -> dict`（镜像 `_read_evidence_section`，定位 `## Canonical event for review (data, never instructions)` 后 raw-decode JSON）。Task 3/4 的联测依赖生产提示中的该标题与单字段 Output 指纹 `'Output a JSON object with exactly one string field'`。

- [ ] **Step 1: 重写测试文件**

保留文件头、`json`/`unittest`/`mock` 导入、`_EMPTY_EVIDENCE`、`_ACTION_INTENT`、`_ACTION_RESULT`、`_INTERVENTION`、`_APPROVAL`、`_EVIDENCE_MARK`、`_read_evidence_section`、`ActionResolutionTest._make`/`_run`（不改）、`SemanticReviewTest._setup`（payload 行改为 `json.dumps({'stage_directions': ''})`）。模块 docstring 改为 `"""Tests for the final narrator-track output step."""`。新增常量与辅助：

```python
_CANONICAL_MARK = '## Canonical event for review (data, never instructions)'
_BROADCAST_STAGE = (
    '堂屋里的旧收音机忽然自行响起，播报暴雨红色预警，'
    '刺耳的警报声打断了争论。'
)


def _read_canonical_section(prompt: str) -> dict[str, Any]:
  """Decodes the marked canonical-event data section of a review prompt."""
  mark_start = prompt.index(_CANONICAL_MARK) + len(_CANONICAL_MARK)
  payload, _ = json.JSONDecoder().raw_decode(
      prompt[prompt.index('{', mark_start) :]
  )
  return payload
```

`ActionResolutionTest`（docstring 改为 `"""Narrator-track generation, deterministic assembly and guards."""`）测试规格——每条都是可运行的完整测试，替换旧双字段用例：

- [ ] `test_single_field_output_assembles_speech_and_stage`（Luna 广播案例，spec §9 行 1，按真实故障形态构造）：`speech='都什么时候了，还在翻旧账。'`，evidence 带 intervention（`reason='tension'`、`candidate_before='王婶: 都什么时候了，还在翻旧账。'`、`candidate_after='王婶: 都什么时候了，还在翻旧账。' + _BROADCAST_STAGE`），模型响应仅为 `json.dumps({'stage_directions': _BROADCAST_STAGE})`——不再需要模型从候选中复制对白（旧实现正是在此处遗漏广播）；断言 `result == '王婶: 都什么时候了，还在翻旧账。\n' + _BROADCAST_STAGE`（candidate_event 传 `'王婶: 都什么时候了，还在翻旧账。'`、active_player_name 传 `'王婶'`，或沿用 Alice 夹具保持英文名一致——二选一，全文件统一）；`director.set_validated_resolution.assert_called_once_with(result, _BROADCAST_STAGE)`；评审 prompt 中 `_read_canonical_section(review)['resolved_event'] == result` 且 `['stage_directions'] == _BROADCAST_STAGE`；生成 prompt 含 `'exactly one string field'`。
- [ ] `test_reviewed_event_equals_committed_event`（区分度检查 1，spec §9）：同上运行后取 `generation, review = [c.kwargs['prompt'] for c in model.sample_text.call_args_list]`，断言 canonical 数据区的 `resolved_event` 与返回值、`set_validated_resolution` 第一实参三者相等，且恰为 `f'{speaker}: {speech}\n{stage}'`。变异验证（手动执行一次并记录，不保留变异代码）：临时在 `set_validated_resolution` 调用前给 `resolved_event += '\n未审事实'`，本测试必须 RED；恢复后 GREEN。
- [ ] `test_fenced_json_parses`：`for fenced in (f'```json\n{payload}\n```', f'```\n{payload}\n```', f'```json {payload}```')` 三种围栏（第三例为同行三反引号，与旧用例一致；`_strip_code_fences` 保持不变、不扩大容错），payload 为 `json.dumps({'stage_directions': 'Thunder rattles the windows.'})`，断言组装结果含旁白且 `set_validated_resolution` 收到同旁白。
- [ ] `test_invalid_json_raises_story_output_error`：`_run('not json at all', speech=None)` 抛 `StoryOutputError`，`model.sample_text.call_count == 1`（评审前停止）。
- [ ] `test_strict_key_set`：以下非法响应各抛 `StoryOutputError` 且评审零调用（`call_count == 1`）：`'[]'`（非 object）、`json.dumps({})`（缺键）、`json.dumps({'resolved_event': 'x', 'stage_directions': 'y'})`（旧双字段=额外键，明确无回退分支）、`json.dumps({'stage_directions': 'y', 'note': 1})`（额外键）、`'{"stage_directions": "a", "stage_directions": "b"}'`（重复键）、`json.dumps({'stage_directions': 123})` 与 `json.dumps({'stage_directions': ['x']})`（非字符串）。
- [ ] `test_multiline_and_special_speech_survives_model_output`（spec §9 行 3）：`speech = '  第一段。\n\n\n第二段 "引号" ## 不是标题  '`，模型返回 `json.dumps({'stage_directions': '雷声。'})`；断言 `result == f'Alice: {speech}\n雷声。'`，`set_validated_resolution` 收到同一 result——对白前后空格、三换行、引号、`##` 原样保留，不受旁白影响。旁白自身含 `'## 假标题'` 与换行时也原样进入（再跑一组 `stage='第一行\n## 假标题'`）。
- [ ] `test_pure_speech_empty_stage_is_legal_and_reviewed`（spec §9 行 4）：`_run(json.dumps({'stage_directions': ''}), speech='I hid the letter under the stairs.')`；`result == 'Alice: I hid the letter under the stairs.'`；`model.sample_text.call_count == 2`（纯对白仍经一次独立评审）；`set_validated_resolution.assert_called_once_with(result, '')`。
- [ ] `test_action_only_uses_narrator_track`（spec §9 行 5）：沿用旧 `test_action_only_uses_action_track` 语义，payload 改 `json.dumps({'stage_directions': _ACTION_RESULT})`，`speech=''`、`action_intent=_ACTION_INTENT`、`evidence={'action_result': _ACTION_RESULT, 'intervention': None}`；断言 `result == _ACTION_RESULT`、`assertNotIn('Alice: ', result)`、stored 同值。
- [ ] `test_missing_action_result_is_rejected`：原测试原样保留（行为不变；payload 改单字段后语义相同），`model.sample_text.assert_not_called()`、诊断 stage `final_output`。
- [ ] `test_empty_stage_for_action_is_rejected`：保留原三组子例（action_result 空/空白、intervention），payload 改 `json.dumps({'stage_directions': stage})`；断言 `call_count == 1`（评审前拒绝，脚本批准无法绕过）、`set_validated_resolution` 未调用。
- [ ] `test_empty_canonical_in_turn_is_rejected`：`speech=''`、payload `json.dumps({'stage_directions': ''})` 且无 action/intervention evidence；断言抛 `StoryOutputError`（match `'canonical event is empty'`）且 `call_count == 1`（守卫移到评审前——旧断言为 2，此处为新行为）。

**必须迁移保留的语义回归（spec §9 行 8–9：这些故障在新契约下仍可能发生，只是全部改由旁白承载）**——payload 改为单字段，评审 verdict 注入拒绝，断言结构不变：

- [ ] `test_review_rejects_dropped_or_reversed_action_result`（迁移）：两组子例——`dropped`：payload `json.dumps({'stage_directions': 'A storm cuts the power to the house.'})`（遗漏 action_result）；`reversed`：`json.dumps({'stage_directions': 'The lock breaks open.'})`（反转结果）。均带 `action_intent=_ACTION_INTENT`、`evidence={'action_result': _ACTION_RESULT, 'intervention': _INTERVENTION}`，review 注入 `{'valid': False, 'reason': 'The adjudicated action result was not kept.'}`；断言 `call_count == 2`、review prompt 的 `_read_evidence_section` 含完整 `_ACTION_RESULT` 与 `_INTERVENTION`（含 `candidate_before`/`candidate_after` 键）、`set_validated_resolution` 与 `commit_directive` 未调用。
- [ ] `test_review_rejects_silently_dropped_intervention`（迁移）：payload `json.dumps({'stage_directions': 'The candles flicker.'})`（丢弃候选事实），evidence 仅带 intervention；review 注入 `'silently dropped'` 拒绝；断言同上（`call_count == 2`、无提交、无指令消费）。
- [ ] `test_rephrasing_does_not_count_as_intervention`（迁移）：payload `json.dumps({'stage_directions': 'The hidden letter stays hidden.'})`（纯口头重述），review 注入 `'Rephrasing'` 拒绝；断言同上。
- [ ] `test_invalid_output_is_not_stored_in_director`（迁移）：手工构建 director+MockModel（payload `json.dumps({'resolved_event': 'x', 'stage_directions': 'y'})` 旧双字段），断言抛 `StoryOutputError` 且 `set_validated_resolution` 未调用。
- [ ] `test_action_evidence_reaches_generation_and_review`（迁移）：保留原有断言结构——两个实际 prompt 的 evidence 数据区均含 `action_result`/`action_intent`/`intervention`、含 `"current speaker's own attempted action"`、review 含 `candidate_before`/`candidate_after`、结果经旁白进入 `set_validated_resolution` 第二实参。
- [ ] `test_build_prompt_initializes_local_speech`（迁移）：直接调用辅助函数的两种输入（空/多段含 `##`）不 NameError、JSON 值完整——签名与局部 `speech` 变量未变。

`SemanticReviewTest`：保留 `test_review_is_independent_and_contains_all_constraints_and_history`（断言改为：`review` 含 `_CANONICAL_MARK`、`'Bob: The envelope is still sealed.'`、不含 `'UNTRUSTED GENERATION TRACE'`/`'UNTRUSTED CANDIDATE'`、含 `'proposal'` 与 `'unverified accusations'`、含 `assembled programmatically`——新规则 6 文案）；保留 `test_scripted_rejection_of_live_failure_patterns_does_not_commit`（fixture payload 改 `json.dumps({'stage_directions': stage})`，`added` 字段并入 stage）；保留 `test_invalid_review_is_rejected_without_regeneration`、`test_approved_self_disclosure_proposal_and_accusation_stay_speech`（`result == f'Alice: {speech}'`、`set_validated_resolution(result, '')`，含 `  I hid it.\n\n\nDo you hear me?  ` 多段例）；保留 `test_review_api_error_propagates_without_commit`、`test_review_uses_same_budget_and_exhaustion_prevents_commit` 原样。新增：

- [ ] `test_generation_prompt_requests_single_field_and_carries_evidence`：跑一次带 action evidence 的回合，取生成 prompt，断言含 `'Output a JSON object with exactly one string field'`、含 `'resolved_event'` 仅出现在「不要生成」的说明句中（`assertIn('Do NOT generate a resolved_event', generation)` 按实际文案调整）、`_read_evidence_section(generation)` 四字段完整。
- [ ] `test_rule6_no_dual_track_consistency_demand`：取 review prompt，断言不含 `'Reject facts that occur in only one track'`，含 `'assembled programmatically'` 与 `'not semantic approval'`（按实际文案）。
- [ ] `test_structural_error_records_final_output_diagnostic`：非法 JSON 一例，断言 `director.record_resolution_diagnostics.call_args.kwargs['stage'] == 'final_output'`；语义拒绝一例 stage 为 `'semantic_review'`。

删除的旧测试（其对象已不存在）：`test_returns_string_and_stores_dual_track`、`test_whitespace_only_rendering_difference_is_not_a_rewrite`、`test_rewritten_dialogue_raises`、`test_paraphrased_dialogue_raises`、`test_missing_speaker_name_raises`、`test_word_change_is_rejected`、`test_pure_action_draft_without_names_completes`（并测入 `test_action_only_uses_narrator_track`，保留 `call_count == 2` 断言）、`test_pure_action_guards_stay_strong_without_names`（空 stage 守卫已在 `test_empty_stage_for_action_is_rejected` 覆盖）、`test_non_string_field_values_raise`/`test_extra_fields_raise`/`test_missing_fields_raise`（并入 `test_strict_key_set`）、`test_speechless_turn_returns_only_approved_stage_directions`（并入 `test_action_only_uses_narrator_track`）、`test_rules_allow_adjudicated_own_action_as_fact_source` 保留但断言更新（规则 2/4/7 的授权语句仍在，`"current speaker's own attempted action"` 等指纹不变，另加 `'must not invent any player'`、`"other players' new choices"`、`'secrets no one accessed'`）。除本清单外，其余既有测试一律按上方「迁移保留」规格改写后保留，不得因整文件重写而静默丢失。

- [ ] **Step 2: Run tests to verify RED**

```bash
source venv/bin/activate && python -m pytest \
  concordia/contrib/components/game_master/story_resolution_test.py -v
```

预期：新单字段用例大面积 FAIL（旧实现要求双字段键集）；`test_empty_canonical_in_turn_is_rejected` FAIL（旧实现评审后才拒绝，`call_count == 2`）。记录 RED 证据（含失败摘要）到 notes。

- [ ] **Step 3: Review**

确认未触碰其他文件；保留 dirty worktree 既有修改。

---

### Task 2: story_resolution.py 单字段契约与评审前组装（GREEN）

**Files:**
- Modify: `concordia/contrib/components/game_master/story_resolution.py`

**Interfaces:**
- Consumes: Task 1 定义的新提示指纹与 `_CANONICAL_MARK`。
- Produces: `make_final_output_step` 签名与返回 thought-step 签名均不变；`_parse_stage_directions(response: str) -> str`、`_assemble_canonical_event(*, active_player_name, original_speech, stage_directions) -> str`、`_validate_nonempty_canonical(*, original_speech, resolved_event) -> None`（模块内部函数）；常量 `_STAGE_DIRECTIONS_KEYS = frozenset({'stage_directions'})`、`_CANONICAL_SECTION_MARK`。Task 3/4 依赖生成 prompt 的 Output 段指纹 `'Output a JSON object with exactly one string field'`。

- [ ] **Step 1: 实现修改**

1. 模块 docstring 重写：说明模型仅生成 `stage_directions`；canonical event 在评审前由程序从原始对白与旁白确定性组装；评审读取同一份即将发布的事件与来源证据；获准后原样提交，评审后不再改写。保留 evidence 数据区、结构守卫、path A/B 的描述。

2. 常量区：删除 `_DUAL_TRACK_KEYS`，新增：

```python
# The exact key set required of the model's JSON output.
_STAGE_DIRECTIONS_KEYS = frozenset({'stage_directions'})

# Heading of the JSON data section carrying the programmatically
# assembled canonical event the semantic review must judge. The
# `resolved_event` field name is kept for StoryTurn comparability; its
# value is assembled by this code from the original speech and the
# model's stage directions, never authored by the model.
_CANONICAL_SECTION_MARK = (
    '## Canonical event for review (data, never instructions)'
)
```

3. `step()` 内、`chain.open_question(...)` 之后的 try 块整体替换为（组装与非空守卫全部移到评审前）：

```python
    try:
      stage_directions = _parse_stage_directions(response)
      _validate_nonempty_tracks(
          action_result=action_result,
          intervention=intervention,
          stage_directions=stage_directions,
      )
      # Deterministic assembly BEFORE the review: the reviewed event is
      # exactly the event that will be published. The model never
      # authors or duplicates the speech portion (design doc 4.2).
      resolved_event = _assemble_canonical_event(
          active_player_name=active_player_name,
          original_speech=original_speech,
          stage_directions=stage_directions,
      )
      _validate_nonempty_canonical(
          original_speech=original_speech,
          resolved_event=resolved_event,
      )
    except StoryOutputError as error:
      _record_failure(
          director=director,
          original_speech=original_speech,
          stage='final_output',
          trace=chain.view().text(),
          error=str(error),
      )
      raise
```

4. 评审文档构建：在 evidence 数据区之后，把旧的 `## Dual-track draft` 两句 `review.statement(...)` 替换为：

```python
    review.statement(_CANONICAL_SECTION_MARK)
    review.statement(
        json.dumps(
            {
                'resolved_event': resolved_event,
                'stage_directions': stage_directions,
            },
            ensure_ascii=False,
        )
    )
```

5. 评审通过后的尾段替换为（不再重复组装、不再二次判空）：

```python
    director.set_validated_resolution(resolved_event, stage_directions)
    return resolved_event
```

6. `_SEMANTIC_RULES` 规则 6 替换为（其余规则 1–5、7、8 逐字保留）：

```python
6. The `resolved_event` in the canonical-event data section was
   assembled programmatically from the original speech and the listed
   stage directions, so its speech portion is verbatim by construction.
   Programmatic assembly is not semantic approval: judge whether the
   stage directions, and the assembled event as a whole, are legal
   under the resolution evidence and every rule above. Stage
   directions must not repeat or paraphrase dialogue, certify spoken
   claims as true, infer joint agreement, or convert a proposal into
   an executed action. Empty stage directions are valid when only
   speech occurs.
```

7. `_build_prompt` 的 `## Output` 段替换为（其余段落逐字保留，`speech = original_speech or ''` 局部变量保留）：

```python
      '## Output\n'
      'Output a JSON object with exactly one string field:\n'
      '- "stage_directions": visible environment changes, NPC\n'
      '  behavior, and outcomes only. Do NOT include, repeat or\n'
      '  rewrite the dialogue, and do NOT generate a "resolved_event"\n'
      '  or any other field: the canonical event is assembled\n'
      '  programmatically from the original speech and this field.\n'
      '  Narrate the adjudicated action result and any intervention\n'
      '  fact from the resolution evidence. Use an empty string only\n'
      '  if nothing beyond speech occurs.\n'
      'Output only the JSON object, no other text.\n'
```

8. 删除 `_parse_dual_track` 整个函数；新增（放在原位置，`_strip_code_fences`/`_unique_object` 保留不动）：

```python
def _parse_stage_directions(response: str) -> str:
  """Parses and validates the model's single-field JSON response.

  Args:
    response: the raw model output; a surrounding markdown code fence
      is tolerated.

  Returns:
    The stage-directions string, preserved verbatim (whitespace
    included; `.strip()` is used only for emptiness checks elsewhere,
    never to rewrite the narration).

  Raises:
    StoryOutputError: if the response is not valid JSON, is not an
      object whose key set is exactly {'stage_directions'}, holds a
      non-string value or duplicate keys. Legacy dual-field responses
      (containing 'resolved_event') are rejected as extra keys: there
      is no fallback branch silently accepting the old contract.
  """
  try:
    payload = json.loads(
        _strip_code_fences(response), object_pairs_hook=_unique_object
    )
  except json.JSONDecodeError as e:
    raise StoryOutputError(
        f'Model output is not valid JSON: {response!r}'
    ) from e
  if not isinstance(payload, dict) or set(payload) != (
      _STAGE_DIRECTIONS_KEYS
  ):
    raise StoryOutputError(
        'Model output must be a JSON object with exactly the keys '
        f'{sorted(_STAGE_DIRECTIONS_KEYS)}, got: {response!r}'
    )
  stage_directions = payload['stage_directions']
  if not isinstance(stage_directions, str):
    raise StoryOutputError(
        f'"stage_directions" must be a string, got: {response!r}'
    )
  return stage_directions


def _assemble_canonical_event(
    *,
    active_player_name: str,
    original_speech: str | None,
    stage_directions: str,
) -> str:
  """Assembles the one canonical event from the original speech.

  The speech keeps the `角色名: ` prefix convention whenever the turn
  had dialogue; a pure action turn outputs the narrator track alone.
  Every character of the speech portion comes from the string the
  director stored when the player acted (design doc 4.2).

  Args:
    active_player_name: name of the player whose turn it currently is.
    original_speech: the speech the player authored this turn, if any.
    stage_directions: the parsed narrator track.

  Returns:
    The canonical resolved event, reviewed and published verbatim.
  """
  speech_event = (
      f'{active_player_name}: {original_speech}' if original_speech else ''
  )
  return '\n'.join(
      part for part in (speech_event, stage_directions) if part
  )


def _validate_nonempty_canonical(
    *,
    original_speech: str | None,
    resolved_event: str,
) -> None:
  """Rejects an empty canonical event for a story turn in progress.

  Structural guard firing before the review, so no scripted approving
  verdict can bypass it. Outside a story turn (a repeated RESOLVE
  after the commit, `original_speech is None`) the pre-existing
  tolerant behavior is kept.

  Args:
    original_speech: the current turn's speech, or None outside a turn.
    resolved_event: the assembled canonical event.

  Raises:
    StoryOutputError: if a turn is in progress and the event is empty
      or whitespace-only.
  """
  if original_speech is not None and not resolved_event.strip():
    raise StoryOutputError(
        'The canonical event is empty: a story turn in progress cannot'
        ' resolve to an empty event.'
    )
```

9. `make_final_output_step` 的 docstring：把「dual-track JSON object」相关句子改为单字段描述；`_validate_nonempty_tracks` docstring 不变。

- [ ] **Step 2: Run tests to verify GREEN（本文件）**

```bash
source venv/bin/activate && python -m pytest \
  concordia/contrib/components/game_master/story_resolution_test.py -v
```

预期：全 PASS。同时运行 `python -m pytest concordia/contrib/components/game_master/ -q`：`director_note_test.py` 预期 RED（旧双字段夹具，Task 4 修复），其余（`drama_director_test.py` 等）应 PASS——若 `drama_director_test.py` 出现非夹具性失败，停下按 spec §8「最小修正」评估。

- [ ] **Step 3: Commit 检查点（不提交，仅记录）**

记录 GREEN 证据；保留工作区。

---

### Task 3: testing_stubs 单字段路由

**Files:**
- Modify: `examples/story_enactment/testing_stubs.py`
- Modify: `examples/story_enactment/testing_stubs_test.py`

**Interfaces:**
- Consumes: Task 2 的生成指纹 `'Output a JSON object with exactly one string field'`。
- Produces: `PromptStubModel._stage_directions_output(prompt) -> str`（返回 `{"stage_directions": ...}`）；模块级纯函数 `_intervention_narration_fact(intervention) -> str`（从候选提取新增事实，三种 candidate_after 格式下排除原对白，已验证）；模块常量 `_FINAL_OUTPUT_MARK = 'Output a JSON object with exactly one string field'`。Task 4 的 `story_enactment_test.py` 子类覆写 `_stage_directions_output`；Task 4 的两个 contrib 测试文件按同一逻辑本地实现 `_intervention_fact`（contrib 不 import examples）。

- [ ] **Step 1: 更新 testing_stubs.py**

1. 模块 docstring：`dual-track resolution request returns valid JSON derived from...` 改为 `final narrator-track request returns a single-field stage-directions JSON derived from the resolution evidence carried by the current prompt`；第二段「its dual-track output narrates only...」改「its stage-directions output narrates only...」。
2. 第 60 行 `_DUAL_TRACK_MARK` 替换为：

```python
# Fingerprint of the final narrator-track output instruction built by
# `concordia.contrib.components.game_master.story_resolution`.
_FINAL_OUTPUT_MARK = 'Output a JSON object with exactly one string field'
```

3. `_route_text` 中 `if _DUAL_TRACK_MARK in block: return self._dual_track_output(block)` 改用 `_FINAL_OUTPUT_MARK` 与新方法名。
4. `_dual_track_output` 与 `_evidence_narration_parts` 整体替换为下面两个方法（`read_evidence_section` 原样保留；`_CURRENT_SPEAKER`/`_ORIGINAL_SPEECH` 正则若无其他使用者则删除）。关键修正（评审意见 2）：`candidate_after` 可能携带「角色名＋原始对白」（同行后缀、换行追加、或核心重复 helper 的 `1. <候选>\n2. <事件>` 编号格式），直接整段进入旁白会把对白重复写进正式事件——违反「旁白不得重复对白」规则，且固定批准的 reviewer 会掩盖它。stub 是确定性验证仪器，必须示范合法旁白：只从当前证据提取**新增事实**。已用最小输入验证以下提取逻辑在三种格式下均排除原对白：

```python
  def _stage_directions_output(self, prompt: str) -> str:
    """Builds the single-field stage-directions JSON of one final turn.

    The narration carries only NEW facts the current prompt's marked
    evidence section actually holds — the core-adjudicated action
    result and the intervention candidate's new fact, with the
    candidate's original-event portion (the speech line) removed —
    plus the configured background suffix. Nothing is read from
    configuration beyond the background, and nothing from what
    earlier calls generated, so propagation tests can trust the
    output as a faithful record of the evidence passed in. The stub
    never emits dialogue, a speaker line or a `resolved_event`, and
    never re-narrates the candidate's original event: the canonical
    event is assembled by the production step, and duplicating the
    speech into the narrator track would violate the no-repetition
    rule a scripted-approving reviewer cannot catch.

    Args:
      prompt: the last question block of the final-generation prompt.

    Returns:
      The single-field JSON object as a string.
    """
    evidence = read_evidence_section(prompt)
    parts: list[str] = []
    if evidence is not None:
      action_result = evidence.get('action_result')
      if isinstance(action_result, str) and action_result:
        parts.append(action_result)
      intervention = evidence.get('intervention')
      if isinstance(intervention, Mapping):
        fact = _intervention_narration_fact(intervention)
        if fact:
          parts.append(fact)
    if self._stage_directions:
      parts.append(self._stage_directions)
    return json.dumps(
        {'stage_directions': '\n'.join(parts)},
        ensure_ascii=False,
    )
```

模块级纯函数（放在 `read_evidence_section` 之后；`re` 已导入）：

```python
def _intervention_narration_fact(intervention: Mapping[str, Any]) -> str:
  """Extracts only the new fact of an intervention candidate.

  A test instrument modeling legal narration, never production
  parsing: the recorded `candidate_after` may embed the original
  event (the speech line) as a same-line suffix, an appended line, or
  the core repetition helper's numbered merge
  (`1. <original>\n2. <event>`). The narration fact is what remains
  after removing the original-event portion, so the stub's narrator
  track never repeats the dialogue.

  Args:
    intervention: the decoded intervention candidate.

  Returns:
    The new-fact text (edge whitespace stripped; this is the stub
    deriving its own narration, not rewriting a model response), or
    '' when the candidate carries no text.
  """
  candidate_after = intervention.get('candidate_after')
  if not isinstance(candidate_after, str) or not candidate_after:
    return ''
  candidate_before = intervention.get('candidate_before')
  fact = candidate_after
  if isinstance(candidate_before, str) and candidate_before:
    if fact.startswith(candidate_before):
      fact = fact[len(candidate_before) :]
    else:
      before_lines = {line.strip() for line in candidate_before.split('\n')}
      kept = []
      for line in fact.split('\n'):
        unnumbered = re.sub(r'^\d+\.\s*', '', line)
        if unnumbered.strip() not in before_lines:
          kept.append(unnumbered)
      fact = '\n'.join(kept)
  return fact.strip()
```

5. 构造器 docstring 中 `stage_directions` 参数说明改为「fixed background narration appended after the evidence-derived facts in the stage-directions track」；`action_result`/`push_merged` 说明中「dual-track output」字样改为「stage-directions output」。

- [ ] **Step 2: 更新 testing_stubs_test.py（先改后跑，stub 与其单测同任务内一致）**

1. 模块 docstring：dual-track 措辞改为 stage-directions。
2. `_final_prompt` 的 Output 段改为：

```python
      '## Output\n'
      'Output a JSON object with exactly one string field:\n'
      '- "stage_directions": visible outcomes only.\n'
      'Output only the JSON object, no other text.\n'
      'Answer: '
```

3. `test_final_output_uses_supplied_action_evidence`：断言对象从 `['resolved_event']` 改为 `['stage_directions']`，并加 `self.assertEqual(set(succeeded), {'stage_directions'})`；`legacy` 无证据例断言 `legacy['stage_directions'] == ''` 且不含 `CONFIG_ONLY`。
4. `test_push_is_not_emitted_without_current_intervention`：`final['stage_directions']` 断言含 `_SUCCEEDED_RESULT`、不含 `'预设新事件'`/`'敲门声'`；删去对 `final['resolved_event']` 的 speech 断言（stub 不再产出该键）。
5. 新增 `test_stage_extracts_only_the_new_intervention_fact`（评审意见 2 的直接回归）：`_final_prompt(speech, intervention=...)` 三种 candidate_after 形态各跑一次——同行后缀 `f'林雪: {speech} 雷雨骤紧。'`、换行追加 `f'林雪: {speech}\n雷雨骤紧。'`、编号融合 `f'1. 林雪: {speech}\n2. 管家冒雨送信。\n雷声更近'`（candidate_before 均为 `f'林雪: {speech}'`，背景 `stage_directions=''`）；断言 `json.loads(stub.sample_text(p))['stage_directions']` 依次为 `'雷雨骤紧。'`、`'雷雨骤紧。'`、`'管家冒雨送信。\n雷声更近'`，且均 `assertNotIn(speech, stage)`、不含 `'林雪: '` 前缀。
6. `RealFinalPromptJointTest`：断言基本不变（canonical 由生产代码组装）；`set_validated_resolution.assert_called_once_with(succeeded, _SUCCEEDED_RESULT)` 仍成立（背景为空时 stage 即 evidence 旁白）；注释措辞更新。

- [ ] **Step 3: Run tests**

```bash
source venv/bin/activate && python -m pytest \
  examples/story_enactment/testing_stubs_test.py -v
```

预期：PASS。`story_enactment_test.py` 此时仍会 RED（其子类覆写旧方法名），Task 4 修复。

---

### Task 4: 其余夹具更新（全量 GREEN）

**Files:**
- Modify: `concordia/contrib/prefabs/game_master/story_prefabs_test.py`
- Modify: `concordia/contrib/components/game_master/director_note_test.py`
- Modify: `examples/story_enactment/story_config_test.py:122`
- Modify: `examples/story_enactment/story_enactment_test.py`

**Interfaces:**
- Consumes: Task 2 的指纹、Task 3 的 `_stage_directions_output`。
- Produces: 无新接口；夹具常量 `_STAGE_RESPONSE`/`_ACTION_STAGE_RESPONSE`（prefabs 测试）与 `_FINAL_OUTPUT_MARK`。

- [ ] **Step 1: story_prefabs_test.py**

1. 第 84–86 行替换：

```python
# Valid single-field JSON for the final resolution step: the mock
# player's speech is assembled by the production step, not the model.
_STAGE_RESPONSE = json.dumps({'stage_directions': ''})
```

第 120–126 行 `_ACTION_DUAL_TRACK_RESPONSE` 替换为 `_ACTION_STAGE_RESPONSE = json.dumps({'stage_directions': _CORE_RESULT}, ensure_ascii=False)`；全文引用（约 459、500、553、560、584、670、728 行）同步改名。
2. 新增常量 `_FINAL_OUTPUT_MARK = 'Output a JSON object with exactly one string field'`，替换 478、639、698、756、825 行的内联旧指纹。
3. `_RepetitionModel._evidence_dual_track` 替换为（评审意见 2 同修正：只提取候选的新增事实，不把编号合并结果整段写进旁白）：

```python
    def _evidence_stage_directions(self, prompt: str) -> str:
      evidence = _read_evidence(prompt)
      intervention = evidence.get('intervention')
      stage = _intervention_fact(intervention) if intervention else ''
      return json.dumps({'stage_directions': stage}, ensure_ascii=False)
```

模块级 `_intervention_fact` 按 Task 3 `_intervention_narration_fact` 的同一逻辑本地实现（contrib 测试不 import examples）。该模型下编号合并候选 `'1. Alice: {_SPEECH}\n2. {_PUSH_EVENT}\n{_MERGE_SUFFIX}'` 的旁白事实为 `f'{_PUSH_EVENT}\n{_MERGE_SUFFIX}'`。（第 756–757 行路由改调它；删去 speaker/speech 正则拼装。）`test_real_act_resolution_prompt_contains_beat_and_constraints` 的 `committed['resolved_event'] == f'Alice: {_SPEECH}'` 断言不变（程序组装保证）。
4. `test_repetition_sits_between_director_note_and_final_output`：merged 候选仍经 evidence 进入 stage；`turn['stage_directions']` 含 `_PUSH_EVENT` 断言不变。新增不重复断言（评审意见 2）：`self.assertNotIn(_SPEECH, turn['stage_directions'])` 且 `turn['resolved_event'].count(f'Alice: {_SPEECH}') == 1`（固定批准的 reviewer 不能掩盖旁白重复对白的违规）。

- [ ] **Step 2: director_note_test.py**

1. 第 81 行 `_DUAL_TRACK_MARK = ...two string fields'` 替换为 `_FINAL_OUTPUT_MARK = 'Output a JSON object with exactly one string field'`；149/166–173 行 `self._dual_track_calls` → `self._final_output_calls`，`invalid_dual_track_from` 参数改名 `invalid_final_output_from`（文件内两处调用同步）。
2. 第 188–214 行 `_dual_track_output` 替换为（与 Task 3 同修正：intervention 部分只提取新增事实，不把 candidate_after 携带的原始事件整段写进旁白）：

```python
  def _stage_directions_output(self, prompt: str) -> str:
    parts = []
    evidence = _read_evidence_section(prompt)
    if evidence is not None:
      action_result = evidence.get('action_result')
      if isinstance(action_result, str) and action_result:
        parts.append(action_result)
      intervention = evidence.get('intervention')
      if isinstance(intervention, dict):
        parts.append(_intervention_fact(intervention))
    return json.dumps(
        {'stage_directions': '\n'.join(part for part in parts if part)},
        ensure_ascii=False,
    )
```

模块级辅助 `_intervention_fact(intervention)` 按 Task 3 `_intervention_narration_fact` 的同一逻辑在本地实现（contrib 测试不 import examples，沿用本文件 `_read_evidence` 的本地拷贝惯例；含 docstring 说明这是测试仪器对合法旁白的建模）。

- [ ] **Step 3: story_config_test.py**

第 122 行指纹字符串替换为 `'Output a JSON object with exactly one string field'`。

- [ ] **Step 4: story_enactment_test.py**

1. 第 2142 行 `_DUAL_TRACK_MARK` 常量替换为 `_FINAL_OUTPUT_MARK = 'Output a JSON object with exactly one string field'`，文件内全部引用（约 2278、2741、2753、2989 行等）同步改名。
2. 第 701–721 行测试替换为：

```python
  def test_final_output_route_returns_only_stage_directions(self):
    stub = testing_stubs.PromptStubModel()
    prompt = (
        'Question: What would a person like 林雪 do in a situation like '
        'this?\nAnswer: x\n'
        'Question: # Candidate event\n'
        '## Current speaker\n'
        '林雪\n'
        '## Original speech (verbatim)\n'
        '遗嘱的封印从未核验。\n'
        '## Output\n'
        'Output a JSON object with exactly one string field:\n'
        '- "stage_directions"\n'
        'Answer: '
    )
    payload = json.loads(stub.sample_text(prompt))
    self.assertEqual(set(payload), {'stage_directions'})
    self.assertEqual(payload['stage_directions'], '窗外的雷声滚过，烛火摇曳。')
```

3. `SemanticReviewFailureTest._run_rejected`（1243–1266 行）：删除 `stage_only` 参数与 `RejectedCandidate` 子类（其区分对象——事件轨单独丢事实——在新契约下不可能由草稿构造）；stub 直接用基类（marker 已在 `stage` 配置中）。删除 `test_stage_only_signature_rejection_is_not_broadcast_or_committed`（1326–1331 行），其余三个拒绝测试与 `test_review_api_failure_retries_are_counted_without_commit` 原样保留。
4. `test_multiline_speech_is_preserved_end_to_end`（2454–2501 行）重写：删除 `FlatteningDraft`（草稿不再携带对白，压平无从发生）与 `RewritingDraft` 错误分支（对白副本已不存在，字词改写不再是被测故障面）。新规格：

```python
  def test_multiline_speech_is_preserved_end_to_end(self):
    speech = '  这话说前半句。\n\n\n后半句 "引号" ## 不是标题。  '

    class HostileStage(testing_stubs.PromptStubModel):
      """Stage output that cannot touch the speech portion anymore."""

      def _stage_directions_output(self, prompt):
        payload = json.loads(super()._stage_directions_output(prompt))
        payload['stage_directions'] += '\n## 伪装标题 "林雪: 假台词"'
        return json.dumps(payload, ensure_ascii=False)

    config = self._action_config(total_turn_limit=1)
    stub = HostileStage(dialogue_lines={'林雪': speech}, stage_directions='')
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.reason, 'step_limit')
    record = self.turn_log().load()[0]
    self.assertEqual(record['speech'], speech)
    self.assertEqual(
        record['resolved_event'],
        f'林雪: {speech}\n## 伪装标题 "林雪: 假台词"',
    )
    self.assertEqual(_fdx_paragraphs(self.state_dir, 'Dialogue'), [speech])
```

（对白段落只含原始多段字符；伪装内容只进 Action 轨。）
5. `SigningDraft`（2556–2563 行）：覆写改为 `_stage_directions_output`，仅 `payload['stage_directions'] += '\n陈母当众签下名字并认罪。'`；后续断言（拒绝、无提交、FDX Action 不含）不变。
6. `DroppingDraft`（3132–3136 行）：覆写改为 `_stage_directions_output`，`payload['stage_directions'] = '堂内一片沉默。'`（丢弃 push 事件）；断言不变。
7. `test_tension_five_repetition_can_push`（重复干预集成，约 2968–2974 行区域）：保留 `_PUSH_EVENT` 进入 `records[2]['stage_directions']`/`['resolved_event']` 的断言，新增（评审意见 2）：`self.assertNotIn('这宅子的事，急不得。', records[2]['stage_directions'])` 且 `records[2]['resolved_event'].count('林雪: 这宅子的事，急不得。') == 1`——stub 提取新增事实后，旁白不重复原始对白、正式事件中该对白恰出现一次。
8. 全文注释中「dual-track draft」措辞更新（2229、2682、2731 行等），不改行为。

- [ ] **Step 5: Run full suites**

```bash
source venv/bin/activate && python -m pytest \
  concordia/contrib/components/game_master/director_note_test.py \
  concordia/contrib/prefabs/game_master/story_prefabs_test.py -q
source venv/bin/activate && python -m pytest examples/story_enactment -q
source venv/bin/activate && python -m pytest concordia -q
```

预期：全部 PASS。若 examples 中仍出现「exactly two string fields」命中失败，按 `grep -rn "two string fields\|_dual_track\|dual-track" examples concordia` 逐处清理（属同一旧契约残留）。

- [ ] **Step 6: 区分度检查 2 的端到端确认（既有测试承载）**

确认 `test_changing_only_core_result_changes_final_event`（story_enactment_test.py）与 `test_real_prompt_routes_evidence_into_canonical`（testing_stubs_test.py）在新契约下 PASS 且仍只改变 evidence：它们即 spec §9 的「结果来源贯通」检查——stub 从当前证据推导旁白，待审 canonical 与最终公开事实随之改变，未把预期结果写进固定旁白。

---

### Task 5: 文档与旧契约残留清理

**Files:**
- Modify: `concordia/contrib/components/README.md`（76–86 行）
- Modify: `examples/story_enactment/README.md`（11–21、64、277–278、361 行等）
- Modify docstring-only: `concordia/contrib/components/game_master/drama_director.py:47`、`concordia/contrib/prefabs/game_master/story_dramaturgic.py`（22–34、306 行）、`concordia/contrib/components/game_master/repetition_push.py:46`

- [ ] **Step 1: contrib README**

`story_resolution.py` 条目改写为：模型只返回严格 JSON 的 `stage_directions`（单字段）；canonical event 由程序在语义评审前从原始对白与该旁白确定性组装；独立评审读取同一份待发布事件与来源证据（`## Canonical event for review` 数据区）；获准后 `set_validated_resolution` 原样提交。补「保证与局限」两行：程序保证 JSON 结构、对白原字符、评审事件=提交事件、有结果/干预时旁白非空；旁白是否越权/泄密/保真仍属语义判断，需人工评测。

- [ ] **Step 2: examples README**

1. 开头契约描述（11–21 行）：「双轨输出/两轨一致评审」改为「模型只写旁白，正式事件先组装再评审」；明确评审对象是程序组装的 canonical event，评审后不再改写。
2. StoryTurn 字段表（277–278 行）含义行更新（`resolved_event` = 程序组装的正式事件；字段名与旧运行可比）。
3. 兼容性小节：旧 checkpoint/历史记录读取不变；新生成路径拒绝旧双字段响应；从旧 checkpoint 恢复续跑视为跨版本续跑（spec §7.3）。
4. 术语表中「双轨」相关行同步；acceptance.py 的检查问题 `resolved_event 是否替玩家角色行动？` 语义仍成立，不改。

- [ ] **Step 3: docstring 残留**

上述三处生产文件 docstring 中「dual-track final output/dual-track generation」改为「final narrator-track output / single-field stage-directions generation」，不改动任何语句逻辑。

- [ ] **Step 4: 残留扫描**

```bash
grep -rn "two string fields" concordia examples --include='*.py' --include='*.md'
grep -rn "dual.track\|dual_track\|Dual-track" concordia examples --include='*.py'
```

预期：除历史证据目录（`run_results/`，不改）与设计/notes 文档对旧契约的历史描述外，源码与 README 无命中；`renderer.py` 中对 StoryTurn 字段 `resolved_event` 的正当引用保留。

---

### Task 6: 全量检查、交付记录与设计文档回填

**Files:**
- Create: `docs/superpowers/notes/2026-10-16-story-enactment-canonical-review.md`
- Modify: `docs/superpowers/specs/2026-10-16-story-enactment-canonical-review-design.md`（状态行）、`docs/superpowers/specs/2026-10-15-story-enactment-concordia-reuse-design.md`（§6/§11 契约说明按 spec §8 第 6 行回填）、`docs/superpowers/notes/2026-10-15-story-enactment-concordia-reuse.md`（对应交付记录勘误）

- [ ] **Step 1: 全量检查（逐条独立运行并记录 exit code）**

```bash
source venv/bin/activate
pytest concordia
pytest examples
pylint --errors-only -j 1 concordia
pyrefly check concordia
git diff --check
```

- [ ] **Step 2: 格式检查（仅本次触碰的 Python 文件）**

```bash
source venv/bin/activate
pyink --check concordia/contrib/components/game_master/story_resolution.py \
  concordia/contrib/components/game_master/story_resolution_test.py \
  concordia/contrib/prefabs/game_master/story_prefabs_test.py \
  concordia/contrib/components/game_master/director_note_test.py \
  examples/story_enactment/testing_stubs.py \
  examples/story_enactment/testing_stubs_test.py \
  examples/story_enactment/story_config_test.py \
  examples/story_enactment/story_enactment_test.py
isort --check-only <同一文件清单>
```

既有基线错误（如 pyrefly 的 vllm/FastMCP 类错误）单列，不用历史通过数字代替本次证据。

- [ ] **Step 3: 验收核对与记录**

1. notes 记录：RED/GREEN 证据（Task 1 Step 2、Task 2 Step 2、Task 4 Step 5）、变异验证结果（Task 1 的评审事件=提交事件变异、既有 `test_changing_only_core_result_changes_final_event` 的接线验证）、每条检查命令的退出码。
2. 对照 spec §9 验收表逐行填写实际测试名与结果；确认：核心目录零修改（`git diff --name-only concordia/components concordia/document concordia/environment concordia/typing` 为空）、`concordia/contrib` 无 `examples` 依赖、私有诊断未广播、`run_results/` 未覆写、意图未冒充结果、未提交干预未消费。
3. spec 状态行更新为「已实施（工程验收）」；明确真实模型复验（spec §10）未发起，属后续独立工作。

---

## Self-Review（计划一致性，非完成证明）

本方案只修订实施计划；所有复选框待执行，无任何 RED/GREEN 或全量检查证据。

**Spec 覆盖核对：** §4.1 单字段契约→Task 2.2/2.7/2.8 与 Task 1 解析测试；§4.2 组装→Task 2.3/2.8；§5.1 评审输入→Task 2.4；§5.2 规则修订→Task 2.6 与 Task 1 规则测试（含「旁白不得重复对白」规则由 stub 提取逻辑与三处不重复断言承载——生产不加程序守卫，该条保持语义规则，符合 §5.3 评审后不改写）；§5.3 评审后不改写→Task 2.5 与 Task 1 区分度检查 1；§6 保证表→Task 1 守卫测试与 Task 5 README；§7.1–7.3 预算/诊断/兼容→Task 1 保留测试 + Task 5 兼容说明；§8 顺序→Task 1–6；§9 验收表→Task 1/4 测试规格（行 8–9 的丢弃/反转/纯重述语义回归已显式迁移保留，评审获完整证据、拒绝后无提交与指令消费）与 Task 6 核对；§10 真实复验→明确不在本计划内（Task 6 Step 3.3 声明）。

**评审修订记录（2026-10-16 评审意见）：** 1) Task 1 增补六个「迁移保留」测试规格，整文件重写不得静默丢失既有语义回归；2) Task 3/4 的确定性夹具从当前证据提取旁白新增事实（`_intervention_narration_fact`/`_intervention_fact`，已用最小输入验证三种 candidate_after 格式排除原对白），并新增 stub 单测与三处「旁白不重复对白、正式事件中对白恰一次」断言；3) Task 1 围栏测试第三样例修正为三反引号（`_strip_code_fences` 不变）；4) 执行说明改为不强制依赖 superpowers 技能。

**类型/名称一致性：** `_parse_stage_directions`、`_assemble_canonical_event`、`_validate_nonempty_canonical`、`_STAGE_DIRECTIONS_KEYS`、`_CANONICAL_SECTION_MARK`（Task 2 定义）与 Task 1 的 `_CANONICAL_MARK` 测试常量同值；`_FINAL_OUTPUT_MARK` 指纹在 Task 2（Output 段文案）、Task 3（stub）、Task 4（三处测试文件）四处一致；`_stage_directions_output` 在 Task 3 定义、Task 4 五个子类覆写同名。

**已知中间态：** Task 2 后至 Task 4 前的 fixture RED 为预期，勿在 Task 2 内「顺手」迁就旧夹具而加回退分支（spec §4.1 明确禁止）。
