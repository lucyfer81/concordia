# Copyright 2026 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Archives configuration and prompt sources for human story reviews.

Each invocation records its own source snapshot before simulation starts
and appends itself to ``run_manifest.json`` under a unique
``invocation_id``, so resuming never replaces earlier snapshots and a
change of prompts between processes remains visible. Each invocation
also publishes exactly one immutable
``process_summaries/<invocation_id>.json`` at exit — success, budget
stop, error and caught interruption alike — holding that process's own
timing, usage deltas and private failure diagnostics. The summaries
double as a usage record: ``restore_recorded_usage`` returns the true
cumulative counters even when the usage sidecar's write failed.
Archived data is scrubbed of credential fields and of the runtime
credential values themselves (e.g. an exception quoting the API key).

This is review provenance, not a replacement for the
story-turn/checkpoint commit boundary: ``SIGKILL`` or a power loss
cannot guarantee the finalization code runs at all, so such exits leave
whatever the last committed checkpoint holds and no fabricated summary.
"""

from collections.abc import Mapping
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid

from examples.story_enactment import collector

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_PATHS = (
    'examples/story_enactment/run.py',
    'examples/story_enactment/provenance.py',
    'examples/story_enactment/story_config.py',
    'examples/story_enactment/acceptance.py',
    'examples/story_enactment/testing_stubs.py',
    'concordia/contrib/components/game_master/story_resolution.py',
    'concordia/contrib/components/game_master/story_action.py',
    'concordia/contrib/components/game_master/story_action_adjudication.py',
    'concordia/contrib/components/game_master/repetition_push.py',
    'concordia/contrib/components/game_master/story_terminator.py',
    'concordia/contrib/components/game_master/drama_director.py',
    'concordia/contrib/components/game_master/director_note.py',
    'concordia/contrib/components/game_master/observation_queue.py',
    'concordia/contrib/components/agent/relationships.py',
    'concordia/contrib/components/agent/emotional_stance.py',
    'concordia/contrib/prefabs/game_master/story_dramaturgic.py',
    'concordia/contrib/prefabs/entity/story_character.py',
    'concordia/prefabs/entity/basic.py',
    'concordia/components/game_master/event_resolution.py',
    'concordia/document/interactive_document.py',
)
# Credential dictionary field names, dropped wherever they occur in
# archived data; the runtime VALUES under these keys are additionally
# redacted from archived strings (see ``scrub_credentials``).
CREDENTIAL_KEYS = frozenset({
    'api_key',
    'access_token',
    'authorization',
    'password',
    'client_secret',
})
# Directory holding one JSON file per invocation.
PROCESS_SUMMARIES_DIRNAME = 'process_summaries'
_MANIFEST_FILENAME = 'run_manifest.json'
# Replacement for runtime secret texts found inside archived strings
# (e.g. a provider exception quoting the API key back).
_REDACTED_TEXT = '[REDACTED]'


def _without_credentials(value: Any) -> Any:
  """Copies JSON data without credential fields, preserving story secrets."""
  if isinstance(value, Mapping):
    return {
        key: _without_credentials(item)
        for key, item in value.items()
        if key.lower() not in CREDENTIAL_KEYS
    }
  if isinstance(value, (list, tuple)):
    return [_without_credentials(item) for item in value]
  return value


def _redact_secret_texts(value: Any, secret_texts: tuple[str, ...]) -> Any:
  """Replaces every occurrence of the given secret texts in strings."""
  if not secret_texts:
    return value
  if isinstance(value, str):
    for secret in secret_texts:
      value = value.replace(secret, _REDACTED_TEXT)
    return value
  if isinstance(value, Mapping):
    return {
        key: _redact_secret_texts(item, secret_texts)
        for key, item in value.items()
    }
  if isinstance(value, (list, tuple)):
    return [_redact_secret_texts(item, secret_texts) for item in value]
  return value


def scrub_credentials(
    value: Any,
    *,
    secret_texts: tuple[str, ...] = (),
) -> Any:
  """Returns archive-safe data: no credential fields, no secret texts.

  ``_without_credentials`` drops credential dictionary fields, but a
  runtime secret can still reach an archive inside a plain string —
  most plausibly a provider exception message quoting the API key
  back. ``secret_texts`` holds the actual credential values this
  process knows (from its model configuration); every occurrence is
  replaced with a placeholder before the data is written.

  Args:
    value: the JSON-safe data to scrub.
    secret_texts: runtime credential values to redact from strings.

  Returns:
    The scrubbed copy of the data.
  """
  return _redact_secret_texts(_without_credentials(value), secret_texts)


def _source_snapshot() -> dict[str, dict[str, str]]:
  """Reads the source files defining the prompts used by this invocation."""
  snapshot = {}
  for relative_path in _SOURCE_PATHS:
    source = (_SOURCE_ROOT / relative_path).read_bytes()
    snapshot[relative_path] = {
        'sha256': hashlib.sha256(source).hexdigest(),
        'text': source.decode('utf-8', errors='replace'),
    }
  return snapshot


def _new_invocation_id() -> str:
  """Returns an invocation id unique across resumes.

  A microsecond UTC timestamp keeps ids human-sortable while a random
  suffix rules out collisions between processes started within the same
  microsecond; a second-resolution timestamp alone would not suffice.
  """
  now = datetime.datetime.now(datetime.timezone.utc)
  return f'{now:%Y%m%dT%H%M%S%f}Z-{uuid.uuid4().hex[:12]}'


def summary_relative_path(invocation_id: str) -> str:
  """Returns the manifest-relative path of one invocation's summary."""
  return f'{PROCESS_SUMMARIES_DIRNAME}/{invocation_id}.json'


def archive_invocation(
    state_dir: str,
    *,
    run_config: Mapping[str, Any],
    committed_turn_id: int,
    resume: bool,
) -> str:
  """Atomically records a run invocation before its first model request.

  The new manifest entry carries its own ``invocation_id`` and the
  relative path of the summary file the invocation will publish at
  exit. Entries written before invocation ids existed are kept verbatim;
  their missing ids are never fabricated.

  Args:
    state_dir: Existing directory of the run's checkpoint and exports.
    run_config: Effective run configuration, including model identity.
    committed_turn_id: Last committed turn at invocation start.
    resume: Whether this process resumes an existing checkpoint.

  Returns:
    The unique invocation id of the archived invocation.

  Raises:
    ValueError: If a first run would overwrite a manifest, or an existing
      manifest has an unsupported schema.
    OSError: If source files cannot be read or the archive cannot be saved.
  """
  path = Path(state_dir) / _MANIFEST_FILENAME
  manifest: dict[str, Any]
  if path.exists():
    if not resume:
      raise ValueError(f'{path} already exists; use a new run directory.')
    manifest = json.loads(path.read_text(encoding='utf-8', errors='replace'))
    if manifest.get('schema_version') != 1:
      raise ValueError('unsupported run manifest schema version.')
  else:
    manifest = {
        'schema_version': 1,
        # An older checkpoint cannot reconstruct its original sources.
        'history_complete': not resume,
        'invocations': [],
    }
  snapshot = _source_snapshot()
  fingerprints = {key: value['sha256'] for key, value in snapshot.items()}
  source_version = hashlib.sha256(
      json.dumps(fingerprints, sort_keys=True).encode('utf-8')
  ).hexdigest()
  invocation_id = _new_invocation_id()
  manifest['invocations'].append({
      'invocation_id': invocation_id,
      'summary_path': summary_relative_path(invocation_id),
      'started_at_utc': (
          datetime.datetime.now(datetime.timezone.utc).isoformat()
      ),
      'resume': resume,
      'committed_turn_id_at_start': committed_turn_id,
      'run_config': _without_credentials(run_config),
      'source_version': source_version,
      'source_snapshot': snapshot,
  })
  collector._atomic_write_text(  # pylint: disable=protected-access
      str(path), json.dumps(manifest, ensure_ascii=False, indent=2)
  )
  return invocation_id


def read_invocation(state_dir: str, invocation_id: str) -> dict[str, Any]:
  """Returns the manifest entry of one invocation.

  Args:
    state_dir: directory holding the run manifest.
    invocation_id: the id returned by ``archive_invocation``.

  Returns:
    The manifest entry dictionary.

  Raises:
    OSError: If the manifest cannot be read.
    ValueError: If no entry carries the invocation id, e.g. a legacy
      entry predating invocation ids.
  """
  path = Path(state_dir) / _MANIFEST_FILENAME
  manifest = json.loads(path.read_text(encoding='utf-8', errors='replace'))
  for entry in manifest.get('invocations', ()):
    if entry.get('invocation_id') == invocation_id:
      return dict(entry)
  raise ValueError(f'no manifest invocation {invocation_id!r} in {path}.')


def write_process_summary(
    state_dir: str,
    *,
    invocation_id: str,
    summary: Mapping[str, Any],
) -> None:
  """Publishes one invocation's summary file exactly once.

  The payload is written in full to a temporary file first and then
  linked into place, so the destination appears atomically and an
  existing summary is never overwritten — a second write with the same
  id is rejected with the original bytes untouched. Credential fields
  are scrubbed; story secrets and private traces stay, because the file
  is review-private by construction.

  Args:
    state_dir: the run's state directory.
    invocation_id: the invocation the summary belongs to.
    summary: the JSON-safe summary mapping (schema version 1).

  Raises:
    ValueError: if the invocation id is malformed or a summary already
      exists for it.
    OSError: if the summary cannot be written.
  """
  if not invocation_id or Path(invocation_id).name != invocation_id:
    raise ValueError(f'invalid invocation id {invocation_id!r}.')
  directory = Path(state_dir) / PROCESS_SUMMARIES_DIRNAME
  directory.mkdir(parents=True, exist_ok=True)
  path = directory / f'{invocation_id}.json'
  payload = json.dumps(
      _without_credentials(summary), ensure_ascii=False, indent=2
  )
  fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.summary-')
  try:
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
      handle.write(payload)
      handle.flush()
      os.fsync(handle.fileno())
    try:
      # A hard link publishes the fully written file atomically and
      # fails if the destination exists, unlike an os.replace publish.
      os.link(tmp_path, path)
    except FileExistsError as e:
      raise ValueError(
          f'{path} already holds a summary; refusing to overwrite it.'
      ) from e
  finally:
    with contextlib.suppress(OSError):
      os.unlink(tmp_path)


def restore_recorded_usage(
    state_dir: str,
    *,
    attempt_limit: int,
) -> dict[str, int]:
  """Returns the highest usage recorded by any archived process summary.

  A summary survives even when the usage sidecar's write failed, so its
  ``used_model_attempts_at_end`` / ``used_api_retries_at_end`` are a
  budget-relevant record of their own: a resume restores from the maxima
  across all published summaries. Every published file is validated
  strictly — one that cannot be parsed or holds out-of-range counters
  leaves the true consumption undeterminable, so the resume is refused
  instead of silently falling back to a stale checkpoint count. The same
  refusal applies to an invocation that declared a summary in the
  manifest but never published it: when both the usage sidecar and the
  summary failed to write (or a kill skipped the finalization entirely),
  the missing records are not evidence of zero consumption. Legacy
  entries that predate summary declarations demand nothing.

  Args:
    state_dir: the run's state directory.
    attempt_limit: the run's original total attempt limit, bounding the
      recorded counters.

  Returns:
    ``{'used_model_attempts': int, 'used_api_retries': int}`` — the
    maxima across summaries, zeros when none exist yet.

  Raises:
    ValueError: if any published summary is unreadable or records an
      invalid cumulative usage, or an archived invocation's declared
      summary is missing, or the manifest is unreadable or of an
      unknown schema.
    OSError: if the summaries directory cannot be listed.
  """
  _require_declared_summaries(state_dir)
  directory = Path(state_dir) / PROCESS_SUMMARIES_DIRNAME
  if not directory.is_dir():
    return {'used_model_attempts': 0, 'used_api_retries': 0}
  used = 0
  retries = 0
  for path in sorted(directory.iterdir()):
    if path.name.startswith('.'):
      # Unpublished temporary files are not summaries.
      continue
    try:
      summary = json.loads(path.read_text(encoding='utf-8', errors='replace'))
    except json.JSONDecodeError as e:
      raise ValueError(f'corrupt process summary {path!r}: {e}') from e
    attempts_end = summary.get('used_model_attempts_at_end')
    retries_end = summary.get('used_api_retries_at_end')
    if (
        summary.get('schema_version') != 1
        or not isinstance(attempts_end, int)
        or isinstance(attempts_end, bool)
        or not 0 <= attempts_end <= attempt_limit
    ):
      raise ValueError(
          f'process summary {path!r} does not record a valid cumulative usage.'
      )
    if (
        not isinstance(retries_end, int)
        or isinstance(retries_end, bool)
        or not 0 <= retries_end <= attempts_end
    ):
      raise ValueError(
          f'process summary {path!r} does not record a valid cumulative'
          ' API retry count.'
      )
    used = max(used, attempts_end)
    retries = max(retries, retries_end)
  return {'used_model_attempts': used, 'used_api_retries': retries}


def _require_declared_summaries(state_dir: str) -> None:
  """Refuses an incomplete summary history recorded by the manifest.

  Args:
    state_dir: the run's state directory.

  Raises:
    ValueError: if the manifest is unreadable, of an unknown schema, or
      an invocation declared a ``summary_path`` whose file is absent.
  """
  manifest_path = Path(state_dir) / _MANIFEST_FILENAME
  if not manifest_path.exists():
    # A run from before manifests exist: nothing is declared, so there
    # is nothing to require here.
    return
  try:
    manifest = json.loads(
        manifest_path.read_text(encoding='utf-8', errors='replace')
    )
  except json.JSONDecodeError as e:
    raise ValueError(f'corrupt run manifest {manifest_path!r}: {e}') from e
  if manifest.get('schema_version') != 1:
    raise ValueError(
        'unsupported run manifest schema version'
        f' {manifest.get("schema_version")!r}.'
    )
  for entry in manifest.get('invocations', ()):
    declared = entry.get('summary_path')
    if declared is None:
      # Legacy entries predate summary declarations.
      continue
    if not (Path(state_dir) / declared).is_file():
      raise ValueError(
          f'invocation {entry.get("invocation_id")!r} declared the summary'
          f' {declared!r}, but that summary is missing: its true'
          ' cumulative usage cannot be determined, so the resume is'
          ' refused rather than falling back to a stale checkpoint'
          ' count.'
      )
