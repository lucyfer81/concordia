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

"""OpenAI visible-text contract; no provider client or network calls."""

import types
from unittest import mock

from concordia.contrib.language_models.openai import base_gpt_model
from concordia.language_model import language_model
import pytest


def client_with(content):
  client = mock.Mock()
  client.chat.completions.create.return_value = types.SimpleNamespace(
      choices=[
          types.SimpleNamespace(
              message=types.SimpleNamespace(
                  content=content,
                  reasoning_content='private reasoning fixture',
              ),
              finish_reason='stop',
          )
      ]
  )
  return client


@pytest.mark.parametrize('content', [None, '', '   \n'])
def test_sample_text_rejects_contentless_response(content):
  # Reasoning models can spend the whole completion budget on an invisible
  # reasoning trace, so OpenAI-compatible providers deliver `content=None`.
  # `sample_text` must enforce its `str` contract instead of returning None.
  model = base_gpt_model.BaseGPTModel(
      model_name='gpt-fixture', client=client_with(content)
  )
  with pytest.raises(
      language_model.InvalidResponseError, match='no visible text'
  ):
    model.sample_text('Hello')


def test_sample_text_returns_visible_content():
  model = base_gpt_model.BaseGPTModel(
      model_name='gpt-fixture', client=client_with('Hello.')
  )
  assert model.sample_text('Hello') == 'Hello.'


def test_sample_text_uses_widely_supported_reasoning_effort():
  # 'minimal' is rejected by several OpenAI-compatible endpoints (valid
  # levels there are low/medium/high/...), so text sampling must request
  # the lowest widely supported level instead.
  client = client_with('Hello.')
  model = base_gpt_model.BaseGPTModel(model_name='gpt-fixture', client=client)
  model.sample_text('Hello')
  values = client.chat.completions.create.call_args.kwargs
  assert values['reasoning_effort'] == 'low'


if __name__ == '__main__':
  pytest.main([__file__])
