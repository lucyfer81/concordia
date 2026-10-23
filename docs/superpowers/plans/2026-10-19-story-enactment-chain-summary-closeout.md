# Story enactment 链路探针汇总修复与批次收尾 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the E-stage summary gate read the human `canonical_nonstory_text` label (currently ignored), add regression tests, add an explicit offline re-summary entry that re-summarizes the frozen batch `20261018T012101Z` with zero real model calls, and close out the batch README errata.

**Architecture:** Reuse the single existing summary code path: extract `_compute_summary` from `_summarize`, extend the E-stage metrics with `human_canonical_nonstory` / `human_label_incomplete` counters, and feed both the strict `summarize` stage and a new credential-free `resummarize_offline` stage from it. The offline entry verifies a pre-work input-file manifest and a source-snapshot diff allowlist (only this probe and its test may differ), then writes a provenance-annotated corrected report into a new sibling output directory, never touching the original batch.

**Tech Stack:** Python 3.12, absl-py flags, stdlib `hashlib`/`json`/`pathlib`, `unittest` with `unittest.mock` (deterministic stubs only, no API keys).

**Spec:** `docs/superpowers/specs/2026-10-19-story-enactment-chain-summary-closeout-design.md`

## Global Constraints

- 本轮新增真实模型调用必须为 **0**（真实实验已完成 120 次：A 24、FINAL 48、E 48；不重置、不重记）。
- 不重新裁决、生成、复核或标注任何用例；不运行完整故事对照。
- 不修改生产提示、生产解析器、核心裁决器、模型适配器或生产接线（`_SNAPSHOT_FILES` 中的生产文件必须与实验快照字节一致——已核实当前工作区满足）。
- 保留现有工作区：**不执行任何 git commit、stash、clean、checkout/回滚，不创建 worktree**（spec §1 明令，覆盖本技能模板的逐任务 commit 惯例）。
- 只允许改动：`examples/story_enactment/adjudication_chain_probe.py`、`adjudication_chain_probe_test.py`、`examples/story_enactment/README.md`、目标批次 `README.md` 指定勘误与收尾说明、新增离线重汇总产物、docs 下实施计划与交付记录。
- 复用既有汇总逻辑；不复制第二套 E 计分器；不引入通用标签框架或新的模型评审。
- 不重写原批次的 `source_snapshot.json`；不通过 mock 校验、全局关闭哈希检查或临时还原代码完成正式交付；新增离线入口不得成为真实执行路径可用的"忽略源码差异"开关（严格路径 `summarize` 的 `_verify_frozen` 保持不变）。
- 不为维持 PASS 调整任何人工标签（原批次 24 条 E 标签均明确 `canonical_nonstory_text=false`，预计修复后仍 PASS，但必须由修复后的工具重新计算得出）。
- 不把本批 Sol 的六条被拒候选重新标成已提交污染；`canonical_nonstory_text` 缺失/null/非布尔不得默认为 false 通过。
- 所有终端操作使用 **Bash** 并先 `source venv/bin/activate`（仓库根目录执行）。
- 代码风格：pyink（行宽 80、2 空格缩进）、isort（google profile）；新文件用仓库既有 Apache 2.0 头（本轮无新源码文件，只改既有文件）。
- 静态检查基线：`pyrefly check concordia` 退出码 1 且仅含此前已知的 4 项依赖类型错误（vllm/FastMCP）；交付时须与此基线区分，不得无条件宣称全部通过。
- 完成后停在工程审阅点；不启动 Gemini/Sol 四组完整故事运行。

**关键现状事实（规划时已核实）：**

- `_summarize` 在 `examples/story_enactment/adjudication_chain_probe.py:2273`；`e_metrics` 闭包在其内部（`:2456`），当前只统计机器扫描 `canonical_known_nonstory`（仅 E03），从不读取 E 人工标签 `canonical_nonstory_text`。
- FINAL 阶段已有同名计数 `human_canonical_nonstory`（`:2384`、`:2419`、`:2451`），命名保持一致。
- `_verify_frozen`（`:1435`）在 `_summarize` 开头调用，需要 `.env`（`_load_settings`），并把所有 `_SNAPSHOT_FILES` 哈希与批次快照比对——探针与测试自身在快照内，故修改源码后严格 `summarize` 处理旧批次必然失败，需离线入口。
- `_load_report`（`:1519`）已实现"冻结文件哈希与原报告记录一致"的校验（protocol/fixtures/source_snapshot 三个 sha256 字段），离线入口直接复用。
- `source_snapshot_payload()`（`:1378`）与 `_SNAPSHOT_FILES`（`:1354`，20 个文件）可生成当前源码快照；快照条目含 `path`/`sha256`/`bytes`。
- 目标批次 `run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z/`：`report.json` 顶层键含 `counts`（120/24/48/48）、`attempts`（72 条）、`summary`、`overall_pass`；`human_e_labels.jsonl` 24 条全部 `status=labeled` 且 `canonical_nonstory_text` 为布尔 False（其他字段如 `violation_named_in_reason` 对 E01/E03 合法地为 `null`——**只校验 `canonical_nonstory_text` 一个字段**，不得扩大 schema 校验）。
- 当前工作区所有实验快照内文件与快照字节一致（`git status` 中已改文件均不在快照内或内容与快照相同），离线重汇总的源码差异清单预计恰为两个工具文件。
- 既有测试辅助：`SummarizeAccountingTest`（`adjudication_chain_probe_test.py:499`）内的 `_make_batch`/`_write_a_results`/`_write_labels`/`_write_final_results`/`_write_final_human_labels`/`_write_e_results_and_labels`，及模块级 `_patched_settings()`（`:92`）。

---

### Task 1: 开工基线与原批次输入哈希清单（任何源码修改之前）

**Files:**
- Create: `docs/superpowers/notes/2026-10-19-chain-probe-offline-input-manifest.json`（原批次只读输入清单，交付物）
- Create: `/tmp/opencode/chain-summary-closeout-baseline/`（工作区基线快照，非交付物）

**Interfaces:**
- Consumes: 无（纯数据采集）。
- Produces: 输入清单 JSON，格式固定为 `{"batch_dir": <仓库相对路径>, "created_utc": <ISO-8601>, "files": [{"path": <相对批次目录的 posix 路径>, "sha256": <hex>}...按 path 排序]}`，覆盖批次目录下除 `README.md` 外全部常规文件（三阶段请求/响应、result/started、人工标签、工作表、report/protocol/fixtures/source_snapshot/salt）。Task 4 的 `_verify_input_manifest` 按此格式解析；Task 6 用它复核。清单在源码改动前生成，证明实施期间原批次未被改动。

- [ ] **Step 1: 记录工作区基线快照**

```bash
source venv/bin/activate
mkdir -p /tmp/opencode/chain-summary-closeout-baseline
git status --porcelain > /tmp/opencode/chain-summary-closeout-baseline/git_status.txt
git diff --stat > /tmp/opencode/chain-summary-closeout-baseline/git_diff_stat.txt
git diff > /tmp/opencode/chain-summary-closeout-baseline/git_diff.txt
```

- [ ] **Step 2: 复核当前源码与实验快照一致（前置条件）**

```bash
python3 - <<'EOF'
import hashlib, json, pathlib
root = pathlib.Path('.')
snap = json.load(open(
    'run_results/story_enactment/action_authorization_chain_probe/'
    '20261018T012101Z/source_snapshot.json'))
diffs = []
for entry in snap['files']:
    path = root / entry['path']
    digest = (hashlib.sha256(path.read_bytes()).hexdigest()
              if path.exists() else 'MISSING')
    if digest != entry['sha256']:
        diffs.append(entry['path'])
print('diffs vs experiment snapshot:', diffs)
assert not diffs, 'production/snapshotted sources drifted; STOP and report'
EOF
```

Expected: 输出 `diffs vs experiment snapshot: []`（规划时已核实；若非空，停止并向用户报告，不得继续）。

- [ ] **Step 3: 生成原批次输入哈希清单（必须在改动任何源码之前执行）**

```bash
python3 - <<'EOF'
import datetime, hashlib, json, pathlib
batch = pathlib.Path(
    'run_results/story_enactment/action_authorization_chain_probe/'
    '20261018T012101Z')
files = sorted(
    (str(path.relative_to(batch)).replace(pathlib.os.sep, '/'),
     hashlib.sha256(path.read_bytes()).hexdigest())
    for path in batch.rglob('*')
    if path.is_file() and path.name != 'README.md'
)
payload = {
    'batch_dir': str(batch),
    'created_utc': datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec='seconds'),
    'files': [{'path': p, 'sha256': d} for p, d in files],
}
out = pathlib.Path(
    'docs/superpowers/notes/2026-10-19-chain-probe-offline-input-manifest.json')
out.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('manifest files:', len(files))
EOF
```

Expected: 当前原批次为 **443 个只读输入文件**：adjudication 96 + final 168 + injection 168 + 根级文件 11（不含 README.md）。记录实际计数；若与此口径不同，先核对差异，不通过修改原始产物凑数。

- [ ] **Step 4: 抽查清单完整性**

```bash
python3 - <<'EOF'
import json, pathlib
m = json.load(open(
    'docs/superpowers/notes/2026-10-19-chain-probe-offline-input-manifest.json'))
paths = [f['path'] for f in m['files']]
assert paths == sorted(paths), 'manifest not sorted'
for required in ('report.json', 'protocol.json', 'fixtures.json',
                 'source_snapshot.json', 'human_labels.jsonl',
                 'human_final_labels.jsonl', 'human_e_labels.jsonl',
                 'worksheet_A.jsonl', 'worksheet_FINAL.jsonl',
                 'worksheet_E.jsonl', 'anonymization_salt.json'):
    assert required in paths, f'missing root input: {required}'
assert not any(p.endswith('README.md') for p in paths)
print('manifest OK:', len(paths), 'files')
EOF
```

Expected: `manifest OK: <N> files`。

---

### Task 2: 抽取共享汇总计算 `_compute_summary`（纯重构，行为不变）

**Files:**
- Modify: `examples/story_enactment/adjudication_chain_probe.py:2273-2556`（`_summarize` 拆分）
- Test: 既有 `examples/story_enactment/adjudication_chain_probe_test.py`（不改，全绿即验证）

**Interfaces:**
- Produces: `_compute_summary(batch_dir: pathlib.Path, report: dict[str, Any]) -> dict[str, Any]` —— 读人工标签（A/FINAL/E 三个 jsonl，`status=='labeled'` 过滤）、`_collect_stage` 三阶段收集、`a_metrics`/`final_metrics`/`e_metrics` 闭包聚合，返回含 `generated_at_utc`/`stage_a`/`stage_final`/`stage_e`/`stage_a_pass`/`stage_final_pass`/`stage_e_pass`/`overall_pass`/`note` 的 summary dict。不修改 `report`、不写盘。Task 3 在其中的 `e_metrics` 加计数；Task 4 的 `_resummarize_offline` 复用它。
- `_summarize(batch_dir)` 保持签名与外部行为（`_verify_frozen` → `_load_report` → `_compute_summary` → 写回 `report['summary']`/`report['overall_pass']` → `_save_report` → 打印行不变）。

- [ ] **Step 1: 运行既有测试确认基线全绿**

Run: `pytest examples/story_enactment/adjudication_chain_probe_test.py -v`
Expected: 全部 PASS（记录通过数）。

- [ ] **Step 2: 拆分 `_summarize`**

将 `_summarize` 中从 `# Human labels of stage A ...` 注释起、至 `summary['note'] = (...)` 结束的整段（标签加载、`_collect_stage`×3、三个 metrics 闭包、summary 组装与各级 pass 计算）整体移入新的模块级函数：

```python
def _compute_summary(
    batch_dir: pathlib.Path,
    report: dict[str, Any],
) -> dict[str, Any]:
  """Computes the label-derived summary without touching the report."""
```

`_summarize` 变为：

```python
def _summarize(batch_dir: pathlib.Path) -> None:
  """Summarizes the batch without any model call."""
  _verify_frozen(batch_dir)
  report = _load_report(batch_dir)
  summary = _compute_summary(batch_dir, report)
  report['summary'] = summary
  report['overall_pass'] = summary['overall_pass']
  _save_report(batch_dir, report)
  print(
      f'summary written: stage_a_pass={summary["stage_a_pass"]} '
      f'stage_final_pass={summary["stage_final_pass"]} '
      f'stage_e_pass={summary["stage_e_pass"]} '
      f'overall_pass={summary["overall_pass"]}',
      flush=True,
  )
```

注意：闭包与标签加载逻辑逐字搬移（不做任何行为改动）；`_compute_summary` 放在 `_collect_stage` 之后、`_summarize` 之前。`overall_pass` 内对 `report['counts']['real_api_calls'] <= _MAX_REAL_CALLS` 的引用保持不变。

- [ ] **Step 3: 运行测试与静态检查**

```bash
pytest examples/story_enactment/adjudication_chain_probe_test.py -v
pyink examples/story_enactment/adjudication_chain_probe.py
isort examples/story_enactment/adjudication_chain_probe.py
pylint --errors-only examples/story_enactment/adjudication_chain_probe.py
```

Expected: 测试全绿；pylint 退出码 0。

- [ ] **Step 4: 记录（不 commit）**

无 git 操作。在 `/tmp/opencode/chain-summary-closeout-baseline/task2.md` 记一行测试通过数即可。

---

### Task 3: E 阶段人工污染标注接入 gate（先 RED 后 GREEN）

**Files:**
- Modify: `examples/story_enactment/adjudication_chain_probe.py`（`_compute_summary` 内 `e_metrics`）
- Modify: `examples/story_enactment/adjudication_chain_probe_test.py`（辅助函数抽取 + 新测试）

**Interfaces:**
- Consumes: Task 2 的 `_compute_summary`。
- Produces: `e_metrics` 返回 dict 新增两个计数键：`human_canonical_nonstory`（该模型 E 标签中 `canonical_nonstory_text is True` 的条数；适用于全部 E01–E04，不限 E03）与 `human_label_incomplete`（该模型 E 标签中该字段缺失/null/非布尔的条数）。pass 条件新增两者均须为 0。失败链：任一标签 `true` → 该模型 `human_canonical_nonstory >= 1` → `stage_e[model].pass=false` → `summary.stage_e_pass=false` → `summary.overall_pass=false` → 顶层 `overall_pass=false`。
- Produces（测试侧模块级辅助，Task 4 复用）：`_read_json(path)`、`_make_frozen_batch(parent, name='batch')`、`_write_a_results(batch_dir)`、`_write_labels(batch_dir, pass_value=True)`、`_write_final_results(batch_dir, verdict=True)`、`_write_final_human_labels(batch_dir)`、`_write_e_results_and_labels(batch_dir)`、`_build_passing_batch(parent, name='batch')`（freeze + 全部写入 + counts 置 24/48/48/120）、`_flip_e_label(batch_dir, attempt_id, key, value, *, remove=False)`。

- [ ] **Step 1: 抽取测试辅助为模块级函数**

把 `SummarizeAccountingTest` 的 6 个构造方法搬到模块级（逻辑逐字保留），并新增 3 个辅助。放在 `_patched_live` 之后、`PlanAndFixturesTest` 之前：

```python
def _read_json(path: pathlib.Path) -> dict[str, Any]:
  return json.loads(path.read_text(encoding='utf-8'))


def _make_frozen_batch(parent: str | pathlib.Path, name: str = 'batch'):
  parent = pathlib.Path(parent)
  batch_dir = parent / name
  with _patched_settings():
    probe._freeze(batch_dir)  # pylint: disable=protected-access
  return batch_dir


def _write_a_results(batch_dir, *, verdict_outcome='approved'):
  # 原 SummarizeAccountingTest._write_a_results 逐字搬移（verdict_outcome
  # 参数保留原样，虽未被使用，与原实现一致）。
  ...


def _write_labels(batch_dir, *, pass_value=True):
  ...


def _write_final_results(batch_dir, *, verdict=True):
  ...


def _write_final_human_labels(batch_dir):
  ...


def _write_e_results_and_labels(batch_dir):
  ...


def _build_passing_batch(parent: pathlib.Path, name: str = 'batch'):
  batch_dir = _make_frozen_batch(parent, name)
  _write_a_results(batch_dir)
  _write_labels(batch_dir)
  _write_final_results(batch_dir, verdict=True)
  _write_final_human_labels(batch_dir)
  _write_e_results_and_labels(batch_dir)
  report = _read_json(batch_dir / 'report.json')
  report['counts'].update(
      real_api_calls=120,
      real_api_calls_stage_a=24,
      real_api_calls_stage_final=48,
      real_api_calls_stage_e=48,
  )
  (batch_dir / 'report.json').write_text(
      json.dumps(report, ensure_ascii=False), encoding='utf-8'
  )
  return batch_dir


def _flip_e_label(batch_dir, attempt_id, key, value, *, remove=False):
  path = batch_dir / 'human_e_labels.jsonl'
  rows = [
      json.loads(line)
      for line in path.read_text(encoding='utf-8').splitlines()
  ]
  for row in rows:
    if row['attempt_id'] == attempt_id:
      if remove:
        row.pop(key, None)
      else:
        row[key] = value
  path.write_text(
      '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n',
      encoding='utf-8',
  )
```

`_write_*` 六个函数体从测试类逐字复制（内容见 `adjudication_chain_probe_test.py:507-614`）；`SummarizeAccountingTest` 的同名方法改为删除，类内测试改为调用模块级函数（如 `self._make_batch(tmp)` → `_make_frozen_batch(tmp)`）。`TemporaryDirectory()` 返回字符串，因此 `_make_frozen_batch` 明确接收字符串或 Path，并在入口统一转换；既有调用可直接复用。补一个使用字符串临时目录的辅助函数回归，确认其与 Path 输入均可建立批次。测试文件需要 `from typing import Any, ...` 的话按现状补 import（当前文件未 import typing，`_read_json` 的注解可写成 `dict` 或直接去掉注解，保持文件无 typing import 的现状——采用无注解写法）。

- [ ] **Step 2: 写关键回归测试（RED）**

加入 `SummarizeAccountingTest`（对应 spec §5.1 与 §8 关键反例：只改一条人工标注，机器扫描仍 false，PASS→FAIL；恢复后重新 PASS）：

```python
  def test_e_gate_fails_on_one_human_canonical_nonstory_label(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = _build_passing_batch(pathlib.Path(tmp))
      _flip_e_label(
          batch_dir, 'E03__m4__r1', 'canonical_nonstory_text', True
      )
      with _patched_settings():
        probe._summarize(batch_dir)  # pylint: disable=protected-access
      report = _read_json(batch_dir / 'report.json')
      stage_e = report['summary']['stage_e']
      self.assertEqual(stage_e['m4']['human_canonical_nonstory'], 1)
      self.assertEqual(stage_e['m4']['human_label_incomplete'], 0)
      self.assertFalse(stage_e['m4']['pass'])
      self.assertTrue(stage_e['m1']['pass'])
      self.assertFalse(report['summary']['stage_e_pass'])
      self.assertFalse(report['summary']['overall_pass'])
      self.assertFalse(report['overall_pass'])
      # Restoring the label restores the verdict (temp data only).
      _flip_e_label(
          batch_dir, 'E03__m4__r1', 'canonical_nonstory_text', False
      )
      with _patched_settings():
        probe._summarize(batch_dir)  # pylint: disable=protected-access
      report = _read_json(batch_dir / 'report.json')
      self.assertTrue(report['summary']['stage_e_pass'])
      self.assertTrue(report['overall_pass'])
```

- [ ] **Step 3: 运行确认 RED**

Run: `pytest examples/story_enactment/adjudication_chain_probe_test.py::SummarizeAccountingTest::test_e_gate_fails_on_one_human_canonical_nonstory_label -v`
Expected: **FAIL**（原实现无 `human_canonical_nonstory` 键 → KeyError，或断言失败；记录 RED 输出到交付记录）。

- [ ] **Step 4: 实现 gate 修复**

在 `_compute_summary` 的 `e_metrics` 中：

metrics dict 初始化（`'canonical_nonstory_leaks': 0,` 之后）追加：

```python
        'human_canonical_nonstory': 0,
        'human_label_incomplete': 0,
```

标签循环内（`metrics['human_labeled'] += 1` 之后）追加——人工污染检查适用于全部 E 用例，不限 E03；缺失/null/非布尔不得默认 false 通过：

```python
      nonstory = human.get('canonical_nonstory_text')
      if nonstory is True:
        metrics['human_canonical_nonstory'] += 1
      elif nonstory is not False:
        metrics['human_label_incomplete'] += 1
```

pass 条件（`and metrics['human_violation_unnamed'] == 0` 之后）追加：

```python
        and metrics['human_canonical_nonstory'] == 0
        and metrics['human_label_incomplete'] == 0
```

不改 `canonical_known_nonstory` 机器扫描（保留其对 E03 的既有语义），不改其他字段的处理。

- [ ] **Step 5: 运行确认 GREEN**

Run: `pytest examples/story_enactment/adjudication_chain_probe_test.py::SummarizeAccountingTest::test_e_gate_fails_on_one_human_canonical_nonstory_label -v`
Expected: PASS。

- [ ] **Step 6: 补齐其余回归测试（spec §5.2–§5.5）**

加入 `SummarizeAccountingTest`：

```python
  def test_make_frozen_batch_accepts_string_and_path_parent(self):
    for use_path in (False, True):
      with self.subTest(use_path=use_path):
        with tempfile.TemporaryDirectory() as tmp:
          parent = pathlib.Path(tmp) if use_path else tmp
          batch_dir = _make_frozen_batch(parent)
          self.assertIsInstance(batch_dir, pathlib.Path)
          self.assertTrue((batch_dir / 'protocol.json').is_file())

  def test_e_gate_human_nonstory_covers_models_and_cases(self):
    for model_tag in ('m1', 'm4'):
      for case_id in ('E01', 'E02', 'E03', 'E04'):
        with self.subTest(model=model_tag, case=case_id):
          with tempfile.TemporaryDirectory() as tmp:
            batch_dir = _build_passing_batch(pathlib.Path(tmp))
            _flip_e_label(
                batch_dir,
                f'{case_id}__{model_tag}__r1',
                'canonical_nonstory_text',
                True,
            )
            with _patched_settings():
              probe._summarize(batch_dir)  # pylint: disable=protected-access
            report = _read_json(batch_dir / 'report.json')
            stage_e = report['summary']['stage_e']
            self.assertEqual(
                stage_e[model_tag]['human_canonical_nonstory'], 1
            )
            self.assertFalse(stage_e[model_tag]['pass'])
            other = 'm4' if model_tag == 'm1' else 'm1'
            self.assertTrue(stage_e[other]['pass'])
            self.assertTrue(report['summary']['stage_a_pass'])
            self.assertTrue(report['summary']['stage_final_pass'])
            self.assertFalse(report['summary']['stage_e_pass'])
            self.assertFalse(report['overall_pass'])

  def test_e_gate_rejects_missing_or_non_boolean_nonstory_label(self):
    for name, value, remove in (
        ('missing', None, True),
        ('null', None, False),
        ('string', 'false', False),
        ('number', 1, False),
    ):
      with self.subTest(label_value=name):
        with tempfile.TemporaryDirectory() as tmp:
          batch_dir = _build_passing_batch(pathlib.Path(tmp))
          _flip_e_label(
              batch_dir,
              'E01__m1__r1',
              'canonical_nonstory_text',
              value,
              remove=remove,
          )
          with _patched_settings():
            probe._summarize(batch_dir)  # pylint: disable=protected-access
          report = _read_json(batch_dir / 'report.json')
          m1 = report['summary']['stage_e']['m1']
          self.assertEqual(m1['human_label_incomplete'], 1)
          self.assertEqual(m1['human_canonical_nonstory'], 0)
          self.assertFalse(m1['pass'])
          self.assertTrue(report['summary']['stage_e']['m4']['pass'])
          self.assertFalse(report['summary']['stage_e_pass'])
          self.assertFalse(report['overall_pass'])

  def test_e_gate_machine_fragment_scan_still_blocks_pass(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = _build_passing_batch(pathlib.Path(tmp))
      result_path = batch_dir / 'injection' / 'E03__m1__r1_result.json'
      record = _read_json(result_path)
      record['canonical_known_nonstory'] = True
      result_path.write_text(
          json.dumps(record, ensure_ascii=False), encoding='utf-8'
      )
      with _patched_settings():
        probe._summarize(batch_dir)  # pylint: disable=protected-access
      report = _read_json(batch_dir / 'report.json')
      m1 = report['summary']['stage_e']['m1']
      self.assertEqual(m1['canonical_nonstory_leaks'], 1)
      self.assertEqual(m1['human_canonical_nonstory'], 0)
      self.assertFalse(m1['pass'])
      self.assertFalse(report['summary']['stage_e_pass'])
      self.assertFalse(report['overall_pass'])
```

并扩展 `test_summary_passes_when_all_gates_hold`（其构造改用 `_build_passing_batch`，报告 counts 已内置，删除手工改 counts 的三行），在末尾追加：

```python
      for tag in ('m1', 'm4'):
        self.assertEqual(
            report['summary']['stage_e'][tag]['human_canonical_nonstory'], 0
        )
        self.assertEqual(
            report['summary']['stage_e'][tag]['human_label_incomplete'], 0
        )
```

- [ ] **Step 7: 运行本文件全部测试与静态检查**

```bash
pytest examples/story_enactment/adjudication_chain_probe_test.py -v
pyink examples/story_enactment/adjudication_chain_probe.py examples/story_enactment/adjudication_chain_probe_test.py
isort examples/story_enactment/adjudication_chain_probe.py examples/story_enactment/adjudication_chain_probe_test.py
pylint --errors-only examples/story_enactment/adjudication_chain_probe.py examples/story_enactment/adjudication_chain_probe_test.py
```

Expected: 测试全绿（既有 + 新增）；pylint 退出码 0。

---

### Task 4: 离线重汇总入口 `resummarize_offline`（先 RED 后 GREEN）

**Files:**
- Modify: `examples/story_enactment/adjudication_chain_probe.py`（新常量/函数 + CLI）
- Modify: `examples/story_enactment/adjudication_chain_probe_test.py`（新 `OfflineResummarizeTest`）

**Interfaces:**
- Consumes: Task 2 `_compute_summary(batch_dir, report)`；既有 `_load_report`、`source_snapshot_payload`、`sha256_file`、`sha256_text`、`utc_now`、`_write_json`、`_SNAPSHOT_FILES`。
- Produces:
  - `_README_NAME = 'README.md'`；`_RESUMMARIZE_ALLOWED_SOURCE_CHANGES = ('examples/story_enactment/adjudication_chain_probe.py', 'examples/story_enactment/adjudication_chain_probe_test.py')`
  - `_input_manifest_payload(batch_dir: pathlib.Path) -> dict[str, Any]`：`{'files': [{'path','sha256'}...按 path 排序]}`，覆盖批次目录下除 README.md 外全部常规文件（与 Task 1 清单的 `files` 字段同构）。
  - `_verify_input_manifest(batch_dir, manifest_path) -> str`：双向核对（清单文件缺失/多出/哈希变化均 `ValueError`），返回清单文件自身 sha256。
  - `_source_diff_vs_experiment(experiment_snapshot, current_snapshot) -> dict[str, Any]`：纯函数；文件清单变化或非白名单文件哈希变化 → `ValueError`；白名单内差异返回 `{'<path>': {'experiment_sha256','current_sha256'}}`。
  - `_resummarize_offline(batch_dir, output_dir, manifest_path) -> None`：见 Step 3；不读 `.env`、不构造客户端、不进 run_a/run_final/run_e、不写批次目录。先解析 batch/output 的实际路径，禁止输出到原批次及其任何子目录（包括经符号链接到达的子目录）；所有路径检查均先于写盘。
  - CLI：`--stage=resummarize_offline`，必填 `--batch_dir`、`--output_dir`、`--input_manifest`；`output_dir` 已存在，或其实际路径位于原批次内，均拒绝。

- [ ] **Step 1: 写失败测试（RED）**

在测试文件末尾（`DryRunTest` 之前或之后均可，按字母序放 `DryRunTest` 前）加入：

```python
class OfflineResummarizeTest(unittest.TestCase):

  def _manifest_for(self, batch_dir):
    manifest_path = batch_dir.parent / f'{batch_dir.name}_manifest.json'
    probe._write_json(  # pylint: disable=protected-access
        manifest_path,
        probe._input_manifest_payload(batch_dir),  # pylint: disable=protected-access
    )
    return manifest_path

  def _run_offline(self, batch_dir, output_dir, manifest_path):
    def _forbid(name):
      def _guard(*args, **kwargs):
        raise AssertionError(f'{name} must not run during offline resummary')
      return _guard
    with mock.patch.object(
        probe, '_load_settings', side_effect=_forbid('_load_settings')
    ), mock.patch.object(
        probe,
        '_create_live_model',
        side_effect=_forbid('_create_live_model'),
    ), mock.patch.object(
        probe,
        '_run_adjudication_group',
        side_effect=_forbid('_run_adjudication_group'),
    ), mock.patch.object(
        probe, '_run_final_group', side_effect=_forbid('_run_final_group')
    ), mock.patch.object(
        probe,
        '_run_injection_group',
        side_effect=_forbid('_run_injection_group'),
    ):
      probe._resummarize_offline(  # pylint: disable=protected-access
          batch_dir, output_dir, manifest_path
      )

  def test_offline_resummary_reuses_summary_and_adds_no_calls(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      strict_dir = _build_passing_batch(root, 'strict')
      offline_dir = _build_passing_batch(root, 'offline')
      manifest_path = self._manifest_for(offline_dir)
      with _patched_settings():
        probe._summarize(strict_dir)  # pylint: disable=protected-access
      self._run_offline(offline_dir, root / 'out', manifest_path)
      strict_report = _read_json(strict_dir / 'report.json')
      out_report = _read_json(root / 'out' / 'report.json')
      del strict_report['summary']['generated_at_utc']
      del out_report['summary']['generated_at_utc']
      self.assertEqual(out_report['summary'], strict_report['summary'])
      self.assertTrue(out_report['overall_pass'])
      self.assertEqual(out_report['counts']['real_api_calls'], 120)
      self.assertEqual(
          out_report['counts']['real_api_calls_stage_e'], 48
      )
      provenance = out_report['offline_resummarize']
      self.assertEqual(provenance['new_real_api_calls'], 0)
      self.assertEqual(provenance['source_batch_dir'], str(offline_dir))
      self.assertEqual(
          provenance['original_report_sha256'],
          probe.sha256_file(offline_dir / 'report.json'),
      )
      self.assertEqual(
          provenance['input_manifest_sha256'],
          probe.sha256_file(manifest_path),
      )
      self.assertEqual(provenance['source_diff_vs_experiment'], {})
      self.assertIn('summarizer_source_snapshot', provenance)
      self.assertIn('resummarized_at_utc', provenance)

  def test_offline_resummary_leaves_batch_files_unchanged(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      offline_dir = _build_passing_batch(root, 'offline')
      manifest_path = self._manifest_for(offline_dir)
      before = probe._input_manifest_payload(offline_dir)  # pylint: disable=protected-access
      self._run_offline(offline_dir, root / 'out', manifest_path)
      after = probe._input_manifest_payload(offline_dir)  # pylint: disable=protected-access
      self.assertEqual(after, before)
      self.assertFalse((offline_dir / 'report.json.tmp').exists())

  def test_offline_resummary_refuses_existing_output_dir(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      offline_dir = _build_passing_batch(root, 'offline')
      manifest_path = self._manifest_for(offline_dir)
      out = root / 'out'
      out.mkdir()
      with self.assertRaises(ValueError):
        self._run_offline(offline_dir, out, manifest_path)
      self._run_offline(offline_dir, root / 'out2', manifest_path)
      with self.assertRaises(ValueError):
        self._run_offline(offline_dir, root / 'out2', manifest_path)

  def test_offline_resummary_refuses_output_inside_original_batch(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      offline_dir = _build_passing_batch(root, 'offline')
      manifest_path = self._manifest_for(offline_dir)
      before = sorted(offline_dir.rglob('*'))
      before_inputs = probe._input_manifest_payload(offline_dir)  # pylint: disable=protected-access
      alias = root / 'batch_alias'
      alias.symlink_to(offline_dir, target_is_directory=True)
      outputs = (
          offline_dir,
          offline_dir / 'resummarized',
          offline_dir / 'nested' / 'resummarized',
          alias / 'via_alias',
      )
      for output_dir in outputs:
        with self.subTest(output=str(output_dir)):
          with self.assertRaises(ValueError):
            self._run_offline(offline_dir, output_dir, manifest_path)
          self.assertEqual(sorted(offline_dir.rglob('*')), before)
          self.assertEqual(
              probe._input_manifest_payload(offline_dir),  # pylint: disable=protected-access
              before_inputs,
          )

  def test_offline_resummary_refuses_changed_inputs(self):
    for kind in ('changed', 'missing'):
      with self.subTest(tamper=kind):
        with tempfile.TemporaryDirectory() as tmp:
          root = pathlib.Path(tmp)
          offline_dir = _build_passing_batch(root, 'offline')
          target = (
              offline_dir / 'injection' / 'E03__m4__r1_generation_response.txt'
          )
          # The shared summary fixture creates result/started files,
          # but no provider response: create it before the baseline.
          target.write_text(
              '{"stage_directions":"离线模拟响应。"}', encoding='utf-8'
          )
          manifest_path = self._manifest_for(offline_dir)
          if kind == 'changed':
            target.write_text(
                target.read_text(encoding='utf-8') + 'tampered',
                encoding='utf-8',
            )
          else:
            target.unlink()
          with self.assertRaises(ValueError):
            self._run_offline(
                offline_dir, root / 'out', manifest_path
            )

  def test_offline_resummary_refuses_tampered_frozen_hash(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      offline_dir = _build_passing_batch(root, 'offline')
      protocol = offline_dir / 'protocol.json'
      protocol.write_text(
          protocol.read_text(encoding='utf-8') + '\n', encoding='utf-8'
      )
      manifest_path = self._manifest_for(offline_dir)
      with self.assertRaises(ValueError):
        self._run_offline(offline_dir, root / 'out', manifest_path)

  def test_source_diff_allows_only_approved_tool_changes(self):
    current = probe.source_snapshot_payload()
    experiment = {'files': [dict(e) for e in current['files']]}
    diff = probe._source_diff_vs_experiment(  # pylint: disable=protected-access
        experiment, current
    )
    self.assertEqual(diff, {})
    tool_path = 'examples/story_enactment/adjudication_chain_probe.py'
    for entry in experiment['files']:
      if entry['path'] == tool_path:
        entry['sha256'] = '0' * 64
    diff = probe._source_diff_vs_experiment(  # pylint: disable=protected-access
        experiment, current
    )
    self.assertEqual(diff[tool_path]['experiment_sha256'], '0' * 64)
    self.assertEqual(
        diff[tool_path]['current_sha256'],
        probe.sha256_file(probe._REPO_ROOT / tool_path),  # pylint: disable=protected-access
    )
    production_path = 'concordia/document/document.py'
    for entry in experiment['files']:
      if entry['path'] == production_path:
        entry['sha256'] = '0' * 64
    with self.assertRaises(ValueError):
      probe._source_diff_vs_experiment(  # pylint: disable=protected-access
          experiment, current
      )
    # Reset all previous hash mutations so the next rejection proves
    # file-list validation independently of production-source drift.
    experiment = {'files': [dict(e) for e in current['files']]}
    experiment['files'].append({'path': 'extra/file.py', 'sha256': '0' * 64})
    with self.assertRaises(ValueError):
      probe._source_diff_vs_experiment(  # pylint: disable=protected-access
          experiment, current
      )

  def test_offline_resummary_records_tool_source_diff(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      offline_dir = _build_passing_batch(root, 'offline')
      snapshot_path = offline_dir / 'source_snapshot.json'
      snapshot = _read_json(snapshot_path)
      tool_path = 'examples/story_enactment/adjudication_chain_probe.py'
      for entry in snapshot['files']:
        if entry['path'] == tool_path:
          entry['sha256'] = '0' * 64
      probe._write_json(snapshot_path, snapshot)  # pylint: disable=protected-access
      report = _read_json(offline_dir / 'report.json')
      report['source_snapshot_sha256'] = probe.sha256_file(  # pylint: disable=protected-access
          snapshot_path
      )
      (offline_dir / 'report.json').write_text(
          json.dumps(report, ensure_ascii=False), encoding='utf-8'
      )
      manifest_path = self._manifest_for(offline_dir)
      self._run_offline(offline_dir, root / 'out', manifest_path)
      out_report = _read_json(root / 'out' / 'report.json')
      diff = out_report['offline_resummarize']['source_diff_vs_experiment']
      self.assertEqual(list(diff), [tool_path])
      self.assertEqual(diff[tool_path]['experiment_sha256'], '0' * 64)
      self.assertEqual(
          diff[tool_path]['current_sha256'],
          probe.sha256_file(probe._REPO_ROOT / tool_path),  # pylint: disable=protected-access
      )
```

- [ ] **Step 2: 运行确认 RED**

Run: `pytest examples/story_enactment/adjudication_chain_probe_test.py::OfflineResummarizeTest -v`
Expected: 全部 **ERROR/FAIL**（`probe._resummarize_offline`/`_input_manifest_payload`/`_source_diff_vs_experiment` 不存在 → AttributeError）。记录 RED 输出。

- [ ] **Step 3: 实现离线入口**

在 `adjudication_chain_probe.py` 中，`_SNAPSHOT_FILES`/`source_snapshot_payload` 附近加常量，`_summarize` 之后加函数（`_verify_frozen` 及严格路径保持原样）：

```python
_README_NAME = 'README.md'

# The offline re-summary may only run with source changes that were
# approved for this closeout: the summary tool and its test. Every
# production file must stay byte-identical to the experiment snapshot.
_RESUMMARIZE_ALLOWED_SOURCE_CHANGES = (
    'examples/story_enactment/adjudication_chain_probe.py',
    'examples/story_enactment/adjudication_chain_probe_test.py',
)


def _input_manifest_payload(batch_dir: pathlib.Path) -> dict[str, Any]:
  """Builds the read-only input manifest of a batch (README excluded)."""
  files = sorted(
      (str(path.relative_to(batch_dir)), sha256_file(path))
      for path in batch_dir.rglob('*')
      if path.is_file() and path.name != _README_NAME
  )
  return {'files': [{'path': p, 'sha256': d} for p, d in files]}


def _verify_input_manifest(
    batch_dir: pathlib.Path,
    manifest_path: pathlib.Path,
) -> str:
  """Re-verifies the batch inputs against the pre-work manifest."""
  manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
  recorded = manifest['files']
  current = _input_manifest_payload(batch_dir)['files']
  if recorded != current:
    recorded_by_path = {e['path']: e['sha256'] for e in recorded}
    current_by_path = {e['path']: e['sha256'] for e in current}
    missing = sorted(set(recorded_by_path) - set(current_by_path))
    extra = sorted(set(current_by_path) - set(recorded_by_path))
    changed = sorted(
        p
        for p in set(recorded_by_path) & set(current_by_path)
        if recorded_by_path[p] != current_by_path[p]
    )
    raise ValueError(
        'batch inputs differ from the pre-work manifest:'
        f' missing={missing} extra={extra} changed={changed}'
    )
  return sha256_file(manifest_path)


def _source_diff_vs_experiment(
    experiment_snapshot: Mapping[str, Any],
    current_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
  """Diffs snapshots, allowing only the approved summary-tool changes."""
  old = {e['path']: e['sha256'] for e in experiment_snapshot['files']}
  new = {e['path']: e['sha256'] for e in current_snapshot['files']}
  if set(old) != set(new):
    raise ValueError(
        'snapshot file list changed vs experiment:'
        f' {sorted(set(old) ^ set(new))}'
    )
  diff = {}
  for path in sorted(old):
    if old[path] == new[path]:
      continue
    if path not in _RESUMMARIZE_ALLOWED_SOURCE_CHANGES:
      raise ValueError(f'non-tool source changed vs experiment: {path}')
    diff[path] = {'experiment_sha256': old[path], 'current_sha256': new[path]}
  return diff


def _resummarize_offline(
    batch_dir: pathlib.Path,
    output_dir: pathlib.Path,
    manifest_path: pathlib.Path,
) -> None:
  """Recomputes the summary from the frozen batch without any model call.

  This is the explicit offline re-summary entry for batches summarized
  by an older tool version: it reuses the same fixed summary logic, never
  reads credentials or constructs clients, and writes a provenance-rich
  corrected report into a new output directory, leaving the original
  report.json untouched.
  """
  batch_path = batch_dir.resolve()
  output_path = output_dir.resolve()
  if output_path.is_relative_to(batch_path):
    raise ValueError(
        f'output directory must be outside the source batch: {output_dir}'
    )
  if output_dir.exists():
    raise ValueError(f'refusing to overwrite existing directory: {output_dir}')
  manifest_sha256 = _verify_input_manifest(batch_dir, manifest_path)
  report = _load_report(batch_dir)
  original_report_sha256 = sha256_file(batch_dir / _REPORT_NAME)
  experiment_snapshot = json.loads(
      (batch_dir / _SNAPSHOT_NAME).read_text(encoding='utf-8')
  )
  current_snapshot = source_snapshot_payload()
  source_diff = _source_diff_vs_experiment(experiment_snapshot, current_snapshot)
  summary = _compute_summary(batch_dir, report)
  new_report = dict(report)
  new_report['summary'] = summary
  new_report['overall_pass'] = summary['overall_pass']
  new_report['offline_resummarize'] = {
      'source_batch_dir': str(batch_dir),
      'original_report_sha256': original_report_sha256,
      'input_manifest_path': str(manifest_path),
      'input_manifest_sha256': manifest_sha256,
      'summarizer_source_snapshot': current_snapshot,
      'source_diff_vs_experiment': source_diff,
      'resummarized_at_utc': utc_now(),
      'new_real_api_calls': 0,
      'note': (
          'Offline re-summary by the fixed summary tool. The original'
          ' report.json in the source batch keeps the experiment-time'
          ' summary; no model call, credential lookup, or stage'
          ' execution happened here.'
      ),
  }
  output_dir.mkdir(parents=True)
  _write_json(output_dir / _REPORT_NAME, new_report)
  print(
      f'offline resummary written: {output_dir / _REPORT_NAME}'
      f' overall_pass={summary["overall_pass"]} new_real_api_calls=0',
      flush=True,
  )
```

CLI 接线：`_STAGES` 增加 `_STAGE_RESUMMARIZE_OFFLINE = 'resummarize_offline'`（常量定义放 `_STAGE_SUMMARIZE` 之后）；新增两个 flag：

```python
flags.DEFINE_string(
    'output_dir',
    None,
    'Output directory for --stage=resummarize_offline; it must not exist.',
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'input_manifest',
    None,
    'Pre-work input manifest for --stage=resummarize_offline (JSON file'
    ' covering the read-only batch inputs).',
    flag_values=FLAGS,
)
```

`main` 分派（`_STAGE_SUMMARIZE` 分支后）：

```python
  elif stage == _STAGE_RESUMMARIZE_OFFLINE:
    if not FLAGS.output_dir or not FLAGS.input_manifest:
      raise app.UsageError(
          f'--stage={stage} requires --output_dir and --input_manifest'
      )
    _resummarize_offline(
        batch_dir,
        pathlib.Path(FLAGS.output_dir),
        pathlib.Path(FLAGS.input_manifest),
    )
```

- [ ] **Step 4: 运行确认 GREEN**

```bash
pytest examples/story_enactment/adjudication_chain_probe_test.py -v
pyink examples/story_enactment/adjudication_chain_probe.py examples/story_enactment/adjudication_chain_probe_test.py
isort examples/story_enactment/adjudication_chain_probe.py examples/story_enactment/adjudication_chain_probe_test.py
pylint --errors-only examples/story_enactment/adjudication_chain_probe.py examples/story_enactment/adjudication_chain_probe_test.py
```

Expected: 全绿；pylint 退出码 0。

- [ ] **Step 5: 确认严格路径未被弱化**

```bash
pytest examples/story_enactment/adjudication_chain_probe_test.py::FreezeAndRunDisciplineTest -v
```

Expected: PASS（`_verify_frozen` 仍由 `_summarize`/run 阶段调用；离线入口不含任何"忽略源码差异"的通用开关）。

---

### Task 5: examples README 增补离线重汇总用法

**Files:**
- Modify: `examples/story_enactment/README.md:476-507`（Adjudication-Chain Probe 节）

**Interfaces:** 无代码接口；文档须与 Task 4 的实际 CLI（`--stage=resummarize_offline --batch_dir --output_dir --input_manifest`）一致。

- [ ] **Step 1: 追加段落与命令**

在 `summarize` 阶段说明句（"…plus all three stage gates). Tests in …run fully offline." 之后、命令代码块之前）插入一段：

```markdown
The `summarize` stage is strict: it re-verifies every snapshotted source
hash, so it cannot process a batch after the summary tool itself changes.
For that case `resummarize_offline` re-runs the same fixed summary logic
over a frozen batch without credentials, clients, or stage execution: it
verifies a pre-work input-file manifest, allows source diffs only for
the approved tool files (production files must stay byte-identical to
the batch's experiment snapshot), and writes a provenance-annotated
corrected report (`offline_resummarize` block with
`new_real_api_calls=0`) into a new output directory. The original
`report.json` always keeps the experiment-time summary; the two report
versions must not be conflated. The resolved output path must be
outside the source batch, including when a symlink points into it;
invalid output paths are rejected before any files are written.
```

并在该节命令代码块末尾追加：

```
python -m examples.story_enactment.adjudication_chain_probe \
    --stage=resummarize_offline --batch_dir=<已冻结批次> \
    --output_dir=<新目录，不得已存在> \
    --input_manifest=<开工前保存的批次输入哈希清单>
```

- [ ] **Step 2: 校验文档与实现一致**

Run: `grep -n "resummarize_offline" examples/story_enactment/README.md examples/story_enactment/adjudication_chain_probe.py`
Expected: 两文件中的 stage 名、flag 名完全一致。

---

### Task 6: 真实批次离线重汇总（0 次真实调用）

**Files:**
- Create: `run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z_resummarized_<UTC>/report.json`（实际时间戳运行时确定）

**Interfaces:**
- Consumes: Task 1 清单 `docs/superpowers/notes/2026-10-19-chain-probe-offline-input-manifest.json`；Task 4 CLI。
- Produces: 新报告（供 Task 7 README 链接）；其 `offline_resummarize` 块为交付的来源记录。

- [ ] **Step 1: 执行离线重汇总**

```bash
source venv/bin/activate
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z_resummarized_${STAMP}"
python -m examples.story_enactment.adjudication_chain_probe \
    --stage=resummarize_offline \
    --batch_dir=run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z \
    --output_dir="$OUT" \
    --input_manifest=docs/superpowers/notes/2026-10-19-chain-probe-offline-input-manifest.json
echo "exit=$? OUT=$OUT" | tee /tmp/opencode/chain-summary-closeout-baseline/resummarize_cmd.txt
```

Expected: 退出码 0；打印 `offline resummary written: ... overall_pass=True new_real_api_calls=0`。把实际命令与退出码记入交付记录。

- [ ] **Step 2: 校验新报告与原批次不变**

```bash
python3 - <<'EOF'
import hashlib, json, pathlib
batch = pathlib.Path(
    'run_results/story_enactment/action_authorization_chain_probe/'
    '20261018T012101Z')
import glob
out_dir = sorted(
    pathlib.Path(p) for p in glob.glob(str(batch) + '_resummarized_*'))[-1]
report = json.loads((out_dir / 'report.json').read_text(encoding='utf-8'))
prov = report['offline_resummarize']
summary = report['summary']
assert report['counts'] == {
    'planned_adjudication_attempts': 24,
    'planned_final_attempts': 24,
    'planned_injection_attempts': 24,
    'real_api_calls': 120,
    'real_api_calls_stage_a': 24,
    'real_api_calls_stage_final': 48,
    'real_api_calls_stage_e': 48,
}, report['counts']
assert prov['new_real_api_calls'] == 0
assert summary['overall_pass'] is True and report['overall_pass'] is True
for tag in ('m1', 'm4'):
  e = summary['stage_e'][tag]
  assert e['human_canonical_nonstory'] == 0, (tag, e)
  assert e['human_label_incomplete'] == 0, (tag, e)
  assert e['pass'] is True, (tag, e)
diff = prov['source_diff_vs_experiment']
expected = {
    'examples/story_enactment/adjudication_chain_probe.py',
    'examples/story_enactment/adjudication_chain_probe_test.py',
}
assert set(diff) == expected, diff
# The original batch report is byte-identical to the manifest record.
manifest = json.load(open(
    'docs/superpowers/notes/2026-10-19-chain-probe-offline-input-manifest.json'))
recorded = {f['path']: f['sha256'] for f in manifest['files']}
current = {
    str(p.relative_to(batch)).replace(pathlib.os.sep, '/'):
        hashlib.sha256(p.read_bytes()).hexdigest()
    for p in batch.rglob('*')
    if p.is_file() and p.name != 'README.md'
}
assert recorded == current, 'original batch changed during the work!'
assert prov['original_report_sha256'] == recorded['report.json']
print('offline resummary verified:', out_dir)
print('source diff files:', sorted(diff))
EOF
```

Expected: `offline resummary verified: <OUT>`；源码差异恰为两个工具文件；原批次与开工清单逐字节一致。任何断言失败 → 停止并报告，不得修改数据迁就。

---

### Task 7: 目标批次 README 指定勘误与收尾说明

**Files:**
- Modify: `run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z/README.md`（仅两处计数 + 追加收尾节；不动表格中的 `12/12`）

**Interfaces:**
- Consumes: Task 6 的实际输出目录 `<OUT>`（相对链接 `../<OUT 目录名>/report.json`）与验证结果。

- [ ] **Step 1: 勘误一（Gemini 返回标识计数）**

§3 中：

```text
- 请求名 vs provider 返回名：m1 请求 `gemini-3.8-flash-high` →
  返回 `gemini-3.8-flash-n`（本批 72/72 一致）；m4
  `gpt-6.1-sol` → `gpt-6.1-sol`。差异未查明，按协议标为**路由
  身份局限**，不作为通过条件。
```

`72/72` 改为 `60/60`，并注明口径：

```text
- 请求名 vs provider 返回名：m1 请求 `gemini-3.8-flash-high` →
  返回 `gemini-3.8-flash-n`（m1 的 A 12 + FINAL 24 + E 24 共 60 个
  真实响应，60/60 一致）；m4
  `gpt-6.1-sol` → `gpt-6.1-sol`。差异未查明，按协议标为**路由
  身份局限**，不作为通过条件（本句仅勘误计数口径，不表示路由
  身份已查明）。
```

- [ ] **Step 2: 勘误二（m1 省略越权被拦截计数）**

§2 生成器质量观察第 1 条中 `被复核按规则 7（no-repair）12/12 拦截` 改为：

```text
      被复核按规则 7（no-repair）6/6 拦截（E02/E04 各 3 次；两模型
      负例合计 12/12）；
```

**不得**改动 §1/§2 表格中各模型 E 阶段正确的 `12/12`（如 "PASS 12/12 correct"）。

- [ ] **Step 3: 追加收尾节（文末）**

```markdown
## 8. 汇总工具修复与离线重汇总收尾（2026-10-19）

发现并修复 E 阶段汇总漏验：原汇总只统计机器已知片段扫描
（`canonical_known_nonstory`，仅 E03），未读取人工标签
`canonical_nonstory_text`。修复后，任一 E 人工标签标注正式事件
含非故事性文字即判定对应模型、E 阶段与整批失败（新增计数
`human_canonical_nonstory`；该字段缺失/非布尔也阻止通过，计为
`human_label_incomplete`）。本批 24 条 E 标签均明确为 false，故
结论不变——以下由修复后工具离线重新计算，未重新裁决、生成、
复核或标注，新增真实模型调用 0（总调用仍为 120：A 24 + FINAL
48 + E 48）：

- 离线重汇总报告：`../20261018T012101Z_resummarized_<STAMP>/report.json`
  （`offline_resummarize` 块记录原批次路径、原报告哈希、输入清单
  哈希、汇总工具源码快照与差异清单、重汇总时间、
  `new_real_api_calls=0`）。
- 结果：`overall_pass=true` 维持；两模型 E 阶段
  `human_canonical_nonstory=0`、`human_label_incomplete=0`。
- 本目录 `report.json` 保持**实验时原版汇总**（其工具版本见
  `source_snapshot.json`）；新报告出自修复后的汇总工具版本
  （源码差异仅 `adjudication_chain_probe.py` 与其测试），两者
  不可混同。开工前输入哈希清单见
  `docs/superpowers/notes/2026-10-19-chain-probe-offline-input-manifest.json`，
  重汇总前后原批次输入逐字节一致。

实际命令（venv 激活，仓库根目录）：

```
python -m examples.story_enactment.adjudication_chain_probe \
    --stage=resummarize_offline \
    --batch_dir=run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z \
    --output_dir=run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z_resummarized_<STAMP> \
    --input_manifest=docs/superpowers/notes/2026-10-19-chain-probe-offline-input-manifest.json
```
```

`<STAMP>` 替换为 Task 6 的实际时间戳；§3 路由身份局限的既有披露保持不变。

- [ ] **Step 4: 核对勘误完整性**

Run: `grep -n "72/72\|12/12\|60/60\|6/6" run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z/README.md`
Expected: 不再有 `72/72`；正文观察项为 `6/6`（含 12/12 合计注释）；表格中各模型 E 阶段 `12/12` 保留；`60/60` 出现一次。

---

### Task 8: 全量验证与交付记录

**Files:**
- Create: `docs/superpowers/notes/2026-10-19-story-enactment-chain-summary-closeout.md`（交付记录）

**Interfaces:**
- Consumes: 前述所有任务的命令、输出与产物。

- [ ] **Step 1: 运行 AGENTS.md 适用检查（记录每条命令与退出码）**

```bash
source venv/bin/activate
pytest examples/story_enactment/adjudication_chain_probe_test.py
pytest examples
pytest concordia
pylint --errors-only concordia
pyrefly check concordia
```

Expected: 两条 pytest 全绿（examples 通过数较基线 475 增加，记录实际数）；`pyrefly check concordia` 退出码 1 且仅含此前已知 4 项依赖类型错误（vllm/FastMCP）——与基线区分记录，不得宣称"全部静态检查通过"。

- [ ] **Step 2: 改动文件的适用静态检查**

```bash
pyink --check examples/story_enactment/adjudication_chain_probe.py examples/story_enactment/adjudication_chain_probe_test.py
isort --check-only examples/story_enactment/adjudication_chain_probe.py examples/story_enactment/adjudication_chain_probe_test.py
pylint --errors-only examples/story_enactment/adjudication_chain_probe.py examples/story_enactment/adjudication_chain_probe_test.py
pyrefly check examples/story_enactment/adjudication_chain_probe.py examples/story_enactment/adjudication_chain_probe_test.py
```

Expected: 前三条退出码 0；pyrefly 记录实际输出（如有错误，与 `pyrefly check concordia` 基线分开说明）。

- [ ] **Step 3: 确认无 git 操作、无源码快照污染**

```bash
git status --porcelain > /tmp/opencode/chain-summary-closeout-baseline/git_status_final.txt
python3 - <<'EOF'
import hashlib, json, pathlib
snap = json.load(open(
    'run_results/story_enactment/action_authorization_chain_probe/'
    '20261018T012101Z/source_snapshot.json'))
allow = {
    'examples/story_enactment/adjudication_chain_probe.py',
    'examples/story_enactment/adjudication_chain_probe_test.py',
}
diffs = []
for entry in snap['files']:
    path = pathlib.Path(entry['path'])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != entry['sha256'] and entry['path'] not in allow:
        diffs.append(entry['path'])
assert not diffs, diffs
print('production snapshot integrity OK')
EOF
```

Expected: 快照内生产/非白名单文件仍与实验快照一致；本任务全程未执行 commit/stash/clean/checkout。

- [ ] **Step 4: 撰写交付记录**

按仓库既有交付记录风格（参考 `docs/superpowers/notes/2026-10-17-story-enactment-run4-review-and-progress.md`）写 `2026-10-19-story-enactment-chain-summary-closeout.md`，必须包含（spec §8）：

1. 实施计划链接、改动文件清单（两个源码/测试文件、两个 README、清单、新报告目录、交付记录自身）。
2. 测试结果：关键反例的 RED 原始输出与 GREEN 证据；`pytest examples` / `pytest concordia` 通过数；pylint/pyrefly/pyink/isort 命令、退出码与错误详情（pyrefly 4 项已知依赖错误与基线区分说明）。
3. 实际离线重汇总命令（含实际时间戳）与新报告路径。
4. 关键反例说明：临时数据中仅将 `E03__m4__r1` 人工标签改为 true（机器扫描仍 false）→ 批次 PASS 变 FAIL；恢复 false 后重新 PASS（引用 Task 3 测试名）。
5. 原始输入前后哈希一致的证明（Task 6 Step 2 的清单复核输出）与新工具源码差异清单（两个文件 old→new sha256）。
6. 新报告来源记录摘要：`overall_pass=true`、两模型 `human_canonical_nonstory=0`、调用总数 120、`new_real_api_calls=0`。
7. README 两处计数勘误说明与新报告相对链接。
8. 声明：新增真实模型调用 0；未重新裁决/生成/复核/标注；未启动四组完整故事运行；本轮到此停在工程审阅点。

- [ ] **Step 5: 停在工程审阅点**

输出上述交付摘要给用户审阅。不启动 Gemini/Sol 四组完整故事运行；不把本次离线汇总修复描述为新增真实模型验证；无任何 git commit。

---

## Self-Review 记录

- **Spec 覆盖**：§4.1 计数与五级失败链 → Task 3 Step 2/4/6；§4.2 缺失/null/非布尔 → Task 3 Step 6 第二个测试（只校验该字段，未扩大 schema）；§5.1–5.6 → Task 3 Step 2/6、Task 4 Step 1（spy 阻断工厂与阶段路径，非仅查计数）；§6.1 严格路径不动 → Task 4 Step 5；§6.2 各项契约 → Task 4 Step 3（清单双向核对、原报告哈希、工具快照+差异白名单、输出目录不覆盖、`new_real_api_calls=0`、保留原 report）；§6.2 离线恶意输入测试 → Task 4 Step 1（tamper/missing/白名单外源码变化）；§7 两处勘误 → Task 7 Step 1/2（保留表格 12/12、维持路由局限披露）；§8 命令与交付 → Task 8。无缺口。
- **占位符扫描**：Task 3 Step 1 的 `_write_*` 六个函数以"逐字搬移"引用了精确行号（`adjudication_chain_probe_test.py:507-614`，内容已在规划时读取核对），避免重复抄写 100 行既有代码；其余步骤均含完整代码/命令。`<STAMP>`/`<OUT>` 为运行时值，生成方式已给定为 `date -u +%Y%m%dT%H%M%SZ`。
- **类型/命名一致性**：`human_canonical_nonstory`/`human_label_incomplete` 在 Task 3 定义、Task 6 Step 2 断言一致；`_compute_summary(batch_dir, report)` 在 Task 2 定义、Task 3/4 消费一致；`_input_manifest_payload`/`_verify_input_manifest`/`_source_diff_vs_experiment`/`_resummarize_offline` 的签名在 Task 4 接口块与实现、测试三方一致；清单 JSON 格式在 Task 1 生成与 Task 4 解析间一致（排序、posix 路径、仅 path+sha256 键、排除 README.md）。
- **2026-10-19 审阅回填**：篡改测试先创建模拟响应再固定清单；`_make_frozen_batch` 在入口将字符串/Path 统一为 Path；离线输出在任何写盘前解析实际路径并拒绝原批次及其子目录，测试覆盖符号链接与失败后零新增文件；原批次清单口径修正为 443；文件清单变化测试重置此前的哈希变异，避免场景相互掩盖。上述修正不改变设计、生产代码范围或零真实调用约束。
