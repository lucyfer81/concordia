# 故事演绎修正实现方案：复用 Concordia 行动裁决与重复干预

> 修订日期：2026-10-15。状态：待实施；按计划审阅 findings 修正。使用当前可用工具逐任务执行，不依赖未安装的 superpowers skill。复选框表示待完成工作，不表示测试已通过。

**Goal:** 按设计文档恢复 Concordia 核心行动裁决与重复干预能力：角色输出「对白 + 自身行动意图」的严格 JSON 契约、复用核心 `attempt_to_result` 裁决自身行动、复用核心 `maybe_inject_narrative_push` 做受约束的重复干预，并保持预算、checkpoint、记录与恢复的既有边界。

**Architecture:** 不修改 Concordia 核心代码。新增三个 contrib 模块（共享 action codec、自身行动裁决 thought-step、重复干预适配 thought-step），扩展 `DramaDirector` 的当前回合解析与干预状态，更新 `story_resolution` 的提示与 canonical 组装、`story_dramaturgic` 的链条装配、示例的配置/采集/stub/run 接线。行动契约（`action_mode`）与反馈开关（张力指令/重复干预）相互独立，均显式归档进 run config 与 StoryTurn 记录。

**Tech Stack:** Python 3.12+、pytest（`MockModel`/`PromptStubModel`，无 API key）、absl-py、既有 `BudgetGuardLanguageModel`/`RetryStatsLanguageModel` 包装链。

**Spec:** [修正设计](../specs/2026-10-15-story-enactment-concordia-reuse-design.md)（执行者须同时阅读 spec 与本方案）。

## Global Constraints

- 不修改 `concordia/components`、`concordia/typing`、`concordia/document`、`concordia/environment`、`concordia/prefabs`（核心）的任何代码；核心文件只读引用。
- 不新增执行引擎、物理系统、通用剧情规划器或证据数据库；不复制 `AccountForAgencyOfOthers`，不重写通用重复检测器，不为核心 helper 写模型代理（spec §1、§2、§7.3）。
- 所有测试使用 `concordia.testing.mock_model.MockModel` 或 `examples.story_enactment.testing_stubs.PromptStubModel`；CI 无 API key。
- 现有模式名保留：`beats_only`、`tension_observe`、`director`、`character_depth`；新增 `action_mode` ∈ {`speech_only`, `speech_and_action`}，默认 `speech_only`，与反馈开关独立（spec §8）。
- 一个已裁决行动仍计一个 story_turn；解析、行动裁决、重复判断、张力评分不增加剧情步数。checkpoint 仍是唯一提交边界：StoryTurn 先写、checkpoint 后提交（spec §8）。
- 预算路径不变：解析/行动裁决/重复干预/最终生成或复核期间耗尽为提交前停止（path A，不提交本步）；完成判断或评分期间耗尽沿用提交后停止（path B）。所有模型调用经共享 BudgetGuard 与既有重试包装器（spec §8）。
- 旧配置兼容：run config schema 1 的旧 checkpoint 按 `speech_only` 读取；旧 StoryTurn 记录缺新字段仍可渲染；恢复保持原动作模式，不得中途切换契约（spec §9）。
- 新运行配置 schema 版本为 `2`，StoryTurn 记录 schema 版本为 `2`（spec §9）。
- 干预原因记为 `repetition` 与 `tension`；诊断仅入归档记录（`director_diagnostics`），绝不广播（spec §7.2）。
- 一轮至多成功融入一路外部干预，两条路径共用执行轮次与 3 步冷却；核心 helper 原样返回只记「已检查」，不记执行、不进冷却（spec §7.2）。
- 实施顺序：先完成 Phase A/B 的无反馈验证，再开启 Phase C；不得同时改变动作模式与导演参数后把质量变化归因于单一修复（spec §10）。
- 开工记录既有 dirty worktree 基线，保留现有 live-fixes 与其他修改；文件重叠时逐段审阅。无需先清理、提交或再次确认才能实施已授权工作。本计划不要求 git commit；需要提交时只纳入已授权且可独立审阅的改动，不能把同一文件中的既有修改一并提交。
- 代码风格：pyink（80 列、2 空格缩进）、isort（Google profile）、Google 风格 docstring、新文件带 Apache 2.0 头（与本目录其他文件一致）。
- 不覆盖 `run_results/` 既有结果；不以真实模型批量请求代替设计审阅（spec §10）。
- 核心 helper 的返回值必须贯穿最终生成、独立复核、公开事件及导出；仅验证 helper 被调用不算验收。测试不得预先把预期行动或干预结果放进固定旁白。
- 候选文本改变只表示 proposed change。最终复核确认行动结果及干预事实保留后才允许提交；无声丢弃候选不得消费指令、增加执行计数或进入冷却。
- 以下 Task 4–14 以具体测试规格替换不完整的测试代码草稿。测试名是实施时须新增的名称；fixture 须按所列真实入口实现，不能将规格文字或省略号复制为可运行测试。

---

## Phase A：契约与动作（spec §10 顺序 A）

### Task 1: contrib 共享 action codec

**Files:**
- Create: `concordia/contrib/components/game_master/story_action.py`
- Test: `concordia/contrib/components/game_master/story_action_test.py`

**Interfaces:**
- Produces: `story_action.ACTION_MODE_SPEECH_ONLY = 'speech_only'`、`story_action.ACTION_MODE_SPEECH_AND_ACTION = 'speech_and_action'`、`story_action.ACTION_MODES`（二元组）、`story_action.validate_action_mode(mode) -> None`（非法抛 `ValueError`）、`story_action.decode_action(raw: str) -> tuple[str, str]`（返回 `(speech, action_intent)`，违规抛 `story_action.ActionDecodeError`）。后续所有任务使用这些名称。

- [ ] **Step 1: Write the failing test**

```python
# concordia/contrib/components/game_master/story_action_test.py
# （文件头为本目录其他测试相同的 Apache 2.0 许可证文本）
"""Tests for the shared speech-and-action codec."""

import json
import unittest

from concordia.contrib.components.game_master import story_action


class DecodeActionTest(unittest.TestCase):

  def test_speech_only_payload_decodes(self):
    raw = json.dumps(
        {'speech': '我现在核对原件上的日期。', 'action_intent': ''}
    )
    speech, intent = story_action.decode_action(raw)
    self.assertEqual(speech, '我现在核对原件上的日期。')
    self.assertEqual(intent, '')

  def test_action_only_payload_decodes(self):
    raw = json.dumps({'speech': '', 'action_intent': '用钥匙尝试开柜。'})
    speech, intent = story_action.decode_action(raw)
    self.assertEqual(speech, '')
    self.assertEqual(intent, '用钥匙尝试开柜。')

  def test_fenced_payload_decodes(self):
    raw = json.dumps({'speech': 'x', 'action_intent': 'y'})
    for fenced in (f'```json\n{raw}\n```', f'```\n{raw}\n```'):
      with self.subTest(fenced=fenced):
        speech, intent = story_action.decode_action(fenced)
        self.assertEqual((speech, intent), ('x', 'y'))

  def test_invalid_json_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action('not json at all')

  def test_non_object_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action('["speech"]')

  def test_missing_field_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action(json.dumps({'speech': 'x'}))

  def test_extra_field_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action(
          json.dumps({'speech': 'x', 'action_intent': '', 'note': 'y'})
      )

  def test_non_string_field_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action(
          json.dumps({'speech': 1, 'action_intent': ''})
      )

  def test_both_empty_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action(
          json.dumps({'speech': '', 'action_intent': ''})
      )

  def test_duplicate_key_raises(self):
    raw = '{"speech": "a", "speech": "b", "action_intent": ""}'
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action(raw)

  def test_inner_whitespace_is_preserved_verbatim(self):
    raw = json.dumps(
        {'speech': '  第一段。\n\n第二段。  ', 'action_intent': ''}
    )
    speech, _ = story_action.decode_action(raw)
    self.assertEqual(speech, '  第一段。\n\n第二段。  ')


class ActionModeTest(unittest.TestCase):

  def test_modes_are_the_two_contracts(self):
    self.assertEqual(
        story_action.ACTION_MODES, ('speech_only', 'speech_and_action')
    )

  def test_validate_action_mode_rejects_unknown(self):
    story_action.validate_action_mode('speech_only')
    story_action.validate_action_mode('speech_and_action')
    with self.assertRaises(ValueError):
      story_action.validate_action_mode('mime')


if __name__ == '__main__':
  unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
source venv/bin/activate && python -m pytest \
  concordia/contrib/components/game_master/story_action_test.py -v
```

预期：FAIL（`ModuleNotFoundError`：`story_action` 不存在）。

- [ ] **Step 3: Write minimal implementation**

```python
# concordia/contrib/components/game_master/story_action.py
# （文件头为本目录其他源文件相同的 Apache 2.0 许可证文本）
"""Shared codec for the story-enactment speech-and-action contract.

Characters in ``speech_and_action`` mode answer a FREE action spec whose
call to action requests a strict JSON object with exactly two string
fields, ``speech`` and ``action_intent`` (design doc section 3). This
module is the single decoder shared by the drama director (current-turn
parsing), the adjudication thought step, and the example's collector and
test stub, so the contract has one definition. Decoding never normalizes
whitespace inside the fields and never guesses actions out of mixed
prose; an invalid input is a fatal, explicit error: the turn is neither
broadcast nor committed, and the model is never re-asked.
"""

import json
import re

ACTION_MODE_SPEECH_ONLY = 'speech_only'
ACTION_MODE_SPEECH_AND_ACTION = 'speech_and_action'
ACTION_MODES = (ACTION_MODE_SPEECH_ONLY, ACTION_MODE_SPEECH_AND_ACTION)

# The exact key set required of the character's JSON output.
ACTION_JSON_KEYS = frozenset({'speech', 'action_intent'})

# Markdown code fences around the JSON object: stripped before decoding,
# mirroring the tolerance of the dual-track output parser.
_FENCE_START = re.compile(r'^```[a-zA-Z0-9]*\s*')
_FENCE_END = re.compile(r'\s?```\s*$')


class ActionDecodeError(Exception):
  """Raised when a character action violates the output contract.

  Only decoding and contract-validation failures raise this error.
  Language-model API errors and budget exhaustion are unrelated and
  never wrapped.
  """


def validate_action_mode(mode: str) -> None:
  """Checks that ``mode`` names a known action mode.

  Args:
    mode: the configured action mode.

  Raises:
    ValueError: if the mode is not one of ``ACTION_MODES``.
  """
  if mode not in ACTION_MODES:
    raise ValueError(
        f'unknown action mode {mode!r}; expected one of {ACTION_MODES}.'
    )


def decode_action(raw: str) -> tuple[str, str]:
  """Strictly decodes one character action into speech and action intent.

  Args:
    raw: the raw action text after the engine's speaker prefix has been
      stripped once; one surrounding markdown code fence is tolerated.

  Returns:
    A ``(speech, action_intent)`` pair of strings. Either field may be
    empty, never both. Field-internal whitespace is preserved verbatim.

  Raises:
    ActionDecodeError: if the text is not valid JSON, is not an object
      whose key set is exactly ``{'speech', 'action_intent'}``, holds
      non-string values or duplicate keys, or has both fields empty.
  """
  try:
    payload = json.loads(
        _strip_code_fences(raw), object_pairs_hook=_unique_object
    )
  except json.JSONDecodeError as e:
    raise ActionDecodeError(f'not valid JSON: {raw!r}') from e
  if not isinstance(payload, dict) or set(payload) != ACTION_JSON_KEYS:
    raise ActionDecodeError(
        'must be a JSON object with exactly the keys '
        f'{sorted(ACTION_JSON_KEYS)}, got: {raw!r}'
    )
  speech = payload['speech']
  action_intent = payload['action_intent']
  if not isinstance(speech, str) or not isinstance(action_intent, str):
    raise ActionDecodeError(f'both fields must be strings, got: {raw!r}')
  if not speech and not action_intent:
    raise ActionDecodeError('speech and action_intent cannot both be empty.')
  return speech, action_intent


def _strip_code_fences(response: str) -> str:
  """Strips one surrounding markdown code fence pair, if present."""
  text = response.strip()
  if not text.startswith('```'):
    return text
  text = _FENCE_START.sub('', text, count=1)
  return _FENCE_END.sub('', text, count=1).strip()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
  """Rejects duplicate JSON keys instead of trusting the last one."""
  result = {}
  for key, value in pairs:
    if key in result:
      raise ActionDecodeError(f'JSON contains duplicate key {key!r}.')
    result[key] = value
  return result
```

- [ ] **Step 4: Run test to verify it passes**

```bash
source venv/bin/activate && python -m pytest \
  concordia/contrib/components/game_master/story_action_test.py -v
```

预期：PASS（全部）。

- [ ] **Step 5: Review**

核对本任务改动与测试结果，保留既有工作区修改；记录 RED/GREEN 证据。提交不作为进入下一任务的前置。

---

### Task 2: DramaDirector 当前回合解析新契约

**Files:**
- Modify: `concordia/contrib/components/game_master/drama_director.py`
- Test: `concordia/contrib/components/game_master/drama_director_test.py`（新增测试类）

**Interfaces:**
- Consumes: Task 1 的 `story_action.decode_action` / `ACTION_MODE_SPEECH_AND_ACTION` / `validate_action_mode`。
- Produces: `DramaDirector.__init__` 新 kwarg `action_mode='speech_only'`；`get_current_original_speech()` 返回解码后对白（可为 `''`）；`get_current_action_intent() -> str | None`；StoryTurn 增加 schema_version=2、raw_action、action_intent。
- 本任务同时建立以下共享暂态接口，Task 7/8/10/11/13 沿用，避免行动裁决结果在链条中丢失：
  - `record_action_result(result: str) -> None`：保存核心裁决实际返回值，不把 intent 当 result。
  - `record_intervention_candidate(*, reason: str, candidate_before: str, candidate_after: str, trace: str) -> None`：保存 tension/repetition 候选，不消费指令或记执行；同轮不得记录两路候选。
  - `record_resolution_diagnostics(*, stage: str, trace: str, error: str | None = None) -> None`：保存当前阶段可见 trace/错误；用于 helper 尚未返回时的部分记录，不改变 checked、executed 或任何提交状态。
  - `get_current_resolution_evidence() -> dict[str, Any]`：深复制返回 `{'action_result': None或字符串, 'intervention': None或{'reason', 'candidate_before', 'candidate_after'}}`，供最终生成/复核显式使用；不在此暴露私有 trace。
  - `get_current_resolution_diagnostics() -> dict[str, Any]`：深复制返回上述依据及可用 trace、阶段、错误信息，供私有归档，绝不广播。
- `turn_context` 是本轮暂态；新 putative_event 清空旧候选与依据，拒绝或中断不写入已提交事实。成功回合的选中依据/trace 通过 director_diagnostics 进入既有快照与提交记录；完整进行中上下文不作为已提交 state。Task 10 补齐已提交计数和冷却，Task 13 保存失败前暂态诊断。

- [ ] **Step 1: Write the failing test**

在 `drama_director_test.py` 末尾（`if __name__` 之前）新增。`_make_director`（约 263 行）已把 `**director_params` 透传给构造器，无需改 fixture：

```python
class ActionContractParsingTest(unittest.TestCase):
  """Current-turn parsing of the speech-and-action contract (spec §3)."""

  def test_speech_and_action_turn_decodes_and_records_raw(self):
    model = _BeatModel(False)
    director, gm = _make_director(model, action_mode='speech_and_action')
    raw = '{"speech": "我现在核对日期。", "action_intent": "拿出原件查看。"}'
    gm.observe(f'[putative_event] Alice: {raw}')
    self.assertEqual(director.get_current_original_speech(), '我现在核对日期。')
    self.assertEqual(director.get_current_action_intent(), '拿出原件查看。')
    result = gm.act(_RESOLVE_SPEC)
    gm.observe(f'[event] {result}')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['raw_action'], raw)
    self.assertEqual(turn['speech'], '我现在核对日期。')
    self.assertEqual(turn['action_intent'], '拿出原件查看。')

  def test_invalid_action_json_is_fatal_before_resolution(self):
    model = _BeatModel(False)
    director, gm = _make_director(model, action_mode='speech_and_action')
    with self.assertRaises(story_action.ActionDecodeError):
      gm.observe('[putative_event] Alice: 我直接说话')
    self.assertIsNone(director.get_last_story_turn())
    self.assertEqual(model.choice_calls, 0)

  def test_speech_only_mode_keeps_raw_speech_and_empty_intent(self):
    model = _BeatModel(False)
    director, gm = _make_director(model)
    result = _complete_turn(gm, '我只有对白。')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['raw_action'], '我只有对白。')
    self.assertEqual(turn['speech'], '我只有对白。')
    self.assertEqual(turn['action_intent'], '')

  def test_unknown_action_mode_rejected_at_construction(self):
    with self.assertRaises(ValueError):
      drama_director.DramaDirector(
          model=_BeatModel(False), beats=BEATS, action_mode='mime'
      )
```

import 区新增 `from concordia.contrib.components.game_master import story_action`。

- [ ] **Step 2: Run test to verify it fails**

```bash
source venv/bin/activate && python -m pytest \
  concordia/contrib/components/game_master/drama_director_test.py::ActionContractParsingTest -v
```

预期：FAIL（`__init__()` 不接受 `action_mode` / 缺 `raw_action` 键）。

- [ ] **Step 3: Write minimal implementation**

`drama_director.py` 修改（其余代码不动）：

1. import 区新增：

```python
from concordia.contrib.components.game_master import story_action
```

2. `__init__` 签名在 `world_constraints: Sequence[str] = (),` 之后新增（docstring Args 补一行）：

```python
      action_mode: str = story_action.ACTION_MODE_SPEECH_ONLY,
```

校验放在 `_validate_beats(beats)` 之后与其他校验并列；存储放在 `self._world_constraints = tuple(world_constraints)` 之后：

```python
    story_action.validate_action_mode(action_mode)
```

```python
    self._action_mode = action_mode
```

3. `pre_observe` 的 putative 分支替换为：

```python
    if observation.startswith(_PUTATIVE_EVENT_PREFIX):
      self._turn_context = None
      actor = self._get_active_player()
      if actor is not None:
        beat = self._current_beat()
        content = observation.removeprefix(_PUTATIVE_EVENT_PREFIX)
        # Strip the engine's speaker prefix once; what remains is the
        # character's raw action (the JSON object in speech_and_action
        # mode). Invalid JSON is a fatal error: no re-ask, no broadcast,
        # no commit of this turn (spec section 3).
        raw_action = content.removeprefix(f'{actor}: ')
        speech, action_intent = self._decode_raw_action(raw_action)
        self._turn_context = {
            'actor': actor,
            'raw_action': raw_action,
            # Decoded speech; '' means the turn has no dialogue.
            'original_speech': speech,
            'action_intent': action_intent,
            'action_result': None,
            'intervention': None,
            'resolution_diagnostics': {},
            'beat_id_at_action': beat.id if beat is not None else None,
            'directive_at_start': copy.deepcopy(self._directive),
        }
```

4. 新增私有方法与访问器（放在 `get_current_original_speech` 旁）：

```python
  def _decode_raw_action(self, raw_action: str) -> tuple[str, str]:
    """Decodes the raw action per the configured action mode.

    Args:
      raw_action: the raw action text after the speaker prefix strip.

    Returns:
      A (speech, action_intent) pair. Speech-only mode treats the raw
      text as pure speech with an empty intent.
    """
    if self._action_mode != story_action.ACTION_MODE_SPEECH_AND_ACTION:
      return raw_action, ''
    return story_action.decode_action(raw_action)

  def get_current_action_intent(self) -> str | None:
    """Returns the action intent of the turn in progress, if any."""
    if self._turn_context is None:
      return None
    return self._turn_context['action_intent']
```

5. `post_observe` 的 `story_turn` 字典在 `'speech': snapshot['original_speech'],` 之后新增字段：

```python
        'schema_version': 2,
        'raw_action': snapshot['raw_action'],
        'action_intent': snapshot['action_intent'],
```

（snapshot 由当前 turn_context 展开，携带新键；不要把完整私有上下文展开进公开事件。）

6. 实现上面列出的 record/get evidence 与 diagnostics API：访问器深复制，写入器要求存在当前回合且 reason 合法。候选 trace 作为私有 diagnostics 保存；本步不改变干预消费逻辑，执行确认在 Task 10 接线。

- [ ] 增加 `ActionContractParsingTest::test_resolution_evidence_is_transient_and_copied`：本轮初值为空；记录真实 action_result 后可读取，修改返回字典不影响内部；新 putative_event 清空暂态，已提交事件不变。
- [ ] 增加 `test_intervention_candidate_does_not_consume_directive` 与 `test_diagnostics_are_not_public_context`：记录候选不前移计数/冷却，trace 不进入玩家观察。正式广播隔离于 Task 9/13 再联测。
- [ ] 解析失败路径不广播、不提交；原始 speech 内的空白不得规范化。空白容错仅属于 Task 8 的模型草稿比对。

- [ ] **Step 4: Run tests to verify they pass**

```bash
source venv/bin/activate && python -m pytest \
  concordia/contrib/components/game_master/drama_director_test.py -v
```

预期：新增类 PASS，原有全部 PASS（speech_only 行为不变）。

- [ ] **Step 5: Review**

核对本任务改动与测试结果，保留既有工作区修改；记录 RED/GREEN 证据。提交不作为进入下一任务的前置。

---
### Task 3: 示例契约提示与故事配置补齐

**Files:**
- Modify: `examples/story_enactment/story_config.py`
- Test: `examples/story_enactment/story_config_test.py`（新增测试）

**Interfaces:**
- Consumes: Task 1 的 `story_action.ACTION_MODES`。
- Produces: `story_config.make_speech_and_action_action_spec() -> entity_lib.ActionSpec`、`story_config.make_scene_action_spec(action_mode: str) -> entity_lib.ActionSpec`；示例故事新增「保险柜需两把钥匙」公开世界事实与「旧遗嘱落款日期」GM 专属事实（Task 9 集成测试依赖这两个事实的措辞）。

- [ ] **Step 1: Write the failing test**

在 `story_config_test.py` 新增：

```python
class ActionSpecModeTest(unittest.TestCase):

  def test_speech_only_spec_requests_dialogue_only(self):
    spec = story_config.make_scene_action_spec('speech_only')
    self.assertIn('现在说什么？只输出自己的对白正文', spec.call_to_action)

  def test_speech_and_action_spec_requests_the_json_contract(self):
    spec = story_config.make_scene_action_spec('speech_and_action')
    for fragment in ('JSON', 'speech', 'action_intent', '两者不能同时为空'):
      self.assertIn(fragment, spec.call_to_action)

  def test_unknown_mode_raises(self):
    with self.assertRaises(ValueError):
      story_config.make_scene_action_spec('mime')


class ExampleFactsTest(unittest.TestCase):
  """Evidence grounding facts required by spec sections 5 and 11."""

  def test_cabinet_needs_two_keys_is_public(self):
    config = story_config.make_example_config()
    self.assertTrue(
        any('两把钥匙' in fact for fact in config.world_facts)
    )

  def test_old_will_date_is_gm_only(self):
    config = story_config.make_example_config()
    self.assertTrue(
        any('二〇一九年三月十二日' in fact for fact in config.gm_only_facts)
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
source venv/bin/activate && python -m pytest \
  examples/story_enactment/story_config_test.py -k 'ActionSpecMode or ExampleFacts' -v
```

预期：FAIL。

- [ ] **Step 3: Write minimal implementation**

`story_config.py`：

1. import 区新增 `from concordia.contrib.components.game_master import story_action`。
2. `_SPEECH_CALL_TO_ACTION` 之后新增提示常量；`make_speech_action_spec` 旁新增两个工厂：

```python
# Speech-and-action prompt (design doc section 3): the character outputs
# a strict JSON object with their own dialogue and their own current
# action attempt. The action intent never prewrites outcomes, object
# contents, other characters' decisions, or already-completed actions.
_SPEECH_AND_ACTION_CALL_TO_ACTION = (
    '根据你已知的信息，{name} 现在说什么、做什么？只输出一个 JSON 对象，'
    '且仅包含两个字符串字段："speech" 为 {name} 现在的对白正文'
    '（无对白时为空字符串），"action_intent" 为 {name} 现在尝试的自身动作'
    '（无动作时为空字符串），两者不能同时为空。'
    '对白不含角色名、引号包裹、他人台词、旁白或推理过程；'
    '行动意图只描述此刻自己尝试的动作，不预写成功结果、物件内容、'
    '他人的决定或过去已完成的动作。'
    '回应最近公开的新信息，保持自己的目标、底线和表达风格；'
    '没有新事实时不要编造证据或声称他人已经同意。只输出该 JSON 对象。'
)


def make_speech_and_action_action_spec() -> entity_lib.ActionSpec:
  """Returns the FREE action spec of the speech-and-action contract.

  Returns:
    An action spec whose call to action requests the strict JSON object
    with exactly the ``speech`` and ``action_intent`` string fields.
  """
  return entity_lib.free_action_spec(
      call_to_action=_SPEECH_AND_ACTION_CALL_TO_ACTION
  )


def make_scene_action_spec(action_mode: str) -> entity_lib.ActionSpec:
  """Returns the scene action spec of the given action mode.

  Args:
    action_mode: one of ``story_action.ACTION_MODES``.

  Returns:
    The dialogue-only spec for ``speech_only`` and the JSON-contract
    spec for ``speech_and_action``.

  Raises:
    ValueError: if the mode is unknown.
  """
  story_action.validate_action_mode(action_mode)
  if action_mode == story_action.ACTION_MODE_SPEECH_AND_ACTION:
    return make_speech_and_action_action_spec()
  return make_speech_action_spec()
```

3. `make_example_config()` 中：

- `world_facts` 追加 `'堂屋侧室的保险柜须两把钥匙同时转动才能开启，林雪与陈母各执一把。'`
- `gm_only_facts` 追加 `'真相：被换上的旧遗嘱全文落款日期为二〇一九年三月十二日，有陈望山签名与手印，无见证人；信封封印完好，从未被当众核验。'`

- [ ] **Step 4: Run tests to verify they pass**

```bash
source venv/bin/activate && python -m pytest \
  examples/story_enactment/story_config_test.py -v
```

预期：PASS（含既有 roundtrip/validation/场景测试）。

- [ ] **Step 5: Review**

核对本任务改动与测试结果，保留既有工作区修改；记录 RED/GREEN 证据。提交不作为进入下一任务的前置。

---

### Task 4: 采集新字段与纯动作渲染

**Files:** Modify/Test `examples/story_enactment/collector.py`、`collector_test.py`、`renderer.py`、`renderer_test.py`。

**接口与实现：**

- 新 StoryTurn 为 schema 2，记录 `raw_action`、原始 `speech`、`action_intent`、获准 `stage_directions`；诊断仍在 `director_diagnostics`，不进入公开事件。
- collector 用 `StepData.action` 对比 `raw_action`，不将动作 JSON 与解码后的 speech 比较。使用 Task 1 的 codec 校验结构及解码值，不维护第二份宽松解析器。
- 兼容 schema 1：缺失 action_intent 视为 `''`，旧纯对白记录的 raw_action 以 speech 为读取缺省值。读取兼容不改写历史文件或已提交事件。
- 空 speech 时，Fountain、文本、FDX 仅渲染 Action，不生成角色名、空 Dialogue 或“沉默片刻”。旁白只取 stage_directions，不能把未执行意图或 JSON 写入剧本。
- 保留 collector 的 StoryTurn 先写、checkpoint 后提交与恢复截尾机制。

**RED → GREEN：**

- [ ] `collector_test.py::ActionRecordTest::test_json_action_is_compared_to_raw_action`：使用已有 `_make_turn`、`_Harness`，分别提供一致和不一致的 StepData.action；后者必须拒绝且 checkpoint 不前进。
- [ ] 同类 `test_decoded_fields_must_match_raw_action`：原始 JSON 中的多段对白与记录必须一致，不能只校验 raw_action 后忽略 speech/intent 被改写。
- [ ] 同类 `test_action_only_turn_commits_once`：空对白、有获准行动结果；断言一条 StoryTurn、一个提交号、相同事件与旁白。
- [ ] `renderer_test.py::ActionOnlyRenderingTest`：覆盖纯动作成功、受阻、多段对白、旧记录缺新字段四组输入。FDX 使用 `xml.etree.ElementTree` 解析 `Paragraph` 的 Type 和 Text，断言 Action 内容与 stage_directions 相同、无 Character/Dialogue；多段对白按 XML 解码后的实际字符比较。
- [ ] 新测试先 RED；最小修改后运行 collector/renderer 两个完整测试文件。不要使用未定义的 `_paragraph_types` 或 `callback(...)`。

```bash
source venv/bin/activate && python -m pytest examples/story_enactment/collector_test.py examples/story_enactment/renderer_test.py -q
```

### Task 5: 能验证结果传播的 PromptStubModel

**Files:** Modify `examples/story_enactment/testing_stubs.py`；Create `examples/story_enactment/testing_stubs_test.py`。

**接口与实现：**

- 保留旧默认行为；增加 `action_intents`、`action_result`、`repetition_answer`、`push_events`、`push_merged`、`scoring_answer` 参数并记录为实例字段，特别是 `self._scoring_answer = scoring_answer`。
- 行动契约提示返回严格 JSON，纯对白提示仍返回原对白；按当前问题路由，不能因早先上下文出现标记误判调用类型。
- `action_result` 只供核心 attempt_to_result 的真实问题使用。双轨生成不能直接读取该配置值，而应读取 Task 8 提示中的结构化 `Resolution evidence`，从实际传入的 action_result/candidate_after 生成旁白。旧固定 stage_directions 仅作背景；传播测试设为 `''`。
- Task 8 使用明确的 JSON 数据节传入 `{speech, action_intent, action_result, intervention}`；stub 从该节用 JSON 解码器读取，不用易截断多段对白的正则猜测。A 阶段先测试解码辅助逻辑，B 阶段接入正式提示。
- 如需将核心返回的因果陈述转换成短旁白，转换仅在测试 stub 内完成；生产代码保留核心原返回值供模型生成与复核，不新增基于 `Because of that` 的生产解析器。
- 干预的融合 stub 必须读取实际当前 candidate 与选择结果。`push_merged` 可作为测试的融合模板/后缀，不能覆盖已有行动结果；最终双轨输出只在本轮 evidence 含干预候选时加入该事实。
- 核心重复 helper 的随机选择在确定性测试中用单个合法建议，或在测试内 patch `random.choice`；不能改生产 helper 或增加模型代理。
- `scoring_answer` 路由到评分问题，`repetition_answer` 路由到最后一个 choice 问题的重复检测；保留 beat completion 路由，不能拿全局 choice_answer 代替所有问题。
- 单元测试可使用 recording subclass；新增状态不得依赖“上一次调用生成了什么”来把并不存在的结果偷偷注入最终输出。

**RED → GREEN：**

- [ ] `PromptStubActionTest::test_json_contract_preserves_multiline_speech`：含换行、引号、`##` 的原始对白精确往返。
- [ ] 同类 `test_final_output_uses_supplied_action_evidence`：两次 final prompt 只改变实际 evidence 中的 ACTION_SUCCEEDED / ACTION_FAILED，保持 stub 配置不变；输出必须不同，且只含各自实际结果。仅改变配置而不改变 evidence，不能凭空产生结果。
- [ ] 同类 `test_push_is_not_emitted_without_current_intervention`：前两轮没有干预 evidence 时不能出现预设新事件。
- [ ] `PromptStubRoutingTest::test_scoring_answer_is_stored_and_used`、`test_last_choice_question_controls_repetition_route`：显式评分 5/9 生效，历史 choice 标记不污染本次路由。
- [ ] 先 RED 后实现，运行本文件与示例回归；Task 8/11 补齐真实提示联测。

```bash
source venv/bin/activate && python -m pytest examples/story_enactment/testing_stubs_test.py -q
```

### Task 6: action_mode、schema 与恢复默认值

**Files:** Modify `examples/story_enactment/run.py`、`story_enactment_test.py`；Test `concordia/contrib/prefabs/game_master/story_prefabs_test.py`（prefab 接线于 Task 9 完成）。

**接口：**

| 入口 | 规则 |
|---|---|
| `make_run_config(..., action_mode='speech_only')` | 新生成配置 schema 2，验证并归档 action_mode |
| `run(..., action_mode: str \| None = None)` 首次运行 | 未提供 run_config 时，None 才缺省为 speech_only；显式模式用于建配置 |
| 首次提供 run_config | 复制后以该配置为准，包括 action_mode；与既有 limits/mode 参数语义一致，不能被函数默认值覆盖 |
| `run(resume=True, action_mode=None)` | 采用 checkpoint 保存的模式 |
| resume 显式提供同模式 | 允许 |
| resume 显式提供不同模式 | 建模前 ValueError；不改存档、不请求模型 |
| schema 1 缺 action_mode | 在配置副本上 setdefault 为 speech_only；首次传入旧配置与真正恢复均适用 |
| CLI `--action_mode` | 用 `_FLAGS['action_mode'].present` 区分未指定与显式指定；未指定传 None |

**实施步骤：**

- [ ] 新增 action_mode flag、API/docstring 与配置验证；scene spec 使用 `make_scene_action_spec`，GM params 接收相同模式。不要只改 CLI 或只改 scene。
- [ ] 对两条配置来源统一做兼容归一化，至少包括 `action_mode` 和旧配置缺失的干预开关默认值；深复制会修改的嵌套字典，不能修改调用方对象。未知 schema 明确拒绝。
- [ ] 不把旧 schema 1 源记录改标为 schema 2，不重写旧 scene/action spec、对白或事实。新产出的 StoryTurn 可用 schema 2，历史 StoryTurn 保留原版；归一化后的有效值另行归档。
- [ ] 保留现有模型身份与累计预算校验，不放宽 `_merge_resume_model_config` 或预算恢复规则。action_mode 的显式冲突判断只在参数非 None 时执行。

**RED → GREEN：**

- [ ] `ActionModeRunTest::test_first_run_defaults_to_speech_only`、`test_first_run_records_json_action_mode`。
- [ ] 同类 `test_supplied_config_is_authoritative_and_not_mutated`：首次传入 schema 1 和 schema 2 配置，分别检查默认、显式 JSON 模式与调用方原对象不变。
- [ ] 同类 `test_resume_omitted_action_mode_keeps_saved_json_mode`、`test_resume_explicit_same_mode_is_allowed`、`test_resume_explicit_different_mode_is_rejected`：先真实提交再恢复，不用新建 run 代替 resume。
- [ ] 同类 `test_schema1_checkpoint_really_resumes_as_speech_only`：构造包含 simulation state、旧 run_config、历史日志的完整 schema 1 checkpoint；实际 resume 后编号连续、原对白与旧已提交记录不变。只从 v2 字典删 action_mode 后跑首次运行不算此测试。
- [ ] 同类 `test_cli_distinguishes_omitted_and_explicit_action_mode`：使用 absl flags 的测试上下文覆盖 flag presence，断言 main 向 run 传 None/实际值。
- [ ] Task 9 完成前可先验证配置/恢复规则；真实 JSON 运行的 GREEN 必须等 prefab 和最终收口接线后补验，不提前宣称 Phase A 完成端到端支持。

```bash
source venv/bin/activate && python -m pytest examples/story_enactment/story_enactment_test.py::ActionModeRunTest -v
```

## Phase B：裁决接线（先关闭两类反馈）

### Task 7: 复用 attempt_to_result，显式保留核心返回值

**Files:** Create `concordia/contrib/components/game_master/story_action_adjudication.py`、`story_action_adjudication_test.py`。

**接口：** `make_action_adjudication_step(director)` 返回既有 `(InteractiveDocument, candidate_event, active_player_name) -> str` thought-step。

**实施步骤：**

- [ ] 无 action_intent：将原始 JSON 前提换成干净的有归属对白，零新增模型调用；不从“我已经录像”推断过去行动。Task 1 保证纯动作不会同时没有 speech 和 intent。
- [ ] 有意图：向既有裁决文档提供原始对白、自身尝试、事实与授权边界，直接调用 `event_resolution.attempt_to_result` 一次。
- [ ] 保存真实返回值 `result`：先 `director.record_action_result(result)`，再返回同一 result 给链条后续步骤。不能保存意图或 stub 配置来冒充核心结果。
- [ ] 提示明确：只裁决当前角色主动提出的自身尝试；依配置与已提交历史判断成功、失败或受阻，不替他人新作选择、不凭空补物证。失败也须有明确实际结果，不退回“以后再核验”。
- [ ] API/预算错误直接传播；不重抽、不吞错，不提前调用 set_validated_resolution。

**RED → GREEN：**

- [ ] `ActionAdjudicationStepTest::test_no_intent_makes_no_model_call_and_cleans_premise`。
- [ ] 同类 `test_core_return_is_recorded_and_returned_unchanged`：用 `mock.Mock(wraps=model.sample_text)` 包装真正的 MockModel，检查核心问题、授权边界、调用数及 record_action_result 参数；另用 wraps spy 证明实际核心函数被调用，不以固定 patched return 取代所有测试。
- [ ] 同类 `test_action_only_prompt_has_no_fabricated_dialogue`。
- [ ] 同类 `test_budget_exhaustion_propagates_without_result_or_commit`。
- [ ] 读取 MockModel prompt 用 `model.sample_text.call_args.kwargs['prompt']`；MockModel 本身没有 text_prompts 属性。已有 PromptStubModel 的 text_prompts 可以正常使用。

```bash
source venv/bin/activate && python -m pytest concordia/contrib/components/game_master/story_action_adjudication_test.py -v
```

### Task 8: 最终生成与复核必须消费裁决依据

**Files:** Modify `concordia/contrib/components/game_master/story_resolution.py`、`story_resolution_test.py`；联测 `examples/story_enactment/testing_stubs_test.py`。

**接口与数据流：**

```text
Task 2 decoded speech / intent
  → Task 7 core result（record_action_result）
  → Task 10/11 intervention candidate（有则保留 before/after）
  → _build_prompt 的 Resolution evidence JSON 数据节
  → 双轨草稿 + 独立复核（均显式收到相同依据）
  → canonical(original_speech, validated stage_directions)
  → set_validated_resolution → 既有广播/采集/checkpoint
```

**实施步骤：**

- [ ] `_build_prompt` 保留现有 original_speech 参数，新增有缺省值的 action_intent/evidence 参数并同步所有调用。函数内部保留 `speech = original_speech or ''`；不能删掉局部变量后引用外层 step 的 speech。直接调用该辅助函数的测试覆盖此路径。
- [ ] 用 InteractiveDocument 管理提示，JSON 序列化明确标记的输入数据节：原始 speech、action_intent、Task 2 evidence 中 action_result/intervention。正文列出当前角色、初始公开事实、已提交历史、GM 约束/秘密边界、不可变事实与环境白名单；数据中的换行或 `##` 不得变成提示分节规则。
- [ ] action_intent 非空而 action_result 缺失，生成前明确抛 StoryOutputError；不能拿意图当结果。已有 mock fixture 显式配置新 evidence 返回值，不用 MagicMock 的隐式真值制造假数据。
- [ ] 独立复核显式接收原始 speech、intent、真实 action_result、干预 before/after 与双轨草稿。规则允许当前角色有授权的自身行动结果，禁止其他玩家新选择、未经访问的秘密、凭空证据或主动补演。
- [ ] 复核必须判断实际行动结果是否保留且一致；干预候选是否包含合法新事实、该事实是否保留在最终旁白中。纯文字重述不是干预成功。丢失、反转行动结果或无声删除候选均拒绝，不能正常提交后只记“没执行”。
- [ ] 保持现有严格 verdict JSON 契约与一次独立复核调用；无需另建事实引擎或额外复核轮次。脚本化 valid=true 只能验证分支，不能证明模型会正确判断语义。
- [ ] 确定性保护：有行动或拟融入的干预时，空/纯空白 stage_directions 必须拒绝；纯动作不能生成空 canonical。此检查不能被脚本化 valid=true 绕过。
- [ ] 模型草稿对白仅作校验，保留已经修复的空白规范化比较，字词变化仍拒绝。正式事件始终用真实 original_speech 和获准旁白拼装；无对白不输出空角色行或伪造台词。
- [ ] 只有生成、结构校验和独立复核全部通过，才能调用 set_validated_resolution。失败保留暂态诊断供归档，执行/冷却/指令消费保持未提交。

**RED → GREEN：**

- [ ] `ActionResolutionTest` 的 `_run` 统一返回 `(result, director, model)`，所有解包一致；不用把 model 误称 director 后断言其方法。
- [ ] `test_build_prompt_initializes_local_speech`：对白为空和多段两种输入，直接验证辅助函数不会 NameError、JSON 值完整。
- [ ] `test_action_evidence_reaches_generation_and_review`：分别检查两个实际 prompt 的结果、授权和来源，不能只检查 intent。
- [ ] `test_missing_action_result_is_rejected`、`test_empty_stage_for_action_is_rejected`：包括 reviewer 固定赞成的负例。
- [ ] `test_review_rejects_dropped_or_reversed_action_result`：录制 reviewer prompt，注入拒绝，验证候选、核心结果同时存在且无 set_validated_resolution。
- [ ] `test_review_rejects_silently_dropped_intervention`、`test_rephrasing_does_not_count_as_intervention`：相同边界，无指令消费或冷却。
- [ ] `test_multiline_whitespace_tolerance_preserves_original_canonical`、`test_word_change_is_rejected`、`test_action_only_uses_action_track`；FDX 字符保真在 Task 9 联测。

```bash
source venv/bin/activate && python -m pytest concordia/contrib/components/game_master/story_resolution_test.py examples/story_enactment/testing_stubs_test.py -q
```

### Task 9: 裁决链接线与无反馈验收

**Files:** Modify `concordia/contrib/prefabs/game_master/story_dramaturgic.py`、`story_prefabs_test.py`；Test `examples/story_enactment/story_enactment_test.py`、`story_isolation_test.py`。

**实施步骤：**

- [ ] prefab 接收并验证 action_mode，传给 director；链条顺序为「自身行动裁决 → director_note → 最终双轨生成与复核」。重复步骤 Task 12 再加。contrib 不得 import examples。
- [ ] prefab 单元测试使用已有 `_build_game_master`、`_build_character` 与模块 alias `drama_director_lib`，检查真实组件/行为；需要观察结果时走 act/observe，不依赖未定义的 `_gm_resolution_steps` 或 `gm=...`。
- [ ] 以下集成全部先用 speech_and_action + beats_only/tension_observe，明确关闭张力指令与重复干预，以隔离自身行动改动。
- [ ] 使用真正的 prefab、引擎、collector 与下一玩家观察。记录实际 core 返回值、两次 final/review prompt、canonical、JSONL、XML 解码后的 FDX；验证同一结果流经全部环节。复用已有 observation 边界编码，不能手工直接给下一玩家喂预期事件冒充广播。

**RED → GREEN / 场景：**

- [ ] `ActionPipelineIntegrationTest::test_success_result_flows_to_observation_and_fdx`：律师实际提出查看已配置原件，核心裁决返回该日期后才能公开；动作发生前各轮不得提前出现日期。
- [ ] 同类 `test_blocked_result_flows_to_observation_and_fdx`：一把钥匙打不开需两把的柜子；旁白与观察明确失败，不生成别人交钥匙。
- [ ] 同类 `test_changing_only_core_result_changes_final_event`：同一配置、输入和背景（stage_directions=''），核心回答分别成功/失败；最终事件和导出必须随之改变。将实现临时变异为“调用核心但丢弃返回值”时，此测试应 RED；记录该验证，不保留变异代码。
- [ ] 同类 `test_action_only_is_visible_without_fabricated_speech`：空对白的成功与失败均进入观察和 Action 段落。
- [ ] 同类 `test_multiline_speech_is_preserved_end_to_end`：让模型草稿把 `\n\n` 压成空格，canonical 与 FDX 解码内容仍保留原始字符；字词变化走错误分支。
- [ ] `ActionBoundaryIntegrationTest`：分别覆盖请求他人签字/认罪（不产生其新选择）、当前角色自己同意（只记录本人）、过去录像/封柜自述（不自动生成物证）、非知情者断言私密债务（不升级为确证秘密）、无已配置文书内容（不编造定案内容）。录制 prompt，必要时注入 reviewer 拒绝以验证边界；不要声称固定赞成的 stub 能验证真实语义。
- [ ] budget path A：在自身行动裁决、最终生成、复核分别耗尽；无新增广播/StoryTurn/checkpoint 前进。纯对白跳过行动调用，有意图只增加该步骤实际的一次 text 调用。
- [ ] budget path B：在行动已获准后的完成判断/评分耗尽；沿现有待提交状态保证本步只提交一次、恢复不丢失。不能只运行旧对白预算测试代替新链路测试。

```bash
source venv/bin/activate && python -m pytest concordia/contrib/prefabs/game_master/story_prefabs_test.py examples/story_enactment/story_enactment_test.py examples/story_enactment/story_isolation_test.py -q
```

**Phase gate：** 上述无反馈数据流与恢复测试通过后再进入 Phase C；记录实际测试输出，不能只以调用数或预设旁白证明行动兑现。

## Phase C：重复干预（与张力指令共享提交边界）

### Task 10: director 候选状态、执行确认与门控

**Files:** Modify/Test `concordia/contrib/components/game_master/drama_director.py`、`drama_director_test.py`、`director_note.py`、`director_note_test.py`。

**接口：**

- `DramaDirector(..., repetition_push_enabled=False, repetition_check_interval=3)`；间隔须为正整数。
- `should_attempt_repetition_push() -> bool`：只读此前已提交状态与本轮已选路径。
- `record_repetition_result(*, candidate_before: str, candidate_after: str, trace: str) -> None`：记录 checked/attempted/proposed_change，**不接受 executed=True**。只有最终复核后的提交路径能确认 executed。
- 使用 Task 2 的 `record_intervention_candidate(reason, candidate_before, candidate_after, trace)`、`get_current_resolution_evidence()`；原因只有 tension/repetition。

**状态与实现：**

- [ ] 暂态 repetition 记录 checked、attempted、proposed_change、executed、reason；checked 计 helper 实际成功返回的检查，attempted 计生成/融合尝试，API 中断另记 error。字符串不等仅决定 proposed_change，不能直接置 executed。
- [ ] 已有 pending 张力指令优先；即使张力生成失败，本轮也不再调用重复 helper。门控依据本轮开始的 directive 快照，避免失败后错误切换路径。
- [ ] 无 pending、反馈与重复开关开启、故事未终止、间隔满足且共享冷却结束时可检查；不要求张力偏离。当前第三轮根据已提交两轮 + 当前轮编号计算，不把 recent_events 长度当 story_turn。
- [ ] `director_note` 生成/融合后记录 tension 候选 before/after/trace，不在此消费或计执行。保持原有“无合法建议则标失败并透传”的明确分支，不能与“候选已生成却在 final 被无声丢弃”混为一谈；后者 Task 8 必须拒绝。
- [ ] 经 Task 8 复核后，set_validated_resolution 将已确认干预信息放入既有 pending_commit；通过既有幂等提交路径一次性保存执行计数、`last_intervention={turn_id, reason}`、共享冷却及原张力指令消费。未提交候选不会改变这些持久化值。
- [ ] 在准备本轮 post_observe/pending_commit 时，用已获准干预的投影状态计算共享冷却与后处理门控，避免 repetition 已执行却又生成应被冷却抑制的 tension 指令。当前回合完成节拍后不得依旧节拍再生成新指令；持久化仍只在既有提交路径完成。
- [ ] helper 原样返回只计检查，不计执行或执行冷却。文本变化但未通过“合法新事件且保留”的复核也不计执行。
- [ ] `get_state/set_state` 保存已提交 repetition_check_count、repetition_execution_count、last_intervention 与原指令状态；旧 state 缺字段缺省 0/0/None，保留 last_directive_executed_turn 作为冷却基线，不能重放已消费张力指令。
- [ ] 无论 rejection、预算还是中断，下一次同轮未提交尝试可重新检查；未提交的检查不形成持久化去重。新 putative_event 清除旧暂态候选与依据，不清除已提交计数。检查不等于执行。
- [ ] StoryTurn 的 repetition/intervention 诊断记真实本轮检查/执行与最近执行轮次；trace 仅私有归档，不广播。

**RED → GREEN：**

- [ ] `RepetitionStateTest::test_third_turn_gate_uses_two_committed_turns`：用 `_make_director` 和 `_complete_turn` 真正提交两轮后再 observe 第三轮；不能只塞两个 recent_events。长序列测试用不会提前完成/超时的 beat 配置。
- [ ] 同类 `test_no_change_only_commits_check`、`test_changed_candidate_is_not_yet_execution`、`test_validated_intervention_commits_once`。
- [ ] 同类 `test_uncommitted_retry_is_eligible`：拒绝后同一未提交轮的新 putative_event 仍允许检查，不能期待 False。
- [ ] 同类 `test_pending_tension_wins_even_if_fusion_fails`、`test_shared_cooldown_and_reason_survive_restore`、`test_old_state_preserves_tension_cooldown`。
- [ ] 同类 `test_accepted_repetition_suppresses_same_turn_directive_generation`、`test_completed_beat_does_not_generate_old_beat_directive`：覆盖上述 post_observe 投影状态与节拍切换。
- [ ] `director_note_test.py` 增加 `test_records_candidate_without_consuming_directive` 和 `test_rejected_final_does_not_consume_tension_candidate`，复用真实最终步骤验证消费边界。
- [ ] 避免 fixture `_ResolutionAct` 与测试手工重复调用 set_validated_resolution；一个模拟回合只有一次实际获准结果。需要自定义 stage 时替换该 fixture 的 acting component，不能先手工验证后让它覆盖。

```bash
source venv/bin/activate && python -m pytest concordia/contrib/components/game_master/drama_director_test.py concordia/contrib/components/game_master/director_note_test.py -q
```

### Task 11: 核心重复 helper 适配与候选追踪

**Files:** Create `concordia/contrib/components/game_master/repetition_push.py`、`repetition_push_test.py`；Modify/Test `examples/story_enactment/testing_stubs.py`、`testing_stubs_test.py`。

**接口：** `make_repetition_push_step(director, model)` 返回三参数 thought-step；model 必须是同一预算/重试包装链的模型。

**实施步骤：**

- [ ] 门控不满足时返回当前 candidate，零模型调用。仅在 RESOLVE 链装配，不能把它接到 NEXT_ACTING 等管理查询。
- [ ] 门控满足时创建独立 InteractiveDocument，显式写入有序已提交公开事件、当前候选、当前行动结果、白名单、不可变事实、玩家列表和 GM 私密边界。近期争执本身不等于重复；新事实、行动结果与有效回应须可用于判断进展。
- [ ] 在该独立文档上原样调用 `event_resolution.maybe_inject_narrative_push(document, candidate, actor)`；不复制检测器、不额外生成另一轮建议、不写模型代理。
- [ ] 将真实返回值交给链条下一步，同时调用 Task 10 record_repetition_result。通过 Task 2 暂态记录 helper 输入/输出及独立文档 trace；不要 `del chain` 后丢失所有证据。
- [ ] helper 可能在内部 document copy 中生成并随机选择建议。本次可归档的是实际可获取的选中/融合结果、before/after 与外层可见 trace；明确不承诺捕获五条不可访问的内部候选，也不为此修改核心或拦截模型。选中事实至少须能从融合结果核查。
- [ ] API/预算异常前的部分 trace 用 try/finally 经 `record_resolution_diagnostics` 留在暂态供 Task 13 保存；异常原样抛出，不调用表示成功返回的 record_repetition_result，不把失败伪装为已检查成功或原样返回。
- [ ] stub 对真实核心 choice、建议、融合问题路由，继续使用最后一个问题块和语义选项映射，不能假设固定字母/索引；融合保留 action_result。

**RED → GREEN：**

- [ ] `RepetitionPushStepTest` fixture 用真实 director 提交两轮后进入第三轮；如果只需单元测试门控，可显式 mock 门控并另外保留上述真实状态测试。
- [ ] `test_ineligible_path_has_zero_calls`、`test_core_helper_is_called_with_constrained_context`（wraps spy，实际 helper 执行）。
- [ ] `test_unchanged_result_records_check_only`：一次实际 choice，无建议/融合，无执行。
- [ ] `test_changed_result_records_candidate_and_trace_only`：重复时实际建议/融合，返回值真实传播，归档可查 before/after，executed 仍 False。
- [ ] `test_action_result_survives_push_merge`：行动结果与新环境事实共存，不能只返回固定 push_merged 后把行动结果覆盖。
- [ ] `test_partial_trace_survives_budget_error`：在 choice、建议、融合分别耗尽，异常与暂态记录符合上述语义，无模型旁路调用。
- [ ] `PromptStubRoutingTest` 补齐重复核心真实提示联测，评分/完成判断不被重复路由截获。

```bash
source venv/bin/activate && python -m pytest concordia/contrib/components/game_master/repetition_push_test.py examples/story_enactment/testing_stubs_test.py -q
```

### Task 12: 干预接线、故障注入与真正恢复

**Files:** Modify/Test `concordia/contrib/prefabs/game_master/story_dramaturgic.py`、`story_prefabs_test.py`、`examples/story_enactment/run.py`、`story_enactment_test.py`。

**实施步骤：**

- [ ] 完整顺序为「自身行动裁决 → director_note → repetition_push → 最终生成/复核」。通过 director 的本轮路径快照保证后两者不会各执行一次。
- [ ] 新配置归档 repetition_push_enabled、repetition_check_interval；beats_only、tension_observe 默认 False，director、character_depth 的新配置可开启。用户显式关闭须生效；旧配置恢复缺字段按 False，不能在续跑时悄悄启用新反馈。
- [ ] 用已有构造/运行 fixture 实现下列测试；每个给出实际输入、目标轮、原始预算、故障触发点与观测断言。避免以条件分支绕过本应出现的 pending 或干预。

**RED → GREEN：**

- [ ] `RepetitionPipelineIntegrationTest::test_tension_five_repetition_can_push`：显式 scoring_answer='5' 且评分已发生，公开历史重复，第三轮间隔满足；必须实际调用核心 helper。获准的新环境事实第一次出现在本轮，进入下一玩家真实观察与 FDX。
- [ ] 同类 `test_fresh_progress_does_not_force_push`：近期有实际新行动/回应，stub 明确返回不重复，记录检查而非执行，不能仅因双方仍有分歧强制干预。
- [ ] 同类 `test_pending_tension_strictly_suppresses_repetition`：scoring_answer='9'，真实评分与生成形成 pending 后先 assert pending 非空；下一轮只执行 tension 路径、核心重复调用为零，共享计数/冷却正确。删掉“若无 pending 则改测 repetition”的 fallback。
- [ ] 同类 `test_dropped_push_is_rejected_without_consumption`：核心确实生成新事件、最终模型故意省略、reviewer 收到完整 evidence 后拒绝；无新增广播或提交，执行计数/冷却不变，失败候选可归档。
- [ ] 同类 `test_rejection_on_turn_three_keeps_two_commits`：review_response 不能从第一轮恒为非法 JSON。用按轮/调用定位的脚本在第三轮复核才返回非法/拒绝；断言前两轮真实提交、第三轮无广播无提交。
- [ ] 同类 `test_same_identity_resume_continues_after_targeted_rejection`：首次与恢复保留同一 model_config（包括 prompt_stub）。用 patch make_language_model 返回固定身份的可控 stub，外部测试脚本仅改变指定故障触发行为；不能靠修改持久化 prompt_stub 配置绕过模型身份检查。固定测试 embedder，CI 不请求 API。
- [ ] 同类 `test_interruption_after_turn_three_resumes_with_original_limit`：首次 total_turn_limit=6，实际第三轮 checkpoint 完成后由 callback 注入一次中断；resume 仍用保存的 6/省略参数，继续到原上限。不能首次上限3、恢复上限6。断言编号连续、无重复日志、不重放第三轮干预。
- [ ] 同类 `test_json_resume_without_action_mode_preserves_contract`：结合 Task 6 的 None 语义跑完整新链条，action JSON 不被当成对白。
- [ ] 同类 `test_budget_paths_cover_repetition_calls`：choice、建议、融合耗尽属于 path A；最终事件后完成判断/评分耗尽属于 path B。检查上一 checkpoint、广播、日志、检查/执行计数和指令状态，不只检查异常类型。
- [ ] 同类 `test_resume_does_not_change_model_identity_or_raise_budget`：负例仍然拒绝不同模型或提高原预算；不能为让正例通过删除既有保护。
- [ ] `story_prefabs_test.py` 覆盖模式/开关矩阵与实际 RESOLVE 路径，使用真实 fixture，无省略号对象。

```bash
source venv/bin/activate && python -m pytest examples/story_enactment/story_enactment_test.py concordia/contrib/prefabs/game_master/story_prefabs_test.py -q
```

## Phase D：归档与验收

### Task 13: 源码、候选与独立进程摘要归档

**Files:** Modify/Test `examples/story_enactment/provenance.py`、`provenance_test.py`、`run.py`、`run_provenance_test.py`；Modify `examples/story_enactment/README.md`、`concordia/contrib/components/README.md`、`concordia/contrib/prefabs/README.md`。

**实施步骤：**

- [ ] provenance 源码快照补齐 story_action.py、story_action_adjudication.py、repetition_push.py；同时确认已修改的 director_note、director、story_resolution、prefab、配置、stub/run 已在清单内。保存实际源码与 sha256，不能只有路径。
- [ ] 首次请求前归档有效配置、源码版本、动作模式、反馈开关/间隔和原始预算；恢复追加 invocation，不覆盖旧配置/源码。旧运行无法复原的原始来源标 history_complete=False，不伪造快照。
- [ ] 为每次 invocation 分配稳定唯一 ID，与 manifest 条目关联。创建独立 `process_summaries/<invocation_id>.json`：结束时原子写入且禁止覆盖；每次恢复产生新的文件，summary.json 保持现有最新正常结果语义。
- [ ] 成功、预算停止、StoryOutputError、API 错误、可捕获的 KeyboardInterrupt/其他中断均保存本进程结果；保存后保持原返回或重抛原异常，不能把失败改成正常完成。捕获范围只用于收尾归档，不吞 BaseException；SIGKILL/机器断电无法保证进程收尾，依赖此前 checkpoint 并在文档说明。
- [ ] 摘要至少包含 invocation ID、开始/结束时间、本进程耗时、终止原因/异常类型和消息、起止已提交 turn_id、预算与重试起止累计值及本进程增量、配置/源码引用。不得把最后进程耗时标成整个累计运行耗时。
- [ ] 收尾读取 Task 2 暂态 diagnostics：失败前已有的 action_result、干预 before/after、可见 trace、复核拒绝原因作为 `uncommitted_resolution` 私有诊断保存。此字段不增加 StoryTurn，不把被拒绝候选放进公开脚本。成功候选随原有 director_diagnostics 进入日志/检查点归档。
- [ ] 归档清单必须包含 checkpoint.json、story_turns.jsonl、run_manifest.json、每次进程摘要、剧本导出；无提交的首次失败明确记录 checkpoint 尚未产生。不得只保存 manifest/剧本而遗漏恢复必需数据。
- [ ] 维持凭据清洗，不归档 API key；trace 可能含 GM 私密信息，明确标记 archive-only。不得将 diagnostics 发给角色、观察队列或 FDX。
- [ ] README 更新模式与恢复默认值、额外按实际尝试计费的模型调用、候选/执行区别、旧配置兼容、错误证据位置与私有归档口径；解释 helper 内部不可见候选限制。

**RED → GREEN：**

- [ ] `provenance_test.py::SourceArchiveTest::test_new_adapters_have_content_and_hashes`。
- [ ] `run_provenance_test.py::ProcessSummaryTest::test_success_and_failure_each_get_immutable_summary`：真实执行一次失败再恢复，旧摘要字节不变、新 ID 独立且可由 manifest 找到。
- [ ] 同类 `test_failed_candidate_and_partial_trace_are_archived_without_commit`：第三轮失败时两轮 checkpoint 保持有效，失败摘要含候选/trace，JSONL/FDX 无该候选。
- [ ] 同类 `test_caught_interruption_preserves_summary_and_rethrows`：回调中断，原异常继续抛出、进程统计正确。
- [ ] 同类 `test_archive_contains_checkpoint_and_process_deltas`：成功与恢复产物齐全，累计/增量/耗时口径分别核对。
- [ ] 同类 `test_credentials_and_private_trace_do_not_leak_to_observations`：archive-only trace 可保存但凭据不保存，角色观察/导出不含 GM 私密标记。

```bash
source venv/bin/activate && python -m pytest examples/story_enactment/provenance_test.py examples/story_enactment/run_provenance_test.py -q
```

### Task 14: 全量检查、覆盖矩阵与真实验收准备

**Files:** 仅必要时修复本方案引入的问题；记录到 `docs/superpowers/notes/2026-10-15-story-enactment-concordia-reuse.md`（实施时创建）。

- [ ] 按仓库要求执行两组完整 pytest、pylint、pyrefly；每项独立运行并保存 exit code。不能用 `&&` 串连整个检查序列，以免 pyrefly 基线失败跳过格式检查。
- [ ] pyink/isort 仅检查本方案实际修改的 Python 文件，逐任务记录清单，避免全仓格式化混入既有改动。下例为单独检查一个任务文件的合法命令，其他文件按实际清单逐一补齐。

```bash
source venv/bin/activate
pytest concordia
pytest examples
pylint --errors-only concordia
pyrefly check concordia
pyink --check concordia/contrib/components/game_master/story_action.py
isort --check-only concordia/contrib/components/game_master/story_action.py
```

- [ ] 每条命令执行后立即记录退出码与输出；如使用脚本汇总，显式收集各项状态且不能启用遇首错退出后漏跑检查。格式修复只针对实际触碰文件。
- [ ] 基线问题按当前环境实测区分：已有 pyrefly 的 vllm/FastMCP 等错误和示例 FDX 测试可能存在的类型问题单列；不得将旧的“1381 passed”当成本次证据，也不能笼统标全部类型检查通过。
- [ ] 检查无核心文件修改、contrib 无 examples 依赖、无私有诊断广播、未覆盖 run_results、无意图冒充结果、无未提交干预消费。对比开工基线，保留其他既有修改。
- [ ] 核对以下矩阵，填写实际测试名称、执行结果与证据路径；所有行在实施前均为待验证。

| 设计要求/场景 | 任务与关键测试 | 当前状态 |
|---|---|---|
| 严格 JSON、重复键、空字段、原对白空白 | Task 1/2/3；Task 5 多段往返 | 待验证 |
| 查看原件成功，实际结果抵达观察与 FDX | Task 7/8/9；test_success_result_flows_to_observation_and_fdx | 待验证 |
| 一把钥匙行动受阻、无他人强制配合 | Task 9；test_blocked_result_flows_to_observation_and_fdx | 待验证 |
| 核心调用结果不可被丢弃，成功/失败有差异 | Task 5/8/9；test_changing_only_core_result_changes_final_event | 待验证 |
| 他人签字/认罪、本人同意、过去录像自述 | Task 9；ActionBoundaryIntegrationTest | 待验证 |
| 私密债务断言不变事实、未知文书内容不编造 | Task 3/8/9；ActionBoundaryIntegrationTest | 待验证 |
| Gemini 草稿压平空白，canonical/FDX 仍精确保真 | Task 8/9；test_multiline_speech_is_preserved_end_to_end | 待验证 |
| 纯动作无伪对白，stage/canonical 不为空 | Task 4/8/9 | 待验证 |
| Luna 连续等待核对、张力5仍可查重复 | Task 11/12；test_tension_five_repetition_can_push | 待验证 |
| 近期新事实/有效回应不强制干预 | Task 11/12；test_fresh_progress_does_not_force_push | 待验证 |
| pending 优先、一轮一路、共享冷却 | Task 10/12；test_pending_tension_strictly_suppresses_repetition | 待验证 |
| 候选改变不等于执行，丢弃候选须拒绝 | Task 8/10/12；test_dropped_push_is_rejected_without_consumption | 待验证 |
| 提交前/后预算路径覆盖新增调用 | Task 7/9/11/12 | 待验证 |
| 真正 schema1 恢复、JSON 模式省略参数恢复 | Task 6/12；ActionModeRunTest | 待验证 |
| 原预算/身份恢复、第三轮故障、无重复提交 | Task 12；三组拒绝/中断/恢复测试 | 待验证 |
| 源码/候选/失败摘要不可覆盖，checkpoint 齐全 | Task 13；ProcessSummaryTest | 待验证 |
| 角色底线/秘密与观测隔离回归 | Task 9/13；story_isolation_test.py | 待验证 |

- [ ] 补两个真实运行缩减场景：Luna 持续“等核对日期”须实际兑现合法查看行动；Gemini “先声称录像、再补防伪构图”须保持自述/证据边界。验证真实上下文与事实流转，不能只检查日志非空。
- [ ] 真实模型质量另行验收：固定故事、模型、源码/提示版本与预算，先 `speech_and_action + tension_observe`，再相同条件开启 director；三场景各三次前固定人工质量阈值与记录字段。不得用确定性 stub 结果宣称真实语义质量已通过，不在本计划文档修订时发起批量调用。

## Self-Review（计划一致性，非完成证明）

本轮只修订实施计划。上表及任务复选框全部待执行；尚无本次实现的 RED/GREEN、全量检查或真实模型验收证据。原先逐条标 ✓ 的覆盖声明撤回，以实际测试报告为准。

| 跨任务接口 | 定义与消费者 |
|---|---|
| decode_action → (speech, action_intent) | Task 1 定义；Task 2/4 复用；Task 3/5 生成一致契约 |
| action_mode=None 的 run 语义 | Task 6 定义；Task 9/12 真正运行与恢复验证；Task 13 归档有效值 |
| record_action_result / get_current_resolution_evidence | Task 2 定义；Task 7 写入；Task 8/11 读取；Task 9 验证贯穿 |
| record_intervention_candidate / record_resolution_diagnostics / 私有 diagnostics | Task 2 定义；Task 10 tension、Task 11 repetition 写入；Task 8 复核、Task 13 保存 |
| record_repetition_result(before, after, trace) | Task 10 定义、Task 11 调用；无 executed 布尔入参，执行只由获准提交确认 |
| make_action_adjudication_step(director) | Task 7 定义；Task 9 装配 |
| make_repetition_push_step(director, model) | Task 11 定义；Task 12 装配，同一个预算包装模型 |
| scoring_answer / actual evidence routing | Task 5 存储/路由；Task 8/11 联测；Task 12 显式指定5/9 |
| schema2 记录与 schema1 兼容 | Task 2/4/6；Task 12 恢复、Task 13 归档 |

**可执行性检查要求：** pytest nodeid 必须是 `path.py::Class` 的同一参数；MockModel prompt 用 spy 读取；director/model fixture 返回值不得混淆；门控 fixture 必须有真实提交轮次；第三轮故障不得从第一轮触发；恢复不得偷换 model_config 或提高预算。任务中的测试名称为明确实施规格，没有不可运行的省略号测试块。

**实施顺序：** Task 1–6 建立契约与配置，Task 7–9 完成无反馈行动链验证，再执行 Task 10–12 干预，最后 Task 13–14 归档与全量验收。归档 API 可提前准备以供故障调试，但不能绕过 Phase B gate。后续按授权执行，不以未安装 skill、清理 dirty worktree 或强制提交作为开工前置。
