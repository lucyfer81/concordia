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

"""Entry point of the story enactment example: run, resume, budget.

This module owns the execution boundary of the story enactment example.
A first run builds the run config and the ``Simulation``, injects the
public premise into the game master (which never counts as a story
step), saves the empty story-turn file plus the initial checkpoint
envelope with ``committed_turn_id = 0``, and then plays with the
collector's step callback as the only save boundary. A resumed run
rebuilds a new ``Simulation`` from the config recorded in the envelope,
restores component state with ``load_from_checkpoint``, restores the
budget guard from the envelope's saved attempt count with the ORIGINAL
total limits, trims the story-turn log to the committed boundary, and
continues with ``play(premise='', max_steps=remaining_turns)``.

Budget paths follow spec section 11.3. Exhaustion BEFORE a resolution
commits propagates out of ``play`` and is recorded here as
``budget_limit``: the current step is not saved and the previous full
checkpoint stays in place. Exhaustion AFTER the resolution (the
completion-judgment or tension-scoring position) is converted by the
``DramaDirector`` into a budget-stop flag; the step still commits
exactly once and this module only summarizes. Any other exception is
recorded as ``error``; ``BaseException`` (e.g. a simulated crash) is
never swallowed.

Example:
  python -m examples.story_enactment.run --state_dir /tmp/story-demo \
      --disable_language_model --mode director
"""

from collections.abc import Callable, Mapping, Sequence
import dataclasses
import json
import os
import time
from typing import Any

from absl import app
from absl import flags
from concordia.components.game_master import terminate as terminate_lib
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_terminator
from concordia.contrib.language_models import budget_wrapper
from concordia.contrib.prefabs.entity import story_character
from concordia.contrib.prefabs.game_master import story_dramaturgic
from concordia.contrib.prefabs.game_master import story_scene
from concordia.language_model import language_model
from concordia.language_model import retry_wrapper
from concordia.prefabs.simulation import generic as simulation_lib
from concordia.typing import prefab as prefab_lib
from concordia.typing import scene as scene_lib
import numpy as np

from examples.story_enactment import collector
from examples.story_enactment import renderer
from examples.story_enactment import story_config
from examples.story_enactment import testing_stubs

# Schema version of the run config recorded inside the envelope.
RUN_CONFIG_SCHEMA = 1

# Stage switches of the plan; stage 2 adds the tension_observe and
# director modes, stage 3 adds character_depth.
MODE_BEATS_ONLY = 'beats_only'
MODE_TENSION_OBSERVE = 'tension_observe'
MODE_DIRECTOR = 'director'
MODE_CHARACTER_DEPTH = 'character_depth'
MODES = ('beats_only', 'tension_observe', 'director', 'character_depth')

# Appendix-C character-depth parameters: the emotion palette offered to
# the characters' EmotionalStance and the relationship refresh cadence.
_EMOTION_OPTIONS = ('平静', '焦虑', '愤怒', '悲伤', '希望', '羞愧')
_RELATIONSHIPS_REFRESH_INTERVAL = 3

# Appendix-C default scoring configuration of the drama director.
_DEFAULT_DIRECTOR_PARAMS = {
    'eval_interval': 2,
    'scoring_window': 8,
    'tolerance': 1,
}
# beats_only keeps the exact stage-1 model-call pattern: eval_interval=0
# disables scoring, so no story turn falls on a scoring step.
_BEATS_ONLY_DIRECTOR_PARAMS = {**_DEFAULT_DIRECTOR_PARAMS, 'eval_interval': 0}
# tension_observe records scores only; feedback stays off so deviations
# never generate directives.
_TENSION_OBSERVE_DIRECTOR_PARAMS = {
    **_DEFAULT_DIRECTOR_PARAMS,
    'feedback_enabled': False,
}
# The director mode enables feedback: deviating scores generate
# directives drawn from the story's allowed environment events, with
# the appendix-C cooldown between executions.
_DIRECTOR_MODE_DIRECTOR_PARAMS = {
    **_DEFAULT_DIRECTOR_PARAMS,
    'cooldown_turns': 3,
    'feedback_enabled': True,
}

# Names of the two prefabs inside the run's Config.
_CHARACTER_PREFAB = 'story_character'
_GM_PREFAB = 'story_gm'
_GM_NAME = 'story rules'

# Stub embedder dimension; live runs use a sentence transformer.
_STUB_EMBEDDER_DIM = 16

_FLAGS = flags.FLAGS
flags.DEFINE_string(
    'state_dir', None, 'directory holding the run state and outputs.'
)
flags.DEFINE_bool('resume', False, 'resume the run in --state_dir.')
flags.DEFINE_bool(
    'disable_language_model',
    False,
    'use the deterministic PromptStubModel instead of a live model.',
)
flags.DEFINE_string('mode', MODE_BEATS_ONLY, 'stage switch of the run.')
flags.DEFINE_integer('total_turn_limit', 24, 'total story-turn budget.')
flags.DEFINE_integer(
    'total_model_attempt_limit', 400, 'total model-attempt budget.'
)
flags.DEFINE_string('api_type', None, 'provider key of a live model.')
flags.DEFINE_string('model_name', None, 'model id of a live model.')
flags.DEFINE_string('api_key', None, 'API key of a live model.')


@dataclasses.dataclass(frozen=True)
class RunResult:
  """The record a run returns and writes to summary.json.

  Attributes:
    reason: final termination reason by the priority of spec section
      4.7 (error > beats_completed > beat_timeout > scene_limit >
      step_limit > budget_limit).
    committed_turn_id: highest story turn confirmed by a checkpoint.
    budget_exhausted: whether the model-attempt budget stopped the run.
    budget_error: diagnostic message of the budget exhaustion, if any.
    error_message: message of a non-budget exception, if any.
    attempts_used_this_process: model attempts spent by this process
      only, excluding the count restored from the checkpoint.
    used_model_attempts: total model attempts accounted for by the
      guard, including the restored count.
    wall_time_seconds: wall time of the playing phase.
    step_durations: elapsed seconds between consecutive saved steps.
    token_usage: always None; token counts are unavailable.
  """

  reason: str
  committed_turn_id: int
  budget_exhausted: bool
  budget_error: str | None
  error_message: str | None
  attempts_used_this_process: int
  used_model_attempts: int
  wall_time_seconds: float
  step_durations: list[float]
  token_usage: None


def make_example_beats() -> tuple[drama_director.Beat, ...]:
  """Returns the narrative-conditional beat outline of the example.

  Beat descriptions state narrative conditions (what may happen or
  become available for characters to act on), never predetermined
  character choices, following spec section 4.3.1. The outline loosely
  follows Freytag's pyramid: opening quarrel, rising evidence, climax
  pressure, and resolution direction.

  Returns:
    The four beats of the appendix-C estate-dispute story.
  """
  return (
      drama_director.Beat(
          id='b1',
          description=(
              '宣读遗嘱后的第一轮争执：各方表面立场浮现，可供质疑的疑点'
              '（封印、立遗嘱时间或程序问题）被摆上台面'
          ),
          tension_range=(3, 5),
          done_question='已有可供角色追问的疑点（封印、时间或程序问题）被公开摆到台面了吗？',
          min_turns=3,
          max_turns=10,
      ),
      drama_director.Beat(
          id='b2',
          description=(
              '疑点发酵：出现可供角色追问的新证据或反常迹象（旧信、录音'
              '残片、烧过的纸灰等任一）'
          ),
          tension_range=(5, 7),
          done_question='剧中是否已出现至少一件可供各方追问的新证据或反常迹象？',
          min_turns=3,
          max_turns=10,
      ),
      drama_director.Beat(
          id='b3',
          description='摊牌时刻：关键事实被揭示，有人面临不得不当场回应的压力',
          tension_range=(7, 9),
          done_question='是否已有角色不得不面对被揭示的关键事实并作出回应？',
          min_turns=3,
          max_turns=10,
      ),
      drama_director.Beat(
          id='b4',
          description=(
              '走向结局：争执朝某个允许的结局方向收束（真相大白、秘密'
              '永埋、坦白和解或僵局散场之一）'
          ),
          tension_range=(4, 6),
          done_question='故事的结局走向是否已经清晰并开始收束？',
          min_turns=2,
          max_turns=8,
      ),
  )


def make_public_premise(story: Mapping[str, Any]) -> str:
  """Joins the public conflict start and world background.

  Args:
    story: a story dictionary (as recorded by ``to_dict``) or config.

  Returns:
    The public premise shown to the game master. It never contains
    game-master-only facts or any character's secret.
  """
  return '\n'.join([story['conflict_start'], *story['world_facts']])


def make_character_params(
    character: story_config.CharacterInfo,
) -> dict[str, Any]:
  """Maps one character of the story onto the character prefab params.

  The prefab builds the ``CharacterBrief`` and, when the secret is
  non-empty, the owner-only ``PrivateSecret`` constant itself, so only
  JSON-safe values ever enter the params.

  Args:
    character: the character's configuration.

  Returns:
    The params of the story character prefab instance.
  """
  return {
      'name': character.name,
      'identity': character.identity,
      'goal': character.goal,
      'hard_constraint': character.hard_constraint,
      'secret': character.secret,
      'public_facts': list(character.public_facts),
      'style': character.style,
      'initial_relationship_notes': character.initial_relationship_notes,
      'randomize_choices': False,
  }


def make_gm_params(story: story_config.StoryConfig) -> dict[str, Any]:
  """Maps the story onto the game master prefab params.

  The game master alone conditions its resolutions on the hidden truth
  (``gm_only_facts``) and on every character's private secret, plus the
  catalogue of allowed environment events. These live in the
  resolution-readable world-constraints component only; they never
  enter scene premises or the broadcast queue.

  Args:
    story: the story configuration.

  Returns:
    The params of the story game master prefab, without the scene and
    beat dictionaries, which ``build_simulation`` injects from the
    shared copies of the run config.
  """
  constraints = tuple(f'仅裁决者可知：{fact}' for fact in story.gm_only_facts)
  constraints += tuple(
      f'仅裁决者可知：{character.name}的私隐：{character.secret}'
      for character in story.characters
      if character.secret
  )
  constraints += (
      '环境事件只能从以下选项中选择：'
      + '；'.join(story.allowed_environment_events),
  )
  return {
      'name': _GM_NAME,
      'world_constraints': list(constraints),
      'immutable_facts': list(story.immutable_facts),
  }


def _public_model_identity(
    model_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
  """Returns the JSON-safe identity of a model config, without secrets."""
  config = dict(model_config or {})
  identity = {
      key: config[key]
      for key in ('api_type', 'model_name', 'device', 'prompt_stub')
      if key in config
  }
  identity.setdefault('api_type', 'stub')
  identity.setdefault('model_name', 'prompt_stub')
  return identity


def _merge_resume_model_config(
    persisted: Mapping[str, Any],
    passed: Mapping[str, Any] | None,
) -> dict[str, Any]:
  """Merges this invocation's credentials into the checkpointed identity.

  The checkpointed model identity is authoritative on resume, and it
  never contains secrets, so the API key of a caller-provided config
  merges over the persisted identity instead of being dropped with it.

  Args:
    persisted: the run config's secret-free model identity.
    passed: the model config passed to this invocation, or None.

  Returns:
    The effective model config for building the language model.

  Raises:
    ValueError: if the passed config's identity contradicts the
      checkpointed one.
  """
  effective = dict(persisted)
  if passed is None:
    return effective
  passed_identity = _public_model_identity(passed)
  if passed_identity != dict(persisted):
    raise ValueError(
        'model_config mismatch: checkpoint holds'
        f' {dict(persisted)!r}, got {passed_identity!r}.'
    )
  if 'api_key' in passed:
    effective['api_key'] = passed['api_key']
  return effective


def make_run_config(
    story: story_config.StoryConfig,
    *,
    total_turn_limit: int = 24,
    total_model_attempt_limit: int = 400,
    mode: str = MODE_BEATS_ONLY,
    model_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """Builds the JSON-safe run config of one story enactment.

  Args:
    story: the story to enact.
    total_turn_limit: original total story-turn budget.
    total_model_attempt_limit: original total model-attempt budget.
    mode: stage switch; one of ``MODES``.
    model_config: identity of the model choice; the API key is dropped
      before the config is recorded.

  Returns:
      The run-config dictionary with the schema version, the full story
      data, scene and beat dictionaries, character and game-master prefab
      params, both total limits, the mode, the director parameters
      (scoring configuration) and the model identity.

  Raises:
      ValueError: if the mode is unknown or the story is invalid.
  """
  if mode not in MODES:
    raise ValueError(f'unknown mode {mode!r}; expected one of {MODES}.')
  problems = story_config.validate(story)
  if problems:
    raise ValueError('invalid story configuration: ' + '; '.join(problems))
  if mode == MODE_BEATS_ONLY:
    director_params = dict(_BEATS_ONLY_DIRECTOR_PARAMS)
  elif mode == MODE_TENSION_OBSERVE:
    director_params = dict(_TENSION_OBSERVE_DIRECTOR_PARAMS)
  else:
    director_params = {
        **_DIRECTOR_MODE_DIRECTOR_PARAMS,
        'allowed_environment_events': list(story.allowed_environment_events),
        'immutable_facts': list(story.immutable_facts),
    }
  premise_lines = (story.conflict_start, *story.world_facts)
  scene = scene_lib.SceneSpec(
      scene_type=scene_lib.SceneTypeSpec(
          name='故事主线',
          action_spec=story_config.make_speech_action_spec(),
      ),
      participants=tuple(character.name for character in story.characters),
      num_rounds=total_turn_limit,
      premise={character.name: premise_lines for character in story.characters},
  )
  entity_params = []
  for character in story.characters:
    params = make_character_params(character)
    if mode == MODE_CHARACTER_DEPTH:
      # Character depth layers emotion and relationship perception on
      # every character; each character perceives exactly the other
      # public participants (appendix C).
      params['emotion_options'] = list(_EMOTION_OPTIONS)
      params['relationships_refresh_interval'] = _RELATIONSHIPS_REFRESH_INTERVAL
      params['other_player_names'] = [
          other.name
          for other in story.characters
          if other.name != character.name
      ]
    entity_params.append(params)
  return {
      'schema_version': RUN_CONFIG_SCHEMA,
      'story': story.to_dict(),
      'scene_dicts': [story_scene.scene_to_dict(scene)],
      'beat_dicts': [beat.to_dict() for beat in make_example_beats()],
      'entity_params': entity_params,
      'gm_params': make_gm_params(story),
      'total_turn_limit': total_turn_limit,
      'total_model_attempt_limit': total_model_attempt_limit,
      'mode': mode,
      'director_params': director_params,
      'model_config': _public_model_identity(model_config),
  }


def _uses_stub_model(
    model_config: Mapping[str, Any] | None,
    *,
    disable_language_model: bool,
) -> bool:
  """Returns whether the run uses the deterministic prompt stub."""
  if disable_language_model:
    return True
  api_type = (model_config or {}).get('api_type')
  return api_type in (None, 'stub')


def _provider_retryable_exceptions(
    api_type: str,
) -> tuple[type[Exception], ...]:
  """Returns the provider-specific transient API exception whitelist.

  Built-in ``ConnectionError``/``TimeoutError`` are deliberately not
  used as a universal whitelist: each provider's own transient error
  types are imported lazily, and unknown providers get no retries.

  Args:
    api_type: the provider key of the model config.

  Returns:
    Exception types safe to retry for this provider.
  """
  if api_type in ('gemini', 'google_aistudio'):
    from google.genai import errors as genai_errors  # pylint: disable=import-outside-toplevel

    return (genai_errors.ServerError,)
  if api_type == 'openai':
    import openai  # pylint: disable=import-outside-toplevel

    return (
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.RateLimitError,
    )
  return ()


def make_language_model(
    model_config: Mapping[str, Any] | None,
    *,
    disable_language_model: bool = False,
) -> tuple[language_model.LanguageModel, tuple[type[Exception], ...]]:
  """Builds the provider model and its transient retry whitelist.

  Args:
    model_config: the model identity; a missing or ``'stub'`` api type
      builds the deterministic ``PromptStubModel`` from its
      ``prompt_stub`` parameters.
    disable_language_model: force the prompt stub for demonstrations.

  Returns:
    The provider (or stub) model and the retryable exception whitelist;
    the stub whitelist contains exactly the stub's transient error.
  """
  config = dict(model_config or {})
  if _uses_stub_model(config, disable_language_model=disable_language_model):
    return (
        testing_stubs.PromptStubModel(**config.get('prompt_stub', {})),
        (testing_stubs.TransientAPIError,),
    )
  from concordia.contrib import language_models  # pylint: disable=import-outside-toplevel

  api_type = str(config['api_type'])
  model = language_models.language_model_setup(
      api_type=api_type,
      model_name=str(config.get('model_name', '')),
      api_key=config.get('api_key'),
      device=config.get('device'),
  )
  return model, _provider_retryable_exceptions(api_type)


def _make_embedder(
    model_config: Mapping[str, Any] | None, *, disable_language_model: bool
) -> Callable[[str], np.ndarray]:
  """Returns a fixed embedder for stub runs, a transformer otherwise."""
  if _uses_stub_model(
      model_config, disable_language_model=disable_language_model
  ):
    return lambda _: np.ones(_STUB_EMBEDDER_DIM, dtype=np.float32)
  import sentence_transformers  # pylint: disable=import-outside-toplevel

  st_model = sentence_transformers.SentenceTransformer(
      'sentence-transformers/all-mpnet-base-v2'
  )
  return lambda text: st_model.encode(text, show_progress_bar=False)


def _validate_retry_whitelist(
    retryable_exceptions: Sequence[type[Exception]],
) -> None:
  """Rejects whitelists that would retry budget or arbitrary errors.

  Args:
    retryable_exceptions: the whitelist to check.

  Raises:
    ValueError: if the whitelist holds ``Exception``/``BaseException``
      or any base of ``BudgetExhaustedError``, which must never be
      retried or masked as an API service error.
  """
  for exception in retryable_exceptions:
    if exception in (Exception, BaseException) or issubclass(
        budget_wrapper.BudgetExhaustedError, exception
    ):
      raise ValueError(
          f'retry whitelist must not contain {exception.__name__!r}: it '
          'would retry budget exhaustion or arbitrary errors.'
      )


def build_simulation(
    *,
    run_config: Mapping[str, Any],
    model: language_model.LanguageModel,
    embedder: Callable[[str], np.ndarray],
    used_attempts: int = 0,
    retryable_exceptions: Sequence[type[Exception]] = (),
) -> tuple[
    simulation_lib.Simulation,
    drama_director.DramaDirector,
    budget_wrapper.BudgetGuardLanguageModel,
]:
  """Builds the simulation with the shared budget and retry wrapper chain.

  Every entity and the game master share one chain,
  ``RetryLanguageModel(BudgetGuard(provider_model))``, so each retry
  passes the budget check. The guard uses the ORIGINAL total attempt
  limit of the run config and the given used count. Building performs
  no model calls.

  Args:
    run_config: the run's saved configuration dictionary.
    model: the provider (or stub) model to wrap.
    embedder: the sentence embedder of the simulation.
    used_attempts: budget already consumed, e.g. restored from a
      checkpoint envelope.
    retryable_exceptions: transient API exception whitelist; broad
      bases and budget errors are rejected.

  Returns:
    The simulation, its drama director, and the budget guard.

  Raises:
    ValueError: if the retry whitelist is broad or catches budget
      errors.
  """
  _validate_retry_whitelist(retryable_exceptions)
  guard = budget_wrapper.BudgetGuardLanguageModel(
      model,
      total_attempt_limit=run_config['total_model_attempt_limit'],
      used_attempts=used_attempts,
  )
  wrapped = retry_wrapper.RetryLanguageModel(
      guard,
      retry_on_exceptions=tuple(retryable_exceptions),
      # Zero-delay retries keep deterministic tests fast; production
      # runs may prefer backoff, which does not change the contract.
      retry_tries=3,
      retry_delay=0.0,
      jitter=(0.0, 0.0),
      exponential_backoff=False,
  )
  instances = [
      prefab_lib.InstanceConfig(
          prefab=_CHARACTER_PREFAB,
          role=prefab_lib.Role.ENTITY,
          params=dict(params),
      )
      for params in run_config['entity_params']
  ]
  gm_params = {
      **run_config['gm_params'],
      'scene_dicts': run_config['scene_dicts'],
      'beat_dicts': run_config['beat_dicts'],
      # The drama director's scoring configuration travels inside the
      # game master prefab params (eval_interval/scoring_window/
      # tolerance); the prefab reads the keys it knows.
      **run_config.get('director_params', {}),
  }
  instances.append(
      prefab_lib.InstanceConfig(
          prefab=_GM_PREFAB,
          role=prefab_lib.Role.GAME_MASTER,
          params=gm_params,
      )
  )
  config = prefab_lib.Config(
      prefabs={
          _CHARACTER_PREFAB: story_character.Entity(),
          _GM_PREFAB: story_dramaturgic.GameMaster(),
      },
      instances=instances,
      default_premise='',
      default_max_steps=run_config['total_turn_limit'],
  )
  simulation = simulation_lib.Simulation(
      config=config,
      model=wrapped,
      embedder=embedder,
  )
  director = simulation.game_masters[0].get_component(
      drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
      type_=drama_director.DramaDirector,
  )
  return simulation, director, guard


class _CheckpointAdapter:
  """JSON-sanitizes checkpoint data written through the collector.

  ``Simulation.make_checkpoint_data`` sanitizes entity and game-master
  component states but stores the engine's raw log verbatim, and the
  scene tracker logs live ``SceneSpec`` objects into it. The collector
  serializes the whole envelope with ``json.dumps``, so this adapter
  re-sanitizes the raw log before it reaches the writer.
  """

  def __init__(self, simulation: simulation_lib.Simulation):
    self._simulation = simulation

  def make_checkpoint_data(self) -> dict[str, Any]:
    data = self._simulation.make_checkpoint_data()
    data['raw_log'] = self._simulation._make_json_serializable(  # pylint: disable=protected-access
        data['raw_log']
    )
    return data


def _load_envelope(checkpoint_path: str) -> dict[str, Any]:
  """Loads and structurally validates the checkpoint envelope.

  Args:
    checkpoint_path: path of the envelope file.

  Returns:
    The envelope dictionary.

  Raises:
    ValueError: if the file is missing, unreadable, or its schema
      version, budget ranges or committed turn id are invalid.
  """
  if not os.path.exists(checkpoint_path):
    raise ValueError(f'no checkpoint to resume at {checkpoint_path!r}.')
  try:
    with open(checkpoint_path, encoding='utf-8') as handle:
      envelope = json.load(handle)
  except json.JSONDecodeError as e:
    raise ValueError(f'corrupt checkpoint {checkpoint_path!r}: {e}') from e
  if envelope.get('schema_version') != collector.CHECKPOINT_SCHEMA_VERSION:
    raise ValueError(
        'checkpoint schema version '
        f'{envelope.get("schema_version")!r} does not match '
        f'{collector.CHECKPOINT_SCHEMA_VERSION}.'
    )
  if not isinstance(envelope.get('run_config'), dict):
    raise ValueError('checkpoint envelope has no run config.')
  used = envelope['used_model_attempts']
  attempt_limit = envelope['total_model_attempt_limit']
  committed = envelope['committed_turn_id']
  turn_limit = envelope['total_turn_limit']
  if not 0 <= used <= attempt_limit:
    raise ValueError(f'envelope attempt budget out of range: {used}.')
  if not 0 <= committed <= turn_limit:
    raise ValueError(f'envelope committed turn id out of range: {committed}.')
  return envelope


def _reject_configured_mismatch(
    run_config: Mapping[str, Any],
    *,
    total_turn_limit: int,
    total_model_attempt_limit: int,
    mode: str,
) -> None:
  """Rejects CLI parameters that disagree with the saved run config.

  Args:
    run_config: the authoritative run config.
    total_turn_limit: the requested turn limit.
    total_model_attempt_limit: the requested attempt limit.
    mode: the requested mode.

  Raises:
    ValueError: if any requested value differs from the saved one; a
      resume must never silently raise a budget or switch stages.
  """
  if total_turn_limit != run_config['total_turn_limit']:
    raise ValueError(
        'total_turn_limit mismatch: checkpoint holds'
        f' {run_config["total_turn_limit"]}, got {total_turn_limit}.'
    )
  if total_model_attempt_limit != run_config['total_model_attempt_limit']:
    raise ValueError(
        'total_model_attempt_limit mismatch: checkpoint holds'
        f' {run_config["total_model_attempt_limit"]},'
        f' got {total_model_attempt_limit}.'
    )
  if mode != run_config['mode']:
    raise ValueError(
        f'mode mismatch: checkpoint holds {run_config["mode"]!r}, got {mode!r}.'
    )


def _reject_budget_exhausted_summary(summary_path: str) -> None:
  """Refuses auto-resume of a run whose summary records budget exhaustion.

  The last checkpoint of a budget-stopped run may predate the
  exhaustion, so its remaining allowance alone must not decide to
  continue.

  Args:
    summary_path: path of the run's summary.json, if present.

  Raises:
    ValueError: if the summary records a budget-limit reason or a
      budget-exhaustion diagnostic.
  """
  if not os.path.exists(summary_path):
    return
  with open(summary_path, encoding='utf-8') as handle:
    summary = json.load(handle)
  if summary.get(
      'reason'
  ) == story_terminator.REASON_BUDGET_LIMIT or summary.get('budget_exhausted'):
    raise ValueError(
        'refusing to resume a budget-exhausted run; the original budget '
        'cannot be reset from the old checkpoint.'
    )


def _component_termination_reason(
    simulation: simulation_lib.Simulation,
) -> str:
  """Reads the terminator component's reason from the game master."""
  terminator = simulation.game_masters[0].get_component(
      terminate_lib.DEFAULT_TERMINATE_COMPONENT_KEY,
      type_=story_terminator.SceneOrStoryTerminator,
  )
  return terminator.termination_reason()


def _step_durations(marks: list[float], start: float) -> list[float]:
  """Diffs consecutive step marks against the play start time."""
  durations = []
  previous = start
  for mark in marks:
    durations.append(mark - previous)
    previous = mark
  return durations


def _write_outputs(
    state_dir: str,
    *,
    result: RunResult,
    run_config: Mapping[str, Any],
    turn_log: collector.StoryTurnLog,
    committed_turn_id: int,
) -> None:
  """Writes summary.json and the two script exports.

  Only story turns confirmed by the latest full checkpoint are
  rendered; an uncommitted tail never reaches the exports.

  Args:
    state_dir: the run's state directory.
    result: the run's result record.
    run_config: the run's configuration.
    turn_log: the story-turn log to read confirmed records from.
    committed_turn_id: the highest confirmed turn id.
  """
  summary = {
      **dataclasses.asdict(result),
      'mode': run_config['mode'],
      'total_turn_limit': run_config['total_turn_limit'],
      'total_model_attempt_limit': run_config['total_model_attempt_limit'],
      'model_config': run_config['model_config'],
      'story_title': run_config['story']['title'],
  }
  collector._atomic_write_text(  # pylint: disable=protected-access
      os.path.join(state_dir, 'summary.json'),
      json.dumps(summary, ensure_ascii=False, indent=2),
  )
  records = [
      record
      for record in turn_log.load()
      if record['turn_id'] <= committed_turn_id
  ]
  story = run_config['story']
  scene_display = {
      'interior': story['interior'],
      'location': story['location'],
      'time_label': story['time_label'],
  }
  collector._atomic_write_text(  # pylint: disable=protected-access
      os.path.join(state_dir, 'script.fountain'),
      renderer.render_fountain(records, scene_display),
  )
  collector._atomic_write_text(  # pylint: disable=protected-access
      os.path.join(state_dir, 'script.txt'),
      renderer.render_plain_text(records, scene_display),
  )


def run(
    state_dir: str,
    *,
    resume: bool = False,
    disable_language_model: bool = False,
    total_turn_limit: int | None = None,
    total_model_attempt_limit: int | None = None,
    mode: str = MODE_BEATS_ONLY,
    model_config: Mapping[str, Any] | None = None,
    run_config: Mapping[str, Any] | None = None,
) -> RunResult:
  """Runs (or resumes) one story enactment and writes its outputs.

  Args:
    state_dir: directory holding story_turns.jsonl, checkpoint.json and
      the summary/script outputs.
    resume: resume the run recorded in ``state_dir``.
    disable_language_model: force the deterministic prompt stub.
    total_turn_limit: total story-turn budget of a first run; None uses
      the 24-turn default. On resume, None keeps the checkpoint's
      original limit; an explicitly passed different value is rejected.
    total_model_attempt_limit: total model-attempt budget; None uses
      the 400-attempt default, and the same resume rule applies.
    mode: stage switch; stage 2 implements beats_only (scoring
      disabled, identical stage-1 call pattern), tension_observe
      (default scoring configuration, scores recorded only) and
      director (default scoring configuration plus feedback: deviating
      scores generate directives, fused into resolutions through the
      story's allowed environment events with a cooldown between
      executions). Stage 3 adds character_depth: the director
      configuration plus an EmotionalStance commitment and a cached
      RelationshipsToOthers view on every character, with the acting
      character's emotion collected into the story turns.
    model_config: identity of a live model (api_type, model_name,
      api_key); a first run without it uses the prompt stub. On resume
      the identity must match the checkpoint and only the api_key
      carries over.
    run_config: optional prebuilt run config for a first run (used by
      tests and higher-level drivers); when given, its limits and mode
      are authoritative and the limit/mode parameters are ignored.

  Returns:
    The run's result record; summary.json, script.fountain and
      script.txt are written next to the state files.

  Raises:
    ValueError: on resume validation failures (missing or mismatched
      checkpoint, mismatched limits, mode or model identity,
      budget-exhausted summary) or when starting over an existing run.
  """
  os.makedirs(state_dir, exist_ok=True)
  checkpoint_path = os.path.join(state_dir, 'checkpoint.json')
  summary_path = os.path.join(state_dir, 'summary.json')
  turn_log = collector.StoryTurnLog(
      os.path.join(state_dir, 'story_turns.jsonl')
  )

  used_at_start = 0
  committed_start = 0
  saved_state: dict[str, Any] = {}
  if resume:
    envelope = _load_envelope(checkpoint_path)
    config = envelope['run_config']
    # The original total limits are authoritative on resume: an
    # explicitly passed different value is rejected, never silently
    # applied to raise the budget.
    _reject_configured_mismatch(
        config,
        total_turn_limit=(
            total_turn_limit
            if total_turn_limit is not None
            else config['total_turn_limit']
        ),
        total_model_attempt_limit=(
            total_model_attempt_limit
            if total_model_attempt_limit is not None
            else config['total_model_attempt_limit']
        ),
        mode=mode,
    )
    _reject_budget_exhausted_summary(summary_path)
    used_at_start = envelope['used_model_attempts']
    committed_start = envelope['committed_turn_id']
    saved_state = envelope['simulation_state']
  else:
    if os.path.exists(checkpoint_path):
      raise ValueError(
          f'{checkpoint_path!r} already exists; pass resume=True to continue.'
      )
    if run_config is None:
      config = make_run_config(
          story_config.make_example_config(),
          total_turn_limit=total_turn_limit or 24,
          total_model_attempt_limit=total_model_attempt_limit or 400,
          mode=mode,
          model_config=model_config,
      )
    else:
      # A supplied run config is authoritative; the limit and mode
      # parameters of this call are ignored on a first run.
      config = run_config

  if resume:
    # The checkpointed identity is authoritative; only this invocation's
    # credentials (the API key) merge over it, because the checkpoint
    # never stores secrets.
    effective_model_config = _merge_resume_model_config(
        config['model_config'], model_config
    )
  else:
    effective_model_config = (
        dict(model_config) if model_config is not None else None
    ) or dict(config['model_config'])
  provider, retryable = make_language_model(
      effective_model_config, disable_language_model=disable_language_model
  )
  embedder = _make_embedder(
      effective_model_config, disable_language_model=disable_language_model
  )
  simulation, director, guard = build_simulation(
      run_config=config,
      model=provider,
      embedder=embedder,
      used_attempts=used_at_start,
      retryable_exceptions=retryable,
  )
  # The collector serializes the whole envelope, so checkpoint data
  # must be JSON-safe (see _CheckpointAdapter).
  checkpoint_source = _CheckpointAdapter(simulation)
  turn_limit = config['total_turn_limit']
  attempt_limit = config['total_model_attempt_limit']

  stop_before_play = False
  exception_reason: str | None = None
  budget_error: str | None = None
  error_message: str | None = None
  if resume:
    # Resume order: rebuild, restore state, then trim and verify the
    # story-turn log against the committed boundary.
    simulation.load_from_checkpoint(saved_state)
    turn_log.trim_after(committed_start)
    turn_log.validate_contiguous()
    records = turn_log.load()
    if len(records) != committed_start:
      raise ValueError(
          f'story turn log holds {len(records)} records, expected exactly '
          f'{committed_start} committed turns.'
      )
    if director.get_state()['story_turn'] != committed_start:
      raise ValueError(
          'drama director story_turn '
          f'{director.get_state()["story_turn"]} does not match the '
          f'committed turn id {committed_start}.'
      )
    remaining_turns = turn_limit - committed_start
    remaining_attempts = attempt_limit - used_at_start
    stop_before_play = (
        director.is_story_stopped()
        or remaining_turns <= 0
        or remaining_attempts <= 0
    )
    if remaining_attempts <= 0:
      exception_reason = story_terminator.REASON_BUDGET_LIMIT
      budget_error = (
          'attempt budget of'
          f' {attempt_limit} exhausted at resume; no attempts remain.'
      )
    max_steps = max(remaining_turns, 0)
  else:
    # First-run order: inject the public premise into the game master
    # while READY (no awaiting-event flag, so it never counts as a
    # story step), then save the empty turn file and the initial
    # checkpoint with committed_turn_id = 0.
    simulation.game_masters[0].observe(
        '[event] ' + make_public_premise(config['story'])
    )
    collector._atomic_write_text(  # pylint: disable=protected-access
        turn_log.path, ''
    )
    collector._save_checkpoint(  # pylint: disable=protected-access
        simulation=checkpoint_source,
        budget_guard=guard,
        checkpoint_path=checkpoint_path,
        run_config=config,
        committed_turn_id=0,
        total_turn_limit=turn_limit,
        total_model_attempt_limit=attempt_limit,
    )
    max_steps = turn_limit

  base_callback = collector.make_step_callback(
      simulation=checkpoint_source,
      drama_director=director,
      budget_guard=guard,
      turn_log=turn_log,
      checkpoint_path=checkpoint_path,
      run_config=config,
      total_turn_limit=turn_limit,
      total_model_attempt_limit=attempt_limit,
      # Only character_depth registers EmotionalStance on the entities,
      # so only that mode collects the acting character's emotion.
      emotion_enabled=config['mode'] == MODE_CHARACTER_DEPTH,
  )
  step_marks: list[float] = []

  def timed_callback(step_data: Any) -> None:
    base_callback(step_data)
    step_marks.append(time.monotonic())

  play_start = time.monotonic()
  if not stop_before_play:
    try:
      # No auto checkpoint_path/get_state_callback: the collector's
      # callback is the only save boundary.
      simulation.play(
          premise='',
          max_steps=max_steps,
          step_callback=timed_callback,
      )
    except budget_wrapper.BudgetExhaustedError as e:
      exception_reason = story_terminator.REASON_BUDGET_LIMIT
      budget_error = str(e)
    except Exception as e:  # pylint: disable=broad-except
      exception_reason = story_terminator.REASON_ERROR
      error_message = f'{type(e).__name__}: {e}'
    # BaseException (e.g. a simulated crash) propagates untouched.
  wall_time = time.monotonic() - play_start

  committed = collector._read_committed_turn_id(  # pylint: disable=protected-access
      checkpoint_path
  )
  if committed is None:
    committed = 0
  director_budget_stopped = director.get_state()['budget_stopped']
  if director_budget_stopped and budget_error is None:
    budget_error = (
        'post-resolution budget stop after committed turn '
        f'{committed} (attempt budget of {attempt_limit}).'
    )
  component_reason = _component_termination_reason(simulation)
  reason = story_terminator.resolve_final_reason(
      exception_reason,
      component_reason or None,
      committed,
      turn_limit,
  )
  result = RunResult(
      reason=reason,
      committed_turn_id=committed,
      budget_exhausted=budget_error is not None,
      budget_error=budget_error,
      error_message=error_message,
      attempts_used_this_process=guard.used_attempts - used_at_start,
      used_model_attempts=guard.used_attempts,
      wall_time_seconds=wall_time,
      step_durations=_step_durations(step_marks, play_start),
      token_usage=None,
  )
  _write_outputs(
      state_dir,
      result=result,
      run_config=config,
      turn_log=turn_log,
      committed_turn_id=committed,
  )
  return result


def main(argv: Sequence[str]) -> None:
  del argv
  if not _FLAGS.state_dir:
    raise ValueError('--state_dir is required.')
  if not _FLAGS.disable_language_model and not _FLAGS.api_type:
    raise ValueError('--api_type (or --disable_language_model) is required.')
  model_config = None
  if not _FLAGS.disable_language_model:
    model_config = {
        'api_type': _FLAGS.api_type,
        'model_name': _FLAGS.model_name,
        'api_key': _FLAGS.api_key,
    }
  result = run(
      _FLAGS.state_dir,
      resume=_FLAGS.resume,
      disable_language_model=_FLAGS.disable_language_model,
      total_turn_limit=_FLAGS.total_turn_limit,
      total_model_attempt_limit=_FLAGS.total_model_attempt_limit,
      mode=_FLAGS.mode,
      model_config=model_config,
  )
  print(
      f'reason={result.reason} committed_turns={result.committed_turn_id} '
      f'attempts={result.used_model_attempts} '
      f'wall_time={result.wall_time_seconds:.2f}s'
  )


if __name__ == '__main__':
  app.run(main)
