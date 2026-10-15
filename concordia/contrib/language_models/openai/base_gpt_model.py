# Copyright 2023 DeepMind Technologies Limited.
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

"""Base class for GPT models (OpenAI and Azure)."""

from collections.abc import Collection, Sequence
from typing import override

from concordia.language_model import language_model
from concordia.utils import measurements as measurements_lib
from openai import AzureOpenAI
from openai import OpenAI


_MAX_MULTIPLE_CHOICE_ATTEMPTS = 20
_DEFAULT_VERBOSITY = 'low'


class BaseGPTModel(language_model.LanguageModel):
  """Base class for GPT models (OpenAI and Azure).

  Supports "thinking" models like GPT-5.
  """

  def __init__(
      self,
      model_name: str,
      client: AzureOpenAI | OpenAI,
      measurements: measurements_lib.Measurements | None = None,
      channel: str = language_model.DEFAULT_STATS_CHANNEL,
  ):
    """Initializes the base instance."""
    self._model_name = model_name
    self._measurements = measurements
    self._channel = channel
    self._client = client

    self._verbosity = _DEFAULT_VERBOSITY
    if 'gpt-4o' in self._model_name:
      self._verbosity = 'medium'  # GPT-4o only supports verbosity 'medium'

  def _sample_text(
      self,
      prompt: str,
      reasoning_effort: str,
      verbosity: str,
      *,
      max_tokens: int = language_model.DEFAULT_MAX_TOKENS,
      terminators: Collection[str] = language_model.DEFAULT_TERMINATORS,
      temperature: float = 1.0,  # GPT-5 only supports temperature 1.0
      top_p: float = language_model.DEFAULT_TOP_P,
      timeout: float = language_model.DEFAULT_TIMEOUT_SECONDS,
      seed: int | None = None,
  ) -> str:
    del terminators, top_p  # Unused for OpenAI models.

    messages = [
        {
            'role': 'system',
            'content': (
                'You always continue input provided '
                + 'by the user and you never repeat what '
                + 'the user already said.'
            ),
        },
        {
            'role': 'user',
            'content': 'Question: Is Jake a turtle?\nAnswer: Jake is ',
        },
        {'role': 'assistant', 'content': 'not a turtle.'},
        {
            'role': 'user',
            'content': (
                'Question: What is Priya doing right now?\nAnswer: '
                + 'Priya is currently '
            ),
        },
        {'role': 'assistant', 'content': 'sleeping.'},
        {'role': 'user', 'content': prompt},
    ]

    # pyrefly: ignore [no-matching-overload]
    response = self._client.chat.completions.create(
        model=self._model_name,
        messages=messages,
        temperature=temperature,
        max_completion_tokens=max_tokens,
        timeout=timeout,
        seed=seed,
        reasoning_effort=reasoning_effort,
        verbosity=verbosity,
    )

    # Reasoning models can spend the whole completion budget on an invisible
    # reasoning trace, so OpenAI-compatible providers may deliver a response
    # whose `content` is None or whitespace-only. Enforce the visible-text
    # contract instead of returning None (same policy as
    # together_ai_model._visible_text): re-asking the identical request is
    # unlikely to help and would only spend more paid attempts.
    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
      raise language_model.InvalidResponseError(
          'OpenAI-compatible endpoint returned no visible text:'
          ' message.content must be a nonempty string. Request not retried;'
          ' check model reasoning support and response budget.'
      )

    if self._measurements is not None:
      self._measurements.publish_datum(
          self._channel, {'raw_text_length': len(content)}
      )

    return content

  @override
  def sample_text(
      self,
      prompt: str,
      *,
      max_tokens: int = language_model.DEFAULT_MAX_TOKENS,
      terminators: Collection[str] = language_model.DEFAULT_TERMINATORS,
      temperature: float = 1.0,  # GPT-5 only supports temperature 1.0
      top_p: float = language_model.DEFAULT_TOP_P,
      top_k: int = language_model.DEFAULT_TOP_K,
      timeout: float = language_model.DEFAULT_TIMEOUT_SECONDS,
      seed: int | None = None,
  ) -> str:
    del top_k  # Unused
    return self._sample_text(
        prompt=prompt,
        # 'low' is the lowest reasoning effort that is widely supported
        # across OpenAI-compatible endpoints ('minimal' is rejected by
        # several of them, e.g. proxies listing low/medium/high/...).
        reasoning_effort='low',
        verbosity=self._verbosity,
        max_tokens=max_tokens,
        terminators=terminators,
        temperature=temperature,
        top_p=top_p,
        timeout=timeout,
        seed=seed,
    )

  @override
  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, dict[str, float]]:
    prompt = (
        prompt
        + '\nRespond EXACTLY with one of the following strings:\n'
        + '\n'.join(responses)
        + '.'
    )

    sample = ''
    answer = ''
    for attempts in range(_MAX_MULTIPLE_CHOICE_ATTEMPTS):
      answer = self._sample_text(
          prompt,
          reasoning_effort='medium',
          verbosity=self._verbosity,
          temperature=1.0,
          seed=seed,
      )
      try:
        idx = responses.index(answer)
      except ValueError:
        continue
      else:
        if self._measurements is not None:
          self._measurements.publish_datum(
              self._channel, {'choices_calls': attempts}
          )
        debug = {}
        return idx, responses[idx], debug

    raise language_model.InvalidResponseError((
        f'Too many multiple choice attempts.\nLast attempt: {sample}, '
        + f'extracted: {answer}'
    ))
