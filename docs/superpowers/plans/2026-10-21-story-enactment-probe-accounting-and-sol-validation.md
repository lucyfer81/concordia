# 实验账本修复与 Sol REV 补验实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 S1 离线勘误工具（生成本批 `20261020T075501Z` 的纠正报告）与 S2
Sol-only REV 补验的最小 runner（八个冻结样本、修订夹具、新账本预算规则），
全程零真实模型调用，停在工程审阅点。

**Architecture:** 新增一个 stdlib-only 的共享请求账本模块
（解析/审计/按 started 预扣重建计数 + 写入器），S1 closeout 工具与 S2 REV
probe 都复用它；REV probe 复用既有生产工厂
（`make_final_output_step`、真实 director、严格解析）与
`fixed_model_probe` 的包装器模式，只实现 REV 组的冻结生成槽 + 单次真实评审。

**Tech Stack:** Python 3.12+，stdlib（json/pathlib/hashlib/dataclasses），
absl-py flags，既有 `examples.story_enactment` 模块（`fixed_model_probe`、
`run`、`story_config`、`provenance`）。测试用 unittest + tmp 目录合成夹具，
CI 无 API key、无网络、不依赖 `run_results`。

**Spec:** [docs/superpowers/specs/2026-10-21-story-enactment-probe-accounting-and-sol-validation-design.md](../specs/2026-10-21-story-enactment-probe-accounting-and-sol-validation-design.md)
（本计划从该设计推导；执行者须同时读取设计原文。）

## Global Constraints

- 零真实模型调用；本轮不发起 S2 的八次评审请求，也不在 `run_results` 下
  冻结新批次目录（`freeze` 阶段只做无网络测试验证，真实冻结留给授权轮）。
- 旧批次 `run_results/story_enactment/world_response_fixed_probe/20261020T075501Z/`
  全部只读；不写回、不覆盖、不修改其中任何文件。
- 不修改生产代码、生产提示、核心 helper；不导入旧批次的 `probe.py`；
  closeout 工具不创建真实模型客户端、不读 `.env`/凭据、不执行 run/freeze。
- 不新增进度引擎、事实数据库、任务图、角色规划调用或额外评审模型；
  Gemini 格式兼容（设计的 §7/G）与完整故事（S3）不在本轮范围。
- 不 commit、不 stash、不 clean、不创建 worktree、不回滚既有工作
  （设计 §8、§10；工作区当前有未提交的阶段 A 交付，保持原样）。
- 新源文件带仓库既有 Apache 2.0 许可头；pyink（80 列、2 空格缩进）、
  isort（Google profile）、`pylint --errors-only`、`pyrefly` 全部通过。
- 测试与代码同目录 colocated（`foo.py` + `foo_test.py`），CI 不依赖
  `run_results` 存在；夹具文本可缩减为测试常量。
- 每个任务收尾运行对应测试；最后统一运行 `pytest examples`、
  `pylint --errors-only`、`pyrefly check`。
- 中文文本（夹具、README、勘误说明）使用 UTF-8；所有终端命令为 Bash，
  先 `source venv/bin/activate`。

## File Structure

```text
examples/story_enactment/
├── request_ledger.py                        # 新增：共享请求账本（stdlib only）
├── request_ledger_test.py                   # 新增
├── world_response_probe_closeout.py         # 新增：S1 离线勘误工具
├── world_response_probe_closeout_test.py    # 新增
├── world_response_rev_probe.py              # 新增：S2 Sol-only REV 补验 probe
├── world_response_rev_probe_test.py         # 新增
└── README.md                                # 修改：新增两节说明 + 文件结构表

run_results/story_enactment/world_response_fixed_probe_closeout/<UTC>/   # 新目录（Task 10 生成）
docs/superpowers/notes/2026-10-21-story-enactment-probe-accounting-closeout.md  # 交付记录
```

责任划分：`request_ledger.py` 是唯一计数来源（设计的 §4.1）；
`world_response_probe_closeout.py` 只读解析旧批次并输出派生勘误（§5）；
`world_response_rev_probe.py` 自带夹具常量、slot 守卫与执行入口（§6），
预算与留证全部委托 `request_ledger`。

---

### Task 1: 工作区记录 + 请求账本纯解析、审计与重建

**Files:**
- Create: `examples/story_enactment/request_ledger.py`
- Test: `examples/story_enactment/request_ledger_test.py`
- Create: `docs/superpowers/notes/2026-10-21-story-enactment-probe-accounting-closeout.md`（先只写工作区记录节）

**Interfaces:**
- Produces（后续任务依赖的精确签名）:
  - `LedgerAuditError(Exception)`、`LedgerBudgetError(Exception)`
  - `load_events(path: pathlib.Path) -> list[dict[str, Any]]`
  - `audit_events(events: Sequence[Mapping[str, Any]], *, raw_dir: pathlib.Path | None = None) -> dict[str, list[str]]`
  - `rebuild_counts(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]`

- [ ] **Step 1: 固化工作区状态（实施前，不清理）**

```bash
source venv/bin/activate && git status --porcelain=v1 > /tmp/opencode/ws_before.txt \
  && git log -1 --format='%H %s' >> /tmp/opencode/ws_before.txt && cat /tmp/opencode/ws_before.txt
```

把输出原样粘进
`docs/superpowers/notes/2026-10-21-story-enactment-probe-accounting-closeout.md`
的「工作区记录（实施前）」一节（含日期与 `git log -1` 行）。

- [ ] **Step 2: 写失败测试（解析 + 审计 + 重建 + 本批形状重现）**

`request_ledger_test.py` 关键测试（完整写出，其余同型）：

```python
"""Tests for request_ledger (offline, stdlib only)."""

import json
import pathlib
import unittest

from examples.story_enactment import request_ledger as rl


def _started(slot_id, model_tag='m4', model_name='gpt-6.1-sol'):
  sample_id = slot_id.split('__', 1)[0]
  group = ''.join(c for c in sample_id if c.isalpha())
  return {
      'event': 'started', 'slot_id': slot_id, 'sample_id': sample_id,
      'group': group, 'model_tag': model_tag, 'model_name': model_name,
      'stage': 'real_request', 'request_started_at_utc': '2026-10-20T09:00:00Z',
      'budgets_before': {'global': 0, 'model': 0, 'group': 0, 'sample': 0},
      'budget_caps': {'global': 94, 'model': 47, 'group': 24, 'sample': 4},
      'prompt_sha256': '0' * 64, 'passed_params': {},
  }


def _finished(slot_id, status='completed', model_tag='m4'):
  e = _started(slot_id, model_tag=model_tag)
  return {
      'event': 'finished', 'slot_id': slot_id, 'sample_id': e['sample_id'],
      'group': e['group'], 'model_tag': model_tag, 'status': status,
      'error': None, 'boundary': {}, 'finished_at_utc': '2026-10-20T09:00:01Z',
  }


def _write_ledger(path: pathlib.Path, events):
  path.write_text(
      ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events),
      encoding='utf-8',
  )


class LoadEventsTest(unittest.TestCase):

  def test_truncated_tail_line_is_rejected_with_line_number(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    (path / 'requests.jsonl').write_text(
        json.dumps(_started('A01__m4__s1_x')) + '\n{"event": "star',
        encoding='utf-8',
    )
    with self.assertRaisesRegex(rl.LedgerAuditError, 'line 2'):
      rl.load_events(path / 'requests.jsonl')


class AuditEventsTest(unittest.TestCase):

  def test_duplicate_started_of_same_slot_is_rejected(self): ...
  def test_finished_without_started_is_rejected(self): ...
  def test_second_request_blocked_without_started_is_rejected(self): ...
  def test_same_slot_started_under_two_model_names_is_rejected(self): ...

  def test_orphan_sidecar_started_file_is_reported(self):
    # raw/<slot>.started.json 存在但账本无对应 started 事件 → LedgerAuditError
    ...

  def test_pending_slot_is_reported_not_rejected(self):
    result = rl.audit_events([_started('A01__m4__s1_x')])
    self.assertEqual(result['pending_slots'], ['A01__m4__s1_x'])


class RebuildCountsTest(unittest.TestCase):

  def test_started_without_finish_still_counts_and_finish_never_doubles(self):
    events = [
        _started('A01__m4__s1_x'), _finished('A01__m4__s1_x'),
        _started('A02__m4__s1_x'),
    ]
    counts = rl.rebuild_counts(events)
    self.assertEqual(counts['global'], 2)
    self.assertEqual(counts['by_model']['m4'], 2)
    self.assertEqual(counts['started_slots'], ['A01__m4__s1_x', 'A02__m4__s1_x'])

  def test_frozen_skipped_and_blocked_events_consume_nothing(self):
    events = [
        _started('R01__m4__s2_review'), _finished('R01__m4__s2_review'),
        {'event': 'frozen_stub', 'slot_id': 'R01__m4__s1_generation',
         'status': 'frozen_stub'},
        {'event': 'skipped', 'slot_id': 'R02__m4__s2_review',
         'status': 'skipped_by_upstream_failure'},
        {'event': 'second_request_blocked', 'slot_id': 'P01__m1__s1_judgment'},
        {'event': 'budget_blocked', 'slot_id': 'P02__m1__s1_judgment',
         'level': 'global', 'used': 94, 'cap': 94},
    ]
    self.assertEqual(rl.rebuild_counts(events)['global'], 1)


class BatchShapeReproductionTest(unittest.TestCase):
  """设计的 §8：真实重现本批形状，重建得到 ACT=22、Gemini=39、总数=82。"""

  def test_rebuild_reproduces_the_20261020_batch_counts(self):
    events = _batch_shaped_events()
    counts = rl.rebuild_counts(events)
    self.assertEqual(counts['global'], 82)
    self.assertEqual(counts['by_model'], {'m4': 43, 'm1': 39})
    self.assertEqual(counts['by_group'], {'ACT': 22, 'CHAIN': 36, 'REV': 16, 'REP': 8})
    audit = rl.audit_events(events)
    self.assertEqual(audit['pending_slots'], ['ACT03__m1__s2_self_perception'])
    self.assertEqual(
        [e['slot_id'] for e in events if e['event'] == 'second_request_blocked'],
        ['REP01__m1__s1_judgment', 'REP02__m1__s1_judgment'],
    )
```

批次形状生成器（测试常量，精确复刻本批执行形状：m4 先于 m1、
ACT03__m1 s1 完成后 s2 只有 started、两个 REP m1 判定槽
SecondRequestBlocked、REP "No" 分支 s2/s3 从未启动、REV 生成槽只有
frozen_stub）：

```python
_STAGES = {
    'ACT': ('situation_perception', 'self_perception',
            'person_by_situation', 'action_output'),
    'CHAIN': ('adjudication', 'generation', 'review'),
    'REV': ('generation', 'review'),
    'REP': ('judgment', 'suggestion', 'merge'),
}
_NO_BRANCH_STOPPED = {('REP02', 'm4'), ('REP03', 'm4'), ('REP03', 'm1')}
_SRB_SLOTS = {'REP01__m1__s1_judgment', 'REP02__m1__s1_judgment'}
_MODEL_NAMES = {'m4': 'gpt-6.1-sol', 'm1': 'gemini-3.8-flash-high'}


def _batch_shaped_events():
  events = []
  order = [sid for sid in (
      'ACT01', 'ACT02', 'ACT03', 'CHAIN01', 'CHAIN02', 'CHAIN03',
      'CHAIN04', 'CHAIN05', 'CHAIN06', 'REV01', 'REV02', 'REV03',
      'REV04', 'REV05', 'REV06', 'REV07', 'REV08', 'REP01', 'REP02',
      'REP03')]
  for sample_id in order:
    group = ''.join(c for c in sample_id if c.isalpha())
    for tag in ('m4', 'm1'):
      stages = _STAGES[group]
      for i, stage in enumerate(stages, start=1):
        slot = f'{sample_id}__{tag}__s{i}_{stage}'
        if group == 'REV' and stage == 'generation':
          events.append({'event': 'frozen_stub', 'slot_id': slot,
                         'sample_id': sample_id, 'status': 'frozen_stub'})
          continue
        if (sample_id, tag) in _NO_BRANCH_STOPPED and i > 1:
          continue
        events.append(_started(slot, tag, _MODEL_NAMES[tag]))
        if slot == 'ACT03__m1__s2_self_perception':
          continue
        if slot in _SRB_SLOTS:
          events.append({'event': 'second_request_blocked',
                         'slot_id': slot, 'first_content_sha256': '1' * 64})
          events.append(_finished(slot, status='SecondRequestBlocked',
                                  model_tag=tag))
        else:
          events.append(_finished(slot, model_tag=tag))
  return events
```

（`_tmp_dir` 用 `tempfile.TemporaryDirectory` 上下文helper实现。）

- [ ] **Step 3: 运行测试确认失败**

Run: `pytest examples/story_enactment/request_ledger_test.py -v`
Expected: FAIL（`ModuleNotFoundError: request_ledger`）

- [ ] **Step 4: 实现 `request_ledger.py`（纯函数部分）**

Apache 头 + 模块 docstring（说明：追加式请求账本是唯一计数来源，
started 即预扣，finished 只是观测终态；引用设计 §4）。实现要点：

```python
KNOWN_EVENT_TYPES = frozenset({
    'started', 'finished', 'frozen_stub', 'skipped',
    'second_request_blocked', 'budget_blocked',
})
_REQUIRED_STARTED_FIELDS = ('slot_id', 'sample_id', 'group', 'model_tag',
                            'model_name')


def load_events(path):
  events = []
  with path.open(encoding='utf-8', errors='replace') as handle:
    for number, line in enumerate(handle, start=1):
      if not line.strip():
        continue
      try:
        events.append(json.loads(line))
      except json.JSONDecodeError as error:
        raise LedgerAuditError(
            f'ledger line {number} is truncated or invalid JSON: {error}'
        ) from error
  return events


def audit_events(events, *, raw_dir=None):
  # 返回 {'pending_slots': [...], 'warnings': [...]}；违规抛 LedgerAuditError：
  # - 未知 event 类型
  # - 同 slot 第二条 started（同 slot 唯一）
  # - finished / second_request_blocked 无此前 started
  # - 同 slot started 的 model_name 不一致
  # - raw_dir 给定时：started 事件与 raw/<slot>.started.json 双向交叉检查，
  #   缺文件或孤立文件均报错
  # pending = 有 started 无 finished 的 slot（合法状态，只报告）


def rebuild_counts(events):
  # 只按唯一 started slot 计数：global / by_model / by_group /
  # by_sample（键 'sample_id__model_tag'），并汇总 started_slots /
  # finished_slots / frozen_slots / skipped_slots / second_request_blocks
```

`audit_events` 与 `rebuild_counts` 不得读任何环境变量或凭据；
本文件不 import concordia，仅 stdlib。

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest examples/story_enactment/request_ledger_test.py -v`
Expected: PASS（全部）

- [ ] **Step 6: 快速静态检查并记录触碰文件**

```bash
pylint --errors-only examples/story_enactment/request_ledger.py && \
pyrefly check examples/story_enactment/request_ledger.py
```

---

### Task 2: RequestLedger 写入器（started 预扣 + 留证时机 + 恢复重建）

**Files:**
- Modify: `examples/story_enactment/request_ledger.py`
- Test: `examples/story_enactment/request_ledger_test.py`

**Interfaces:**
- Consumes: Task 1 的 `load_events` / `audit_events` / `rebuild_counts`。
- Produces:
  - `RequestLedger(batch_dir, *, batch_id, global_cap, model_caps: Mapping[str,int], group_caps: Mapping[str,int], sample_caps: Mapping[str,int], secrets: Sequence[str] = ())`
  - `ledger.counts -> dict`（与 `rebuild_counts` 同形快照）
  - `ledger.pending_slots() -> list[str]`
  - `ledger.begin_request(*, slot_id, sample_id, group, model_tag, model_name, attempt_id, stage='real_request', prompt, passed_params) -> None`
  - `ledger.finish_request(*, slot_id, status, error=None, boundary=None) -> None`
  - `ledger.record_raw_response(slot_id, text) -> None`
  - `ledger.record_frozen_slot(*, slot_id, sample_id, model_tag, attempt_id, prompt, frozen_text) -> None`
  - `ledger.record_skipped(*, slot_id, sample_id, reason) -> None`
  - `ledger.record_second_request_block(slot_id, first_content) -> None`

- [ ] **Step 1: 写失败测试**

```python
class RequestLedgerTest(unittest.TestCase):

  def _make(self, path, **caps):
    return rl.RequestLedger(
        path, batch_id='test_batch',
        global_cap=caps.get('global', 94),
        model_caps={'m4': caps.get('model', 47), 'm1': caps.get('model', 47)},
        group_caps={'REV': caps.get('group', 16), 'ACT': 24},
        sample_caps={'REV': 1, 'ACT': 4},
    )

  def test_budget_check_and_persist_happen_before_any_request(self):
    ledger = self._make(batch_dir)
    ledger.begin_request(  # 计数 0→1，prompt 与 started 先落盘
        slot_id='R01__m4__s2_review', sample_id='R01', group='REV',
        model_tag='m4', model_name='gpt-6.1-sol', attempt_id='R01__m4',
        prompt='prompt-text', passed_params={'max_tokens': 1000})
    self.assertTrue((raw / 'R01__m4__s2_review.prompt.txt').exists())
    self.assertTrue((raw / 'R01__m4__s2_review.started.json').exists())
    events = rl.load_events(batch_dir / 'requests.jsonl')
    self.assertEqual(events[0]['event'], 'started')
    self.assertEqual(events[0]['batch_id'], 'test_batch')
    self.assertEqual(events[0]['attempt_id'], 'R01__m4')

  def test_over_cap_refuses_before_writing_anything_but_blocked_event(self):
    ledger = self._make(batch_dir, group=1)
    ledger.begin_request(...slot R01...)
    with self.assertRaises(rl.LedgerBudgetError):
      ledger.begin_request(...slot R02...)  # group cap 1 已满
    events = rl.load_events(batch_dir / 'requests.jsonl')
    self.assertEqual(events[-1]['event'], 'budget_blocked')
    self.assertEqual(events[-1]['level'], 'group')
    self.assertFalse((raw / 'R02__m4__s2_review.started.json').exists())

  def test_started_without_finish_occupies_budget_across_restart(self):
    ledger = self._make(batch_dir)
    ledger.begin_request(...R01...)   # 不 finish（模拟崩溃后重入）
    rebuilt = self._make(batch_dir)
    self.assertEqual(rebuilt.counts['global'], 1)
    self.assertEqual(rebuilt.pending_slots(), ['R01__m4__s2_review'])
    rebuilt.begin_request(...R02...)  # budgets_before 反映 1，而非 report 缓存
    events = rl.load_events(batch_dir / 'requests.jsonl')
    started2 = [e for e in events if e['event'] == 'started'][1]
    self.assertEqual(started2['budgets_before']['global'], 1)

  def test_finish_never_adds_budget(self):
    ledger = self._make(batch_dir)
    ledger.begin_request(...R01...)
    ledger.record_raw_response('R01__m4__s2_review', 'answer')
    ledger.finish_request(slot_id='R01__m4__s2_review', status='completed',
                          boundary={'provider_model': 'gpt-6.1-sol'})
    self.assertEqual(ledger.counts['global'], 1)

  def test_frozen_and_skipped_and_blocked_add_nothing(self): ...

  def test_resume_rebuild_matches_ledger_at_all_levels(self):
    # 三条 started（两条 finished、一条 pending）后重建：
    # global=3, by_model m4=2/m1=1, by_group, by_sample 与账本一致，
    # 不读 report.json，也不接受任何手写恢复值。
    ...

  def test_second_request_block_keeps_first_billed_only_once(self):
    ledger = self._make(batch_dir)
    ledger.begin_request(...R01...)
    ledger.record_second_request_block('R01__m4__s2_review', 'b.')
    ledger.finish_request(slot_id='R01__m4__s2_review',
                          status='SecondRequestBlocked')
    self.assertEqual(ledger.counts['global'], 1)
    self.assertTrue(
        (raw / 'R01__m4__s2_review.second_request_blocked.json').exists())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/request_ledger_test.py -v`
Expected: FAIL（`RequestLedger` 未定义）

- [ ] **Step 3: 实现 `RequestLedger`**

要点（设计 §4.1–§4.2）：

- `__init__`：若 `requests.jsonl` 已存在 → `load_events` + `audit_events`
  （违规直接抛 `LedgerAuditError`，拒绝继续付费请求）→ `rebuild_counts`
  初始化内存计数；否则零计数。不读 report.json，不接受外部传入计数。
  **2026-10-23 审阅修正：**账本不存在也必须对空事件与 `raw/` 做审计；
  存在孤立 started 旁侧文件时拒绝恢复，不能当成新批次从零开始。
  审计逐字段核对 started 旁侧 JSON 与账本，并验证请求正文 SHA-256；
  元数据漂移、缺少或改变 prompt 均失败，不能只核对旁侧文件名。
  旧批次另有三份未保存的请求正文（ACT03__m1 未决槽、REP01/02__m1
  被阻止重试的格式失败槽）。仅 S1 只读勘误可对这些失败/未决槽的
  缺失正文逐项警示，不补造、不退款、不改变旧失败结论；已完成正常槽
  缺正文、任何正文哈希漂移仍失败。RequestLedger 恢复和 S2 汇总不得
  启用该离线历史兼容选项。
- `begin_request`：先按当前计数检查 global/model/group/sample 四层
  （任一 `used + 1 > cap` → 追加 `budget_blocked` 事件并抛
  `LedgerBudgetError`，其他什么都不写）；然后**依次**写
  `raw/<slot>.prompt.txt`、`raw/<slot>.started.json`、追加 `started`
  事件（字段含 `batch_id`、`attempt_id`、旧批 12 字段全集 +
  `budgets_before`/`budget_caps`/`prompt_sha256`/`passed_params`），
  最后内存计数 +1（预扣）。字符串一律过 `redact`（secrets →
  `[REDACTED]`）。
- `finish_request`：计数不变；写 `raw/<slot>.record.json`（status、
  error、boundary、finished_at_utc）+ 追加 `finished`。
- `record_raw_response`：写 `raw/<slot>.response.txt`（SDK 观察边界、
  解析之前调用；非 str 先 `json.dumps`）。
- `record_frozen_slot` / `record_skipped` / `record_second_request_block`：
  与旧批同形记录（frozen_stub / skipped / second_request_blocked 事件 +
  对应 raw 文件），全部不动计数。`second_request_blocked` 的 raw sidecar
  写 `{note, first_content}`（note 文案沿用旧批原文）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/request_ledger_test.py -v`
Expected: PASS

---

### Task 3: S1 closeout 工具 — I/O 纪律与输入清单

**Files:**
- Create: `examples/story_enactment/world_response_probe_closeout.py`
- Test: `examples/story_enactment/world_response_probe_closeout_test.py`

**Interfaces:**
- Consumes: `request_ledger.load_events/audit_events/rebuild_counts`。
- Produces:
  - `input_manifest_payload(batch_dir: pathlib.Path) -> dict`
    （`{'files': [{'path', 'sha256'}]}`，posix 相对路径排序，全量递归）
  - `verify_input_manifest(batch_dir, manifest) -> None`
    （缺失/多余/变更三类差异都列出后抛 `ValueError`）
  - `assert_disjoint_output_dir(source_dir, output_dir) -> None`
    （含 `resolve()` 后的符号链接重叠拒绝、已存在目录拒绝）
  - `main(argv)` / CLI：`--source_batch_dir`（必需）、`--output_dir`（必需）

- [ ] **Step 1: 写失败测试**

```python
class IoDisciplineTest(unittest.TestCase):

  def test_output_inside_source_is_rejected_including_symlink(self):
    # 直接子目录拒绝；symlink 指向源目录内部的 output 同样拒绝
    ...

  def test_existing_output_dir_is_rejected(self): ...

  def test_manifest_covers_all_files_sorted_with_hashes(self):
    manifest = cop.input_manifest_payload(batch_dir)
    paths = [f['path'] for f in manifest['files']]
    self.assertEqual(paths, sorted(paths))
    self.assertIn('requests.jsonl', paths)

  def test_post_work_drift_is_detected(self):
    # 先建 manifest，改动一个源文件后 verify_input_manifest 抛 ValueError
    #（三类差异：missing / extra / changed）
    ...

  def test_missing_required_inputs_fail_explicitly(self):
    # 删掉 fixtures.json → main 报 SystemExit，消息点名缺失文件，
    # 不猜测填零，不输出 corrected_report.json
    ...

  def test_truncated_ledger_fails_without_guessing(self):
    # requests.jsonl 尾行截断 → SystemExit（LedgerAuditError 透传），
    # 输出目录中没有 corrected_report.json
    ...
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/world_response_probe_closeout_test.py -v`
Expected: FAIL

- [ ] **Step 3: 实现骨架**

模块 docstring 说明：只读勘误工具，零真实调用，不导入旧 probe，
不 import concordia / openai / dotenv（仅 stdlib + request_ledger）。
`REQUIRED_INPUTS = ('protocol.json', 'fixtures.json', 'report.json',
'requests.jsonl', 'human_labels.jsonl', 'README.md', 'source_snapshot.json')`。
`main` 流程：参数解析 → 校验必需输入存在 → `assert_disjoint_output_dir`
→ 建输出目录 → 写 `input_manifest.json` →（Task 4 继续）→ 结束后
`verify_input_manifest` 复验输入未漂移。本任务先实现 I/O 部分，
语义部分留 `NotImplementedError` 占位函数（Task 4 填充）——但测试只测
I/O 行为。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/world_response_probe_closeout_test.py -v`
Expected: PASS（I/O 部分）

---

### Task 4: S1 closeout 工具 — 勘误语义与三层状态

**Files:**
- Modify: `examples/story_enactment/world_response_probe_closeout.py`
- Test: `examples/story_enactment/world_response_probe_closeout_test.py`

**Interfaces:**
- Consumes: Task 3 的 I/O 函数；Task 1 的账本解析。
- Produces（输出目录四个文件，均 UTF-8 JSON/MD）:
  - `input_manifest.json`（Task 3）
  - `corrected_report.json`（schema 见 Step 1）
  - `corrections.json`（`{'corrections': [{field, old, corrected, basis, reason}]}`）
  - `README.md`（中文说明：输入批次、口径、三层状态、保留结论）

- [ ] **Step 1: 构建合成迷你批次 helper 并写失败测试**

测试 helper `_make_mini_batch(tmp_path)`：用 Task 1 的
`_batch_shaped_events`（可复制到本测试文件或抽公共常量）写
`requests.jsonl`；写形如旧批的最小 `report.json`（counts 80/37/20、
second_request_blocks=0、attempts 含 ACT03__m1 pending、REP01/02__m1
SRB 失败、REP02__m4/REP03 双模型 No 分支 completed、REV 16 个
review_rejected）、`fixtures.json`（REV expected true×2/false×6、REP
expected）、`human_labels.jsonl`（40 行 annotator_type=agent）、
`protocol.json`（budgets 94/47/…、`execution_order` 40 个 attempt id、
`slots` 每样本每模型的槽位清单且 REV generation 标记 frozen）、
`source_snapshot.json`（最小可解析）、`README.md`（含
`冻结生成槽（REV generation）12 次`）、raw/ 侧文件（含
`REP01__m1__s1_judgment.second_request_blocked.json`、m4 判定
prompt.txt 两行选项 `(a) No` / `(b) Yes`，且 mini 批次中
`REP01__m1__s1_judgment`/`REP02__m1__s1_judgment` 两条 started 事件的
`prompt_sha256` 必须回填为对应 m4 prompt 文本的真实 sha256，供离线意向
分析验证哈希相等）。

关键测试：

```python
class CorrectedCountsTest(unittest.TestCase):

  def test_corrected_counts_come_from_ledger_not_old_report(self):
    report = json.loads((out / 'corrected_report.json').read_text())
    self.assertEqual(report['ledger_counts']['requests_attempted']['total'], 82)
    self.assertEqual(report['ledger_counts']['requests_attempted']['by_model'],
                     {'m4': 43, 'm1': 39})
    self.assertEqual(report['ledger_counts']['requests_attempted']['by_group'],
                     {'ACT': 22, 'CHAIN': 36, 'REV': 16, 'REP': 8})

  def test_responses_no_result_and_format_failures_are_listed_separately(self):
    counts = report['ledger_counts']
    self.assertEqual(counts['responses_observed'], 81)
    self.assertEqual(counts['no_result_requests'],
                     ['ACT03__m1__s2_self_perception'])
    self.assertEqual(counts['format_failures'],
                     ['REP01__m1__s1_judgment', 'REP02__m1__s1_judgment'])
    self.assertEqual(counts['second_request_blocks'], 2)
    self.assertEqual(counts['frozen_generation_slots'], 16)

  def test_corrections_list_old_and_corrected_with_reasons(self):
    corrections = json.loads((out / 'corrections.json').read_text())['corrections']
    fields = {c['field'] for c in corrections}
    self.assertIn('report.counts.real_calls', fields)      # 80 → 82
    self.assertIn('report.counts.by_model.m1', fields)     # 37 → 39
    self.assertIn('report.counts.by_group.ACT', fields)    # 20 → 22
    self.assertIn('report.counts.second_request_blocks', fields)  # 0 → 2
    self.assertIn('readme.frozen_generation_slots', fields)  # 12 → 16


class ThreeLayerStatusTest(unittest.TestCase):

  def test_rev_three_layers_are_separated(self):
    layers = report['attempt_status_layers']
    rev_protocol = layers['protocol_match']['REV']
    self.assertEqual(rev_protocol['matched'], 12)
    self.assertEqual(rev_protocol['unmatched'], 4)
    self.assertEqual(
        {a['sample_id'] for a in rev_protocol['unmatched_attempts']
         if a['fixture_defect']},
        {'REV01', 'REV02'})
    semantic = layers['semantic_annotation']['REV']
    self.assertIn('合法正例接受能力缺少本组证据', semantic['summary'])

  def test_execution_status_classification_rules(self):
    # completed→completed；failure startswith 'SecondRequestBlocked'→format_error；
    # failure_kind review_rejected→review_rejected；
    # attempt 含 pending slot→started_no_result
    ...

  def test_rep_no_branch_slots_are_not_needed_not_skipped(self):
    counts = report['ledger_counts']
    self.assertEqual(len(counts['not_needed_no_branch']), 6)
    self.assertIn('REP02__m4__s2_suggestion', counts['not_needed_no_branch'])
    self.assertEqual(len(counts['skipped_by_upstream_failure']), 6)
    self.assertNotIn('REP02__m4__s2_suggestion',
                     counts['skipped_by_upstream_failure'])


class GeminiOfflineIntentTest(unittest.TestCase):

  def test_offline_letter_analysis_maps_intent_but_keeps_verdict_failed(self):
    analysis = report['gemini_format_offline_analysis']
    by_slot = {a['slot_id']: a for a in analysis}
    rep01 = by_slot['REP01__m1__s1_judgment']
    self.assertEqual(rep01['raw_first_content'], 'b.')
    self.assertEqual(rep01['normalized_letter'], 'b')
    self.assertEqual(rep01['option_meaning'], 'Yes')
    self.assertTrue(rep01['intent_matches_expected'])
    self.assertEqual(rep01['protocol_verdict'], 'failed')
    self.assertIn('仅离线解读', rep01['note'])

  def test_prompt_hash_equality_is_verified_before_mapping(self):
    # m4 prompt sha256 != m1 started prompt_sha256 时 → 该条目标
    # error，不做意向映射
    ...


class HumanLabelAuditTest(unittest.TestCase):

  def test_agent_labels_do_not_count_as_human_confirmation(self):
    audit = report['human_labels_audit']
    self.assertEqual(audit['missing'], [])
    self.assertEqual(audit['duplicates'], [])
    self.assertEqual(audit['unknown_attempt_ids'], [])
    self.assertEqual(audit['annotator_type_counts'], {'agent': 40})
    self.assertEqual(report['human_confirmation'], 'pending')

  def test_missing_duplicate_and_unknown_labels_are_reported(self):
    # 删一行、复制一行、加一个未知 attempt_id → 三个列表非空，仍不抛异常
    ...


class PreservationTest(unittest.TestCase):

  def test_overall_pass_and_original_files_unchanged(self):
    self.assertFalse(report['overall_pass_preserved'])
    # 运行后源目录全部文件哈希与 input_manifest.json 一致；
    # 源 report.json 未被写入（mtime/内容不变）
    ...

  def test_budget_remaining_within_all_caps(self):
    remaining = report['ledger_counts']['budget_check']
    self.assertFalse(remaining['any_cap_exceeded'])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/world_response_probe_closeout_test.py -v`
Expected: FAIL

- [ ] **Step 3: 实现勘误语义**

`corrected_report.json` schema（全部字段由数据推导，常量注解只放
REV01/02 夹具缺陷这一项设计既定标注）：

```python
{
  'source_batch': 'world_response_fixed_probe/<目录名>',
  'provenance': {
      'created_utc', 'tool': 'world_response_probe_closeout',
      'input_manifest_sha256', 'source_report_sha256',
      'new_real_api_calls': 0,
      'note': 'Offline errata only; the source batch is read-only.',
  },
  'ledger_counts': {
      'requests_attempted': {'total', 'by_model', 'by_group'},   # started 口径
      'responses_observed': 81,          # finished 事件数（含 2 次 SRB 首答）
      'no_result_requests': ['ACT03__m1__s2_self_perception'],
      'format_failures': ['REP01__m1__s1_judgment', 'REP02__m1__s1_judgment'],
      'second_request_blocks': 2,
      'frozen_generation_slots': 16,     # frozen_stub 事件数
      'skipped_by_upstream_failure': 6,
      'not_needed_no_branch': 6,         # REP 判定 No 后未执行 s2/s3
      'budget_check': {每层 used/cap/remaining, any_cap_exceeded},
  },
  'attempt_status_layers': {
      'execution_status': {attempt_id: 'completed'|'review_rejected'
                           |'format_error'|'started_no_result'},
      'protocol_match': {'REV': {matched, unmatched, unmatched_attempts,
                                 frozen_labels_unchanged: True}},
      'semantic_annotation': {'REV': {
          'summary': '十二次有效反例拒绝有据，四次正例尝试夹具有缺陷，'
                     '合法正例接受能力缺少本组证据',
          'fixture_defect_samples': ['REV01', 'REV02'],
          'frozen_expected_unchanged': True}},
  },
  'gemini_format_offline_analysis': [
      {'slot_id', 'raw_first_content', 'normalized_letter',
       'option_meaning', 'expected', 'intent_matches_expected',
       'protocol_verdict': 'failed',
       'note': '仅离线解读选项意向；协议判定仍为失败，未计通过。'},
  ],
  'human_labels_audit': {'planned', 'labeled', 'missing', 'duplicates',
                         'unknown_attempt_ids', 'not_labeled',
                         'annotator_type_counts'},
  'human_confirmation': 'pending',
  'overall_pass_preserved': False,
}
```

判定规则（全部确定性，写进代码注释）：

- `execution_status`：attempt `status=='completed'`→`completed`；
  `failure` 以 `'SecondRequestBlocked'` 开头→`format_error`；
  outputs `failure_kind=='review_rejected'`→`review_rejected`；
  attempt 含 pending slot→`started_no_result`。
- `protocol_match`：`expected_valid = fixtures[sample]['expected']=='true'`，
  `actual_valid = execution_status=='completed'`（REV 组）；
  相等→matched。REV01/02 四条 unmatched 附 `fixture_defect: True` +
  设计 §2.2 引注（模块常量 `_FIXTURE_DEFECT_SAMPLES = {'REV01', 'REV02'}`，
  理由文案随常量冻结）。
- `not_needed_no_branch`：planned real slots（protocol.json slots 去掉
  frozen）− started − skipped_by_upstream_failure，且其 attempt 为
  completed 且 judgment=='No'。
- Gemini 离线意向：读 `raw/<REP0X__m1__s1_judgment>.second_request_blocked.json`
  的 `first_content`；`re.fullmatch(r'\s*([a-z])[.)]?\s*', text)` 提取字母；
  用 `raw/<REP0X__m4__s1_judgment>.prompt.txt` 验证 sha256 == m1 started
  的 `prompt_sha256`，再从提示尾部解析 `\(([a-z])\) (.*)` 选项行映射
  字母含义；任一步失败→该条目标 `error` 字段，不做映射。
- `budget_check`：caps 取自 protocol.json `budgets`，used 取重建计数。
- README 勘误：`re.search(r'冻结生成槽（REV generation）(\d+) 次', readme)`
  → old=12, corrected=16（frozen_stub 事件数）；未匹配→warnings 记录。
- 人类标签审计：planned = protocol.json `execution_order`；逐行解析
  labels，缺项/重复/未知 id/非 labeled 状态分别列出；
  `human_confirmation` 仅当全部 annotator_type=='human' 且无缺项。

`main` 收尾：写四个输出文件后 `verify_input_manifest` 复验输入一致，
不一致→SystemExit（输出保留，标记失败）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/world_response_probe_closeout_test.py -v`
Expected: PASS

- [ ] **Step 5: 静态检查**

```bash
pylint --errors-only examples/story_enactment/world_response_probe_closeout.py && \
pyrefly check examples/story_enactment/world_response_probe_closeout.py
```

---

### Task 5: 修订 REV01/REV02 夹具 + REV03–08 原样继承

**Files:**
- Create: `examples/story_enactment/world_response_rev_probe.py`（本任务只写夹具常量部分）
- Test: `examples/story_enactment/world_response_rev_probe_test.py`

**Interfaces:**
- Produces:
  - `RevCase` dataclass（frozen）：`sample_id, title, player, speech,
    action_intent, extra_history, action_result, stage_directions, expected`
  - `CASES: tuple[RevCase, ...]`（REV01–08 顺序）
  - `REVISION_RECORD: dict[str, dict]`（每样本 inherited/revised 逐项差异）
  - `FIXTURE_EVENT_SOURCES: dict[str, str]`（前置来源事件的出处说明）

- [ ] **Step 1: 写失败测试**

```python
class FixturesTest(unittest.TestCase):

  def test_eight_cases_in_order_with_expected_split(self):
    ids = [c.sample_id for c in probe.CASES]
    self.assertEqual(ids, [f'REV0{i}' for i in range(1, 9)])
    self.assertEqual([c.expected for c in probe.CASES],
                     ['true', 'true'] + ['false'] * 6)

  def test_revision_record_lists_field_level_diffs(self):
    rev01 = probe.REVISION_RECORD['REV01']
    self.assertIn('action_result', rev01['revised'])
    self.assertIn('old', rev01['revised']['action_result'])
    self.assertIn('reason', rev01['revised']['action_result'])
    self.assertIn('speech', rev01['inherited'])
    rev02 = probe.REVISION_RECORD['REV02']
    self.assertIn('stage_directions', rev02['revised'])
    for sid in ('REV03', 'REV04', 'REV05', 'REV06', 'REV07', 'REV08'):
      self.assertEqual(probe.REVISION_RECORD[sid]['revised'], {})
      self.assertIn('action_result',
                    probe.REVISION_RECORD[sid]['inherited'])

  def test_rev01_revision_requirements_hold(self):
    case = probe.sample_by_id('REV01')
    self.assertNotIn('没有别的动静', case.action_result)
    for fragment in ('隔门', '邻居', '远去'):
      self.assertIn(fragment, case.action_result)
    for fragment in ('周律师走到门边', '把话说完', '邻居', '远去'):
      self.assertIn(fragment, case.stage_directions)
    self.assertIn('stage_directions',
                  probe.REVISION_RECORD['REV01']['revised'])

  def test_rev02_revision_requirements_hold(self):
    case = probe.sample_by_id('REV02')
    for banned in ('吹干', '折', '收进'):
      self.assertNotIn(banned, case.stage_directions)
    self.assertIn('自称', case.stage_directions)  # 保留归属、不认证

  def test_fixture_sources_are_documented(self):
    self.assertIn('敲门', probe.FIXTURE_EVENT_SOURCES['knock'])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/world_response_rev_probe_test.py -v`
Expected: FAIL

- [ ] **Step 3: 写夹具常量**

REV03–08 六例的 `sample_id/title/player/speech/action_intent/
extra_history/action_result/stage_directions/expected` 九个字段
**逐字节复制**自冻结批次
`run_results/story_enactment/world_response_fixed_probe/20261020T075501Z/probe.py`
的 `_SAMPLES`（REV03 在 865–882 行附近，REV08 至 1028 行附近；以
`sample_id='REV03'`…`'REV08'` 定位）。REV01/02 的继承字段
（title/player/speech/action_intent/extra_history/expected）同样原样复制。
修订字段如下（设计 §6.1）：

```python
# REV01：核心结果补齐「律师实际隔门告知 + 邻居规则内公开回应 + 离开」，
# 删除与旁白矛盾的「门外没有别的动静」；旁白同时保留律师已执行动作。
_REV01_ACTION_RESULT = (
    '周律师走到门边，隔着门向邻居说明今晚家事暂不需要帮助，'
    '请她不必久等。 Because of that, 周律师隔门把话说完；'
    '门外传来邻居的应声："知道了，有需要随时叫我一声"，'
    '随后脚步声在雨里渐渐远去。'
)
_REV01_STAGE_DIRECTIONS = (
    '周律师走到门边，隔着门把话说完。'
    '门外传来邻居的应声："知道了，有需要随时叫我一声"，'
    '随后脚步声在雨里渐渐远去。'
)

# REV02：旁白只保留「记录了谁的什么未核实主张、说话时刻」的已建立
# 表示；删除吹干、折纸、收存等未提出的后续动作；不补造具体钟点、
# 不认证内容。action_result 原样继承。
_REV02_STAGE_DIRECTIONS = (
    '案上的白纸留着方才的一行记录："陈母自称头七前夜始终守在后院灵前"，'
    '旁边注着说话的时刻。'
)
```

`REVISION_RECORD` 每条 revised 字段含 `old`（旧批原文）、`new`、
`reason`（引用设计 §6.1 对应条目）；REV03–08 为
`{'inherited': [九字段], 'revised': {}}`。
`FIXTURE_EVENT_SOURCES` 记录 `_KNOCK`（敲门，来自白名单事件「隔壁邻居被
争执声惊动，前来敲门探问」）与 `_PAPER`/`_CHENMU_CLAIM`（纸笔来源、
陈母主张，均复制自旧批 fixture events）的出处文本。

- [ ] **Step 4: 与旧批做一次本地（非 CI）逐字节核对**

```bash
source venv/bin/activate && python - <<'EOF'
import json, pathlib
from examples.story_enactment import world_response_rev_probe as p
old = json.loads(pathlib.Path(
    'run_results/story_enactment/world_response_fixed_probe/'
    '20261020T075501Z/fixtures.json').read_text())
by_id = {s['sample_id']: s for s in old['samples']}
for case in p.CASES:
    if case.sample_id in ('REV01', 'REV02'):
        continue
    src = by_id[case.sample_id]
    for field in ('title', 'player', 'speech', 'action_intent',
                  'action_result', 'stage_directions', 'expected'):
        assert getattr(case, field) == src[field], (case.sample_id, field)
    assert list(case.extra_history) == src['extra_history'], case.sample_id
for sid, fields in (('REV01', ('speech', 'action_intent')),
                    ('REV02', ('speech', 'action_intent', 'action_result'))):
    case = p.sample_by_id(sid)
    for field in fields:
        assert getattr(case, field) == by_id[sid][field], (sid, field)
print('fixture inheritance verified against the frozen batch')
EOF
```

Expected: `fixture inheritance verified against the frozen batch`

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest examples/story_enactment/world_response_rev_probe_test.py -v`
Expected: PASS

- [ ] **Step 6: 复核点（停止并请用户确认）**

向用户展示 REV01/REV02 修订前后的完整对照（action_result 与
stage_directions 双栏）及 REVISION_RECORD，请用户确认自然语言夹具
无内部矛盾（设计 §6.1：dry-run 只能证明程序路径，不能证明夹具无矛盾；
此项确认是冻结前的必要人工步骤）。未确认前不进入 Task 8 的真实冻结。

---

### Task 6: REV probe 骨架 — slot 计划、守卫客户端与真实评审路径

**Files:**
- Modify: `examples/story_enactment/world_response_rev_probe.py`
- Test: `examples/story_enactment/world_response_rev_probe_test.py`

**Interfaces:**
- Consumes: `request_ledger.RequestLedger`；`fixed_model_probe` 的
  `public_history_text`/`_HISTORY`/哈希与时间 helper（沿用旧批的
  `fp` 委托模式）；`run.make_gm_params` +
  `story_config.make_example_config`。
- Produces:
  - `SlotPlan(slot_id, kind, frozen_text=None)`（frozen dataclass，复制自旧批 probe.py:349–355）
  - `slot_plan(case) -> tuple[SlotPlan, SlotPlan]`（s1_generation frozen +
    s2_review real，frozen_text=`json.dumps({'stage_directions': …})`）
  - `attempt_id(sample_id) -> str`（`f'{sample_id}__m4'`）
  - `SolSlotModel(model_name, *, ledger, inner=None, api_key='', api_base='')`
    （LanguageModel 子类：`begin_attempt(slots)`、`cursor`；
    frozen 槽走 `record_frozen_slot` 零额度；real 槽
    begin_request → 转发 → finish_request）
  - `_SlotGuardedClient/_SlotGuardedChat/_SlotGuardedCompletions`
    （改写自旧批 probe.py:267–341：首次 `create()` 通过
    `ledger.record_raw_response` 在解析之前落盘原始响应并记录
    boundary kwargs；同 slot 第二次 `create()` →
    `record_second_request_block` + 抛 `SecondRequestBlocked`，
    到达模拟提供方之前被阻止）
  - `_run_rev_case(model, case, gm) -> dict`（复制旧批
    `_run_rev_sample`（probe.py:1592–1628）：真实 director +
    `record_action_result` + 真实 `make_final_output_step`；
    `StoryOutputError` → `failure_kind` 区分 review_rejected /
    malformed_generation）
  - 辅助 `_make_director/_NoopAct/_FixedActor/_restore_history/
    _putative_event/_embedder/_BEATS`（自旧批最小复制，仅 REV 所需）

- [ ] **Step 1: 写失败测试（离线 fake inner）**

```python
class SlotDisciplineTest(unittest.TestCase):

  def _fake_inner(self, answer='{"valid": true, "reason": "ok"}'):
    # 旧批 _FakeClient/_FakeChat/_FakeCompletions 模式（probe.py:506-551）：
    # create() 返回 _FakeResponse(answer, 'gpt-6.1-sol')，记录 calls
    ...

  def test_frozen_generation_slot_spends_nothing(self):
    ledger = rl.RequestLedger(batch_dir, batch_id='t', global_cap=8, ...)
    model = probe.SolSlotModel('gpt-6.1-sol', ledger=ledger, inner=fake)
    model.begin_attempt(probe.slot_plan(case))
    model.sample_text('# Candidate event ...', max_tokens=2000)
    self.assertEqual(ledger.counts['global'], 0)
    events = rl.load_events(batch_dir / 'requests.jsonl')
    self.assertEqual(events[0]['event'], 'frozen_stub')

  def test_review_slot_forwards_exactly_once(self):
    raw = model.sample_text('# Story resolution semantic review ...')
    self.assertEqual(fake.completions.calls.__len__(), 1)
    self.assertEqual(ledger.counts['global'], 1)
    self.assertTrue((raw_dir / 'REV01__m4__s2_review.prompt.txt').exists())
    self.assertTrue((raw_dir / 'REV01__m4__s2_review.response.txt').exists())

  def test_prompt_is_persisted_before_and_raw_response_before_parsing(self):
    # fake 的 create() 内部断言：此刻 prompt.txt 与 started.json 已存在；
    # create() 返回前 response.txt 已写入（守卫客户端内完成）→
    # 让 adapter 解析故意抛 InvalidResponseError，响应文件仍在
    ...

  def test_second_request_of_slot_is_blocked_before_provider(self):
    # review 答复后令 adapter 触发第二次 sample_choice/create：
    # SecondRequestBlocked 抛出，fake.completions.calls 仍为 1，
    # sidecar first_content 已存，额度只扣一次
    ...

  def test_boundary_kwargs_are_archived(self):
    # finished 事件 boundary 含 requested_model/provider_model/
    # messages_count/boundary_kwargs_note（沿用旧批文案）
    ...
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/world_response_rev_probe_test.py -v`
Expected: FAIL

- [ ] **Step 3: 实现骨架**

模块 docstring：Sol-only REV 补验 probe（设计 §6），stages
`dry_run/freeze/run/summarize`；`_MODEL_TAG='m4'`、
`_MODEL_NAME='gpt-6.1-sol'`、`_ENV_KEY='AI_MODEL_4'`、
`_GLOBAL_CAP=8`、`_MODEL_CAPS={'m4': 8}`、`_GROUP_CAPS={'REV': 8}`、
`_SAMPLE_CAPS={'REV': 1}`、`_SDK_MAX_RETRIES=0`。复制旧批的
`_REVIEW_MARK`/`_GENERATION_MARK` 等提示指纹常量。`_run_rev_case`
与辅助函数按旧批原文最小复制（含注释），仅去掉 ACT/CHAIN/REP 分支。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/world_response_rev_probe_test.py -v`
Expected: PASS

---

### Task 7: REV probe run 路径 — 恢复、停止规则与失败分类

**Files:**
- Modify: `examples/story_enactment/world_response_rev_probe.py`
- Test: `examples/story_enactment/world_response_rev_probe_test.py`

**Interfaces:**
- Consumes: Task 6 全部；`_load_settings()`（只读
  `AI_MODEL_4`/`AI_API_KEY`/`AI_BASE_URL`，名称不符即 `BatchStop`）。
- Produces:
  - `run_stage(batch_dir, *, authorize_skip_pending: Sequence[str] = ()) -> None`
  - `_verify_frozen(batch_dir) -> None`（protocol/fixtures/snapshot 哈希复验）
  - `_story_failure_kind(error) -> str`（复制旧批 1769–1778）
  - FLAGS：`--stage`、`--batch_dir`、`--dry_run_dir`、
    `--authorize_skip_pending`（逗号分隔 slot 列表，默认空）、
    `--reference_snapshot`（可选，Task 8 用）

- [ ] **Step 1: 写失败测试**

```python
class RunStageTest(unittest.TestCase):

  def _prepared_batch(self):
    # tmp 目录：先调 freeze_stage（Task 8 实现；此处可先构造
    # protocol/fixtures/snapshot 三文件 + 初始 report.json 的最小等价物）
    ...

  def test_positive_and_negative_verdicts_classify(self):
    # fake inner 按 slot 返回批准 JSON（REV01）与拒绝文本（REV03）：
    # REV01 attempt completed、review_outcome=approved；
    # REV03 sample_failure、failure_kind=review_rejected，且不重试
    #（provider calls == 每样本 1）
    ...

  def test_format_error_is_sample_failure_not_batch_stop(self):
    # 评审答复非法 JSON → StoryOutputError → failure_kind=malformed_generation；
    # started 仍计费、不退款
    ...

  def test_api_error_books_budget_and_stops_batch(self):
    # fake create 抛 RuntimeError → attempt batch_stop；ledger 有一条
    # started 无 finished（pending）；再入 run_stage 直接 SystemExit
    ...

  def test_pending_slot_blocks_resume_until_authorized_skip(self):
    # 预置一条 started-only 账本 → run_stage SystemExit 且不追加任何
    # started；authorize_skip_pending=[该 slot] → 该样本记 sample_failure
    # （reason 含 'user-authorized deviation'，写入 report['deviations']，
    #  范围只含该 slot），其余样本继续
    ...

  def test_budget_full_stops_before_sending(self):
    # 预置 8 条 started（global=8）→ run_stage 在任何 provider 调用前
    # 抛 LedgerBudgetError/SystemExit；fake calls == 0
    ...

  def test_completed_attempts_are_skipped_on_resume(self):
    # report 已有 REV01 completed → 重跑只执行 REV02–08，
    # 每样本恰一次 provider create
    ...

  def test_frozen_verify_refuses_drift(self):
    # 修改 fixtures.json 一字节 → run_stage SystemExit
    ...
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/world_response_rev_probe_test.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 run 路径**

规则（设计 §4.3、§6.2）：

- 重入：`_verify_frozen` → 载入 report → `RequestLedger(batch_dir, …)`
  从账本重建计数（绝不读 report 计数）→ 有 `batch_stop` 历史或
  pending slots 时 SystemExit（列出 pending；`authorize_skip_pending`
  命中的 slot 除外：记 deviation、剩余 real slot 记 skipped、
  attempt 记 sample_failure，偏差只覆盖该 slot）。
- 每样本：`_verify_frozen` → `_run_attempt`（Task 6 路径；
  `SecondRequestBlocked`/`InvalidResponseError` → sample_failure +
  下游 skipped；`LedgerBudgetError`/`PlanExhausted`/`BatchStop`/未知
  异常 → batch_stop，写 `report['stop']` 后 SystemExit）→ 保存 report
  （counts 一律 `ledger.counts` 派生刷新，含
  `second_request_blocks`，修复旧批 counts 失衡缺陷）。
- SDK 客户端 `with_options(max_retries=0)`；单 slot 一次请求；
  每次真实调用前 begin_request 已在守卫路径内完成预扣。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/world_response_rev_probe_test.py -v`
Expected: PASS

---

### Task 8: REV probe freeze / dry_run / summarize 与依赖披露

**Files:**
- Modify: `examples/story_enactment/world_response_rev_probe.py`
- Test: `examples/story_enactment/world_response_rev_probe_test.py`

**Interfaces:**
- Consumes: Tasks 5–7；`provenance.py` 的快照惯例。
- Produces:
  - `protocol_payload() -> dict`（models[m4]、identity_disclosure、
    budgets{global 8, per_model 8, per_group REV 8, per_sample 1}、
    slots、execution_order、request_settings（无重试、slot 守卫、
    started 预扣不退款、pending 不重发）、pass_criteria（合法两例接受、
    非法六例拒绝；格式错误与未决不算通过；标注与人工确认分列）、
    stop_rules、labeling_policy、model_scope='Sol-only'）
  - `fixtures_payload() -> dict`（batch、shared{base_history_source,
    fixture_events, fixture_event_sources}、samples[8]、
    revision_record、labels policy）
  - `source_snapshot_payload() -> dict`（`_SNAPSHOT_FILES`：本 probe 两
    文件、request_ledger 两文件、fp/run/story_config/testing_stubs/
    provenance、旧批涉及的 concordia 生产模块、设计与计划两文档；
    逐文件 sha256+bytes）
  - `freeze_stage(batch_dir)`（拒绝已存在目录；写三文件 + 初始
    report.json：counts 全零 + planned_attempts=8）
  - `dry_run_stage(dry_run_dir)`（离线 provider：生成槽按 frozen 文本、
    评审槽固定批准 JSON；断言每样本恰一 frozen 一 review、零真实客户端、
    标签/预期不进提示；写 dry_run_report.json）
  - `summarize_stage(batch_dir, *, reference_snapshot: pathlib.Path | None)`
    （离线：从账本+raw 重建 integrity；rev_classification{approved,
    rejected_expectedly(=拒绝且 expected false), false_rejection,
    missed_detection, format_error, api_error, pending, not_run}；
    rev_gate=两正例 approved 且六反例 rejected_expectedly 且
    format_error=api_error=pending=0 且 real_calls<=8；
    overall_pass=rev_gate 且 human_confirmed（默认 pending → False）；
    `dependency_drift_vs_reference`：与 `--reference_snapshot` 的
    交集文件哈希差异清单，未提供时记 'not compared'）

- [ ] **Step 1: 写失败测试**

```python
class FreezeStageTest(unittest.TestCase):

  def test_freeze_refuses_existing_dir_and_writes_payloads(self): ...
  def test_protocol_declares_sol_only_scope_and_caps(self):
    protocol = probe.protocol_payload()
    self.assertEqual(protocol['model_scope'], 'Sol-only')
    self.assertEqual(protocol['budgets']['global_requests'], 8)
    self.assertIn('格式错误与未决不算通过', protocol['pass_criteria'])

  def test_snapshot_covers_probe_and_ledger_and_production(self):
    paths = [f['path'] for f in probe.source_snapshot_payload()['files']]
    for required in ('examples/story_enactment/world_response_rev_probe.py',
                     'examples/story_enactment/request_ledger.py',
                     'concordia/contrib/components/game_master/'
                     'story_resolution.py'):
      self.assertIn(required, paths)


class DryRunStageTest(unittest.TestCase):

  def test_dry_run_proves_paths_with_zero_real_clients(self):
    # mock openai.OpenAI 断言未调用；8 样本、每样本 1 冻结 + 1 评审槽；
    # 提示文件 16 份；findings 为空（含「expected/标签不出现在任何提示」
    # 的 hygiene 断言）
    ...


class SummarizeStageTest(unittest.TestCase):

  def test_expected_rejection_is_passing_negative_and_defects_are_separate(
      self,
  ):
    # 合成 attempts：REV01/02 approved、REV03-07 rejected_expectedly、
    # REV08 rejected_expectedly → rev_gate True；
    # 改一例反例 approved → missed_detection → gate False；
    # 一例格式错误 → format_error → gate False；一条 pending → gate False
    ...

  def test_integrity_rebuilt_from_ledger(self):
    # integrity.real_calls/by_model/by_group/second_request_blocks/
    # frozen_slots/pending 与账本重建一致（不复述 report 旧值）
    ...

  def test_human_confirmation_requires_human_labels(self):
    # 无 labels → 'pending'、overall_pass False；全 human labeled →
    # rev_gate True 时 overall_pass True
    ...

  def test_dependency_drift_lists_changed_intersection(self):
    # 合成 reference snapshot：交集文件改 1 个 → 清单恰好含该文件
    ...
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/world_response_rev_probe_test.py -v`
Expected: FAIL

- [ ] **Step 3: 实现三个阶段 + main 分发**

`main` 按 `--stage` 分发（`dry_run` 默认输出
`/tmp/opencode/world_response_rev_probe_dry_run`）。本轮**不执行**
真实 `freeze`（新批次目录冻结与八次调用都留给授权轮，见「执行边界」）。

**2026-10-23 审阅修正（summarize）：**先核验冻结文件与源快照、审计账本
及其旁侧文件和请求正文，再核对结果与注册 slot。每个有可用 verdict 的
结果必须对应唯一的真实评审 started、completed 终态、原始响应及一致的
终态记录，并有一次冻结生成记录；响应按既有严格解析验证，不能只信报告
中的 completed/review_rejected。重复/未知人工标签必须报错，缺项或未完成人工
标注保持 pending。审计失败必须清除已保存的通过状态（overall_pass=false、
summary=null），明确报错，不得保留旧 pass，也不得继续付费执行。增加回归：
账本缺失/清空/重复、单例证据缺失、重复/未知标签、旁侧元数据与 prompt 漂移。
REV01 的 stage_directions 也属于修订字段，REVISION_RECORD 记录旧/新全文
与理由；REV03–08 九字段继续原样继承，不修改旧批次。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/world_response_rev_probe_test.py -v`
Expected: PASS

- [ ] **Step 5: 静态检查**

```bash
pylint --errors-only examples/story_enactment/world_response_rev_probe.py && \
pyrefly check examples/story_enactment/world_response_rev_probe.py
```

---

### Task 9: README 更新

**Files:**
- Modify: `examples/story_enactment/README.md`

- [ ] **Step 1: 在 Adjudication-Chain Probe 一节之后新增两节**

`## World-Response Probe Closeout (S1)`：一段说明 + 命令块
（`--stage` 无；直接 `python -m ...world_response_probe_closeout
--source_batch_dir=... --output_dir=...`），说明只读勘误、零调用、
四个输出文件、三层状态语义、源批次不可写。

`## Sol REV Re-Validation Probe (S2)`：一段说明（Sol-only、八例、
每例一次真实评审、上限 8、冻结生成槽零成本、账本预扣规则、
pending 不重发）+ 四个 stage 的命令块（标注：真实 `freeze`/`run`
需按新协议另行授权）。

- [ ] **Step 2: 更新 File Structure 小节**

在文件树中加入
`request_ledger.py`、`world_response_probe_closeout.py`、
`world_response_rev_probe.py` 三行及一行 `*_test.py` 汇总。

- [ ] **Step 3: 验证 README 示例与实际 CLI 一致**

```bash
source venv/bin/activate && python -m examples.story_enactment.world_response_probe_closeout --help 2>&1 | head -5
```

（确认 flag 名与 README 一致。）

---

### Task 10: 全量验证、真实批次勘误执行与交付记录

**Files:**
- Create: `run_results/story_enactment/world_response_fixed_probe_closeout/<UTC时间戳>/`（工具生成）
- Modify: `docs/superpowers/notes/2026-10-21-story-enactment-probe-accounting-closeout.md`

- [ ] **Step 1: 全量测试与静态检查**

```bash
source venv/bin/activate && pytest examples -x -q && \
pytest concordia -q && pylint --errors-only examples/story_enactment && \
pyrefly check examples/story_enactment
```

Expected: 全部 PASS / 无 error（`pytest concordia` 为回归确认；
既有问题与新增问题分列记录——若 examples 存在与本次无关的既有失败，
如实分开记录，不修复不掩盖）。

- [ ] **Step 2: REV probe 无网络 dry-run 证据**

```bash
python -m examples.story_enactment.world_response_rev_probe --stage=dry_run \
  --dry_run_dir=/tmp/opencode/world_response_rev_probe_dry_run
```

Expected: exit 0；`dry_run_report.json` 显示 8 attempts、
16 提示文件、0 真实客户端构造、findings 为空。

- [ ] **Step 3: 对真实冻结批次执行 S1 勘误（零网络）**

```bash
UTC=$(date -u +%Y%m%dT%H%M%SZ) && \
python -m examples.story_enactment.world_response_probe_closeout \
  --source_batch_dir=run_results/story_enactment/world_response_fixed_probe/20261020T075501Z \
  --output_dir=run_results/story_enactment/world_response_fixed_probe_closeout/$UTC
```

Expected: exit 0；输出目录含
`input_manifest.json`、`corrected_report.json`、`corrections.json`、
`README.md`；人工抽查 corrected_report 关键值：
total=82、m1=39、ACT=22、responses_observed=81、no_result=1、
format_failures=2、frozen=16、not_needed_no_branch=6、
overall_pass_preserved=false、human_confirmation='pending'。

- [ ] **Step 4: 写交付记录并停在审阅点**

`docs/superpowers/notes/2026-10-21-story-enactment-probe-accounting-closeout.md`
包含：

1. 工作区记录（实施前 `git status` 快照 + 实施后差集 = 实际触碰文件清单）。
2. 工程验证层（列出每个测试文件覆盖的设计 §8 验收条目）。
3. 本批纠正值（82/39/22、second_request_blocks 2、冻结槽 16、
   not_needed 6）与原结论保留情况（overall_pass=false、REV 冻结标签
   12/4 不变、ACT03__m1 不判 Gemini 能力）。
4. 未证明事项：Gemini 格式兼容未进生产（G 项离线方案保留在 closeout
   输出与设计 §7）；Sol REV 八例未执行；完整故事（S3）未开协议；
   私密 NPC 通道、联合行动仍未实现。
5. 停止点声明：真实执行需另获授权（新目录冻结 + 最多八次请求）。

- [ ] **Step 5: 向用户汇报并停止**

按设计 §10 列明修改范围、纠正值、保留结论、未证明事项，等待审阅。
不 commit、不清理。

---

## 执行边界（再次强调）

- 本计划所有步骤零真实模型调用；Task 10 Step 3 只读解析旧批次。
- S2 的 `freeze`（在 `run_results` 下建新批次目录）与 `run`（最多八次
  真实评审请求）不在本计划内执行——授权后按设计 §6.2 另立执行点。
- G（Gemini 生产适配器兼容）与 S3（完整故事协议）是后续独立任务。

## Self-Review 记录

- 设计 §8 验收条目 → 任务映射：started 未 finished 计数（Task 1/2）、
  恢复由账本重建（Task 2/7）、本批形状重现 82/39/22（Task 1）、
  第二请求只计首请求/冻结槽与 No 分支不计费/预算满先停/未决不重发
  （Task 2/4/7）、截断与漂移明确失败（Task 1/3/7）、预期拒绝为通过
  负例与夹具缺陷单列（Task 4/8）、人类标签缺项不确认（Task 4/8）、
  closeout 不建客户端/不读凭据/不写旧目录/符号链接拒绝/前后清单一致
  （Task 3/4/10）、八例真实工厂+恰一次转发+生成零成本+五类失败覆盖
  （Task 6/7/8）。
- 类型一致性：`RequestLedger` 方法名在 Task 2/6/7/8 间一致；
  `rev_gate`/`dependency_drift_vs_reference` 字段名在 Task 8 测试与
  实现说明间一致。
- 占位符扫描：REV03–08 数据以冻结文件路径+字段清单+本地核对脚本指定
  （字节精确）；REV01/02 修订文本全文内联；无 TBD/TODO。
