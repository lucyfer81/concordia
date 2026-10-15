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
from concordia.contrib.language_models import retry_stats_wrapper
from concordia.contrib.prefabs.entity import story_character
from concordia.contrib.prefabs.game_master import story_dramaturgic
from concordia.contrib.prefabs.game_master import story_scene
from concordia.language_model import language_model
from concordia.prefabs.simulation import generic as simulation_lib
from concordia.typing import prefab as prefab_lib
from concordia.typing import scene as scene_lib
import numpy as np

from examples.story_enactment import collector
from examples.story_enactment import provenance
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
    api_retries_used_this_process: API retries initiated and reaching
      the provider in this process only (spec 13.3, acceptance F5).
      Budget-blocked requests never count; output-parse re-asks (e.g.
      the second tension-scoring call after an unparseable answer) are
      new logical requests and are counted separately below.
    used_api_retries: cumulative API retries including the count
      restored from the checkpoint envelope.
    scoring_parse_retries_used_this_process: output-parse re-asks of
      the tension-scoring question issued in this process only; these
      consume model attempts but are not API retries.
    used_scoring_parse_retries: cumulative scoring parse re-asks,
      including the count restored with the director's state.
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
  api_retries_used_this_process: int
  used_api_retries: int
  scoring_parse_retries_used_this_process: int
  used_scoring_parse_retries: int
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
          description=(
              '摊牌时刻：已有来源的关键事实通过角色自己的发言或合法环境事件公开，'
              '相关角色可自主回应；不要求任何角色认罪或违反底线'
          ),
          tension_range=(7, 9),
          done_question=(
              '仅按公开且已发生的事件判断，是否已有来源的关键事实被明确披露，'
              '并由相关角色在自己的发言中作出具体回应？'
              '私密设定、未拆阅的信件、未公开的录音、指控与核验请求本身都不算事实揭示。'
          ),
          min_turns=3,
          max_turns=10,
      ),
      make_final_beat(story_config.make_example_config().allowed_endings),
  )


def make_final_beat(
    allowed_endings: Sequence[str],
    *,
    beat_id: str = 'b4',
) -> drama_director.Beat:
  """Builds the closing beat from a story's allowed ending range.

  The description and the completion question are constructed from the
  endings the story may reach, so both the game master's resolutions and
  the last beat's completion judgment are conditioned on this story's
  ending range instead of a fixed catalogue. The completion question
  keeps the "judge only by events that actually happened" constraint of
  the P1 prompt (spec section 4.3).

  Args:
    allowed_endings: the endings the enactment may reach.
    beat_id: stable identifier of the closing beat.

  Returns:
    The final beat of a story's outline.

  Raises:
    ValueError: if ``allowed_endings`` is empty.
  """
  if not allowed_endings:
    raise ValueError('a story must allow at least one ending.')
  endings = '；'.join(f'「{ending}」' for ending in allowed_endings)
  return drama_director.Beat(
      id=beat_id,
      description=f'结局成立：下列允许结局之一的具体条件已经公开发生：{endings}',
      tension_range=(4, 6),
      done_question=(
          '仅按已发生的事件判断，下列允许结局之一的全部具体条件是否已经公开成立：'
          f'{endings}？不能把朝某方向发展、提出核验或封存、计划离开、'
          '暂缓争执当作结局成立。涉及同意、承诺或协商的条件必须有相应角色'
          '自己的明确发言支持；GM不能代替角色决定。若条件仍待落实，回答否。'
      ),
      min_turns=2,
      max_turns=8,
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
      (
          '裁决者知道秘密不等于角色知道。私隐和仅裁决者可知事实不得直接广播；'
          '只有角色自己的合法披露或有既定来源的公开证据才能增加共同知识。'
      ),
      (
          '所有玩家角色自主决定自己的认罪、承诺、交付物证、签字和秘密披露；'
          '裁决者不得代替任何玩家作这些选择，也不得将请求、指控、计划、'
          '谎言或他人的陈述直接当作已经发生的行动或客观真相。'
      ),
      '环境事件只能从以下选项中选择：'
      + '；'.join(story.allowed_environment_events),
  )
  constraints += tuple(
      f'角色底线（始终有效）：{character.name}：{character.hard_constraint}'
      for character in story.characters
  )
  # The allowed ending range is game-master-only context (acceptance F3):
  # it conditions resolutions and the closing beat's judgment here and in
  # the run config's beat dictionaries, but never enters scene premises
  # or the broadcast queue, so characters receive no ending guidance.
  constraints += (
      '剧情结局只能收束为以下方向之一，不得引入范围外的结局：'
      + '；'.join(story.allowed_endings),
  )
  return {
      'name': _GM_NAME,
      'public_facts': [
          story.conflict_start,
          *story.world_facts,
          *(
              f'{character.name}：{fact}'
              for character in story.characters
              for fact in character.public_facts
          ),
      ],
      'player_names': [character.name for character in story.characters],
      'character_constraints': {
          character.name: character.hard_constraint
          for character in story.characters
      },
      'allowed_environment_events': list(story.allowed_environment_events),
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


def _actual_model_identity(
    model_config: Mapping[str, Any] | None,
    *,
    disable_language_model: bool,
) -> dict[str, Any]:
  """Returns the secret-free identity of the model actually in effect.

  A first run may receive a run config built without a model config
  (the README's standard call passes the live model as the
  ``model_config`` argument of ``run`` instead), and
  ``disable_language_model`` replaces either with the deterministic
  prompt stub. The identity of the normalized run config therefore
  follows what ``make_language_model`` actually builds, so checkpoints
  and summaries archive the provider really in use.

  Args:
    model_config: the effective model config of the invocation.
    disable_language_model: whether the deterministic stub is forced.

  Returns:
    The secret-free model identity: the effective config's identity for
    a live provider, or the canonical stub identity (plus any stub
    parameters) when the prompt stub is in effect.
  """
  config = dict(model_config or {})
  if _uses_stub_model(config, disable_language_model=disable_language_model):
    identity = {'api_type': 'stub', 'model_name': 'prompt_stub'}
    if 'prompt_stub' in config:
      identity['prompt_stub'] = config['prompt_stub']
    return identity
  return _public_model_identity(config)


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
    beats: Sequence[drama_director.Beat] | None = None,
    total_turn_limit: int = 24,
    total_model_attempt_limit: int = 400,
    mode: str = MODE_BEATS_ONLY,
    model_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """Builds the JSON-safe run config of one story enactment.

  The beats travel explicitly (acceptance F1): every non-default story
  must pass its own beat sequence, matched to its conflict, evidence and
  allowed endings — the acceptance scenarios provide such factories in
  ``acceptance.py``. ``beats=None`` keeps the default outline of the
  appendix-C estate-dispute example, which matches that story only.

  Args:
    story: the story to enact.
    beats: the beat outline of the story; None uses the default
      estate-dispute example beats.
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
  outline = tuple(beats) if beats is not None else make_example_beats()
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
      'beat_dicts': [beat.to_dict() for beat in outline],
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
    used_api_retries: int = 0,
) -> tuple[
    simulation_lib.Simulation,
    drama_director.DramaDirector,
    budget_wrapper.BudgetGuardLanguageModel,
    retry_stats_wrapper.RetryStatsLanguageModel,
]:
  """Builds the simulation with the shared budget and retry wrapper chain.

  Every entity and the game master share one chain assembled by the
  retry-statistics wrapper around the budget guard,
  ``RetryStats(guard)``, so each retry passes the budget check, each
  logical request's attempts are attributed to that request alone, and
  the retry counter sees every logical request. The guard uses the
  ORIGINAL total attempt limit of the run config and the given used
  count; the retry-statistics wrapper is seeded with
  ``used_api_retries`` (e.g. restored from a checkpoint envelope).
  Building performs no model calls.

  Args:
    run_config: the run's saved configuration dictionary.
    model: the provider (or stub) model to wrap.
    embedder: the sentence embedder of the simulation.
    used_attempts: budget already consumed, e.g. restored from a
      checkpoint envelope.
    retryable_exceptions: transient API exception whitelist; broad
      bases and budget errors are rejected.
    used_api_retries: cumulative API retries already accounted for,
      e.g. restored from a checkpoint envelope.

  Returns:
    The simulation, its drama director, the budget guard, and the
    retry-statistics wrapper.

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
  retry_stats = retry_stats_wrapper.RetryStatsLanguageModel(
      guard,
      retry_on_exceptions=tuple(retryable_exceptions),
      used_retries=used_api_retries,
      # Zero-delay retries keep deterministic tests fast; production
      # runs may prefer backoff, which does not change the contract.
      retry_delay=0.0,
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
      model=retry_stats,
      embedder=embedder,
  )
  director = simulation.game_masters[0].get_component(
      drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
      type_=drama_director.DramaDirector,
  )
  return simulation, director, guard, retry_stats


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
  if not 0 <= envelope.get('used_api_retries', 0) <= used:
    raise ValueError(
        'envelope API retry count out of range:'
        f" {envelope.get('used_api_retries')}."
    )
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
  """Writes summary.json and the FDX, Fountain and plain-text exports.

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
      os.path.join(state_dir, 'script.fdx'),
      renderer.render_fdx(records, scene_display),
  )
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
      api_key); a first run without it uses the prompt stub. On a first
      run the model actually in effect defines the archived identity:
      the run config used for building, saving and summarizing is
      normalized to it, with the API key stripped — so the README's
      standard call (a run config built without a model config plus a
      live ``model_config`` here) records the live provider, and
      ``disable_language_model`` records the stub actually used. On
      resume the identity must match the checkpoint and only the
      api_key carries over.
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
  api_retries_at_start = 0
  committed_start = 0
  saved_state: dict[str, Any] = {}
  config: dict[str, Any]
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
    api_retries_at_start = envelope.get('used_api_retries', 0)
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
      config = dict(run_config)

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
  if not resume:
    # The effective model — not the possibly stub-defaulted run config
    # — defines the archived identity: the README's standard call passes
    # the live model to ``run`` while building the run config without
    # it. Normalize the config used for building, saving and
    # summarizing so checkpoints record the provider actually in use,
    # with the API key stripped.
    config = {
        **config,
        'model_config': _actual_model_identity(
            effective_model_config,
            disable_language_model=disable_language_model,
        ),
    }
  simulation, director, guard, retry_stats = build_simulation(
      run_config=config,
      model=provider,
      embedder=embedder,
      used_attempts=used_at_start,
      retryable_exceptions=retryable,
      used_api_retries=api_retries_at_start,
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
        retry_stats=retry_stats,
        checkpoint_path=checkpoint_path,
        run_config=config,
        committed_turn_id=0,
        total_turn_limit=turn_limit,
        total_model_attempt_limit=attempt_limit,
    )
    max_steps = turn_limit
  # The scoring-parse-retry baseline is read only after component state
  # is restored (a fresh director still holds zero), so the per-process
  # metric never counts scoring re-asks of previous processes.
  director_state: Mapping[str, Any] = director.get_state()
  parse_retries_at_start = int(director_state.get('scoring_parse_retries', 0))

  provenance.archive_invocation(
      state_dir,
      run_config=config,
      committed_turn_id=committed_start,
      resume=resume,
  )

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
      retry_stats=retry_stats,
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
  final_director_state: Mapping[str, Any] = director.get_state()
  director_budget_stopped = final_director_state['budget_stopped']
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
  used_scoring_parse_retries = int(
      final_director_state['scoring_parse_retries']
  )
  result = RunResult(
      reason=reason,
      committed_turn_id=committed,
      budget_exhausted=budget_error is not None,
      budget_error=budget_error,
      error_message=error_message,
      attempts_used_this_process=guard.used_attempts - used_at_start,
      used_model_attempts=guard.used_attempts,
      api_retries_used_this_process=(
          retry_stats.used_retries - api_retries_at_start
      ),
      used_api_retries=retry_stats.used_retries,
      scoring_parse_retries_used_this_process=(
          used_scoring_parse_retries - parse_retries_at_start
      ),
      used_scoring_parse_retries=used_scoring_parse_retries,
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
