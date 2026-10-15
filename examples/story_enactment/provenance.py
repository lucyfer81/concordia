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

Each invocation records its own source snapshot before simulation starts.
Resuming appends a session without replacing earlier snapshots, so a change
of prompts between processes remains visible. This is review provenance,
not a replacement for the story-turn/checkpoint commit boundary.
"""

from collections.abc import Mapping
import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from examples.story_enactment import collector

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_PATHS = (
    'examples/story_enactment/run.py',
    'examples/story_enactment/provenance.py',
    'examples/story_enactment/story_config.py',
    'examples/story_enactment/acceptance.py',
    'examples/story_enactment/testing_stubs.py',
    'concordia/contrib/components/game_master/story_resolution.py',
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
_CREDENTIAL_KEYS = frozenset({
    'api_key',
    'access_token',
    'authorization',
    'password',
    'client_secret',
})


def _without_credentials(value: Any) -> Any:
  """Copies JSON data without credential fields, preserving story secrets."""
  if isinstance(value, Mapping):
    return {
        key: _without_credentials(item)
        for key, item in value.items()
        if key.lower() not in _CREDENTIAL_KEYS
    }
  if isinstance(value, (list, tuple)):
    return [_without_credentials(item) for item in value]
  return value


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


def archive_invocation(
    state_dir: str,
    *,
    run_config: Mapping[str, Any],
    committed_turn_id: int,
    resume: bool,
) -> None:
  """Atomically records a run invocation before its first model request.

  Args:
    state_dir: Existing directory of the run's checkpoint and exports.
    run_config: Effective run configuration, including model identity.
    committed_turn_id: Last committed turn at invocation start.
    resume: Whether this process resumes an existing checkpoint.

  Raises:
    ValueError: If a first run would overwrite a manifest, or an existing
      manifest has an unsupported schema.
    OSError: If source files cannot be read or the archive cannot be saved.
  """
  path = Path(state_dir) / 'run_manifest.json'
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
  manifest['invocations'].append({
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
