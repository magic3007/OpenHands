import copy
from unittest.mock import MagicMock, patch

import pytest
from litellm.exceptions import (
    APIConnectionError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
)

from openhands.core.config import LLMConfig
from openhands.core.exceptions import OperationCancelled
from openhands.core.message import Message, TextContent
from openhands.llm.llm import LLM
from openhands.llm.metrics import Metrics


@pytest.fixture(autouse=True)
def mock_logger(monkeypatch):
    # suppress logging of completion data to file
    mock_logger = MagicMock()
    monkeypatch.setattr('openhands.llm.debug_mixin.llm_prompt_logger', mock_logger)
    monkeypatch.setattr('openhands.llm.debug_mixin.llm_response_logger', mock_logger)
    return mock_logger


@pytest.fixture
def default_config():
    return LLMConfig(
        model='gpt-4o',
        api_key='test_key',
        num_retries=2,
        retry_min_wait=1,
        retry_max_wait=2,
    )


def test_llm_init_with_default_config(default_config):
    llm = LLM(default_config)
    assert llm.config.model == 'gpt-4o'
    assert llm.config.api_key == 'test_key'
    assert isinstance(llm.metrics, Metrics)
    assert llm.metrics.model_name == 'gpt-4o'


@patch('openhands.llm.llm.litellm.get_model_info')
def test_llm_init_with_model_info(mock_get_model_info, default_config):
    mock_get_model_info.return_value = {
        'max_input_tokens': 8000,
        'max_output_tokens': 2000,
    }
    llm = LLM(default_config)
    llm.init_model_info()
    assert llm.config.max_input_tokens == 8000
    assert llm.config.max_output_tokens == 2000


@patch('openhands.llm.llm.litellm.get_model_info')
def test_llm_init_without_model_info(mock_get_model_info, default_config):
    mock_get_model_info.side_effect = Exception('Model info not available')
    llm = LLM(default_config)
    llm.init_model_info()
    assert llm.config.max_input_tokens == 4096
    assert llm.config.max_output_tokens == 4096


def test_llm_init_with_custom_config():
    custom_config = LLMConfig(
        model='custom-model',
        api_key='custom_key',
        max_input_tokens=5000,
        max_output_tokens=1500,
        temperature=0.8,
        top_p=0.9,
    )
    llm = LLM(custom_config)
    assert llm.config.model == 'custom-model'
    assert llm.config.api_key == 'custom_key'
    assert llm.config.max_input_tokens == 5000
    assert llm.config.max_output_tokens == 1500
    assert llm.config.temperature == 0.8
    assert llm.config.top_p == 0.9


def test_llm_init_with_metrics():
    config = LLMConfig(model='gpt-4o', api_key='test_key')
    metrics = Metrics()
    llm = LLM(config, metrics=metrics)
    assert llm.metrics is metrics
    assert (
        llm.metrics.model_name == 'default'
    )  # because we didn't specify model_name in Metrics init


@patch('openhands.llm.llm.litellm_completion')
@patch('time.time')
def test_response_latency_tracking(mock_time, mock_litellm_completion):
    # Mock time.time() to return controlled values
    mock_time.side_effect = [1000.0, 1002.5]  # Start time, end time (2.5s difference)

    # Mock the completion response with a specific ID
    mock_response = {
        'id': 'test-response-123',
        'choices': [{'message': {'content': 'Test response'}}],
    }
    mock_litellm_completion.return_value = mock_response

    # Create LLM instance and make a completion call
    config = LLMConfig(model='gpt-4o', api_key='test_key')
    llm = LLM(config)
    response = llm.completion(messages=[{'role': 'user', 'content': 'Hello!'}])

    # Verify the response latency was tracked correctly
    assert len(llm.metrics.response_latencies) == 1
    latency_record = llm.metrics.response_latencies[0]
    assert latency_record.model == 'gpt-4o'
    assert (
        latency_record.latency == 2.5
    )  # Should be the difference between our mocked times
    assert latency_record.response_id == 'test-response-123'

    # Verify the completion response was returned correctly
    assert response['id'] == 'test-response-123'
    assert response['choices'][0]['message']['content'] == 'Test response'


def test_llm_reset():
    llm = LLM(LLMConfig(model='gpt-4o-mini', api_key='test_key'))
    initial_metrics = copy.deepcopy(llm.metrics)
    initial_metrics.add_cost(1.0)
    initial_metrics.add_response_latency(0.5, 'test-id')
    llm.reset()
    assert llm.metrics._accumulated_cost != initial_metrics._accumulated_cost
    assert llm.metrics._costs != initial_metrics._costs
    assert llm.metrics._response_latencies != initial_metrics._response_latencies
    assert isinstance(llm.metrics, Metrics)


@patch('openhands.llm.llm.litellm.get_model_info')
def test_llm_init_with_openrouter_model(mock_get_model_info, default_config):
    default_config.model = 'openrouter:gpt-4o-mini'
    mock_get_model_info.return_value = {
        'max_input_tokens': 7000,
        'max_output_tokens': 1500,
    }
    llm = LLM(default_config)
    llm.init_model_info()
    assert llm.config.max_input_tokens == 7000
    assert llm.config.max_output_tokens == 1500
    mock_get_model_info.assert_called_once_with('openrouter:gpt-4o-mini')


# Tests involving completion and retries


@patch('openhands.llm.llm.litellm_completion')
def test_completion_with_mocked_logger(
    mock_litellm_completion, default_config, mock_logger
):
    mock_litellm_completion.return_value = {
        'choices': [{'message': {'content': 'Test response'}}]
    }

    llm = LLM(config=default_config)
    response = llm.completion(
        messages=[{'role': 'user', 'content': 'Hello!'}],
        stream=False,
    )

    assert response['choices'][0]['message']['content'] == 'Test response'
    assert mock_litellm_completion.call_count == 1

    mock_logger.debug.assert_called()


@pytest.mark.parametrize(
    'exception_class,extra_args,expected_retries',
    [
        (
            APIConnectionError,
            {'llm_provider': 'test_provider', 'model': 'test_model'},
            2,
        ),
        (
            InternalServerError,
            {'llm_provider': 'test_provider', 'model': 'test_model'},
            2,
        ),
        (
            ServiceUnavailableError,
            {'llm_provider': 'test_provider', 'model': 'test_model'},
            2,
        ),
        (RateLimitError, {'llm_provider': 'test_provider', 'model': 'test_model'}, 2),
    ],
)
@patch('openhands.llm.llm.litellm_completion')
def test_completion_retries(
    mock_litellm_completion,
    default_config,
    exception_class,
    extra_args,
    expected_retries,
):
    mock_litellm_completion.side_effect = [
        exception_class('Test error message', **extra_args),
        {'choices': [{'message': {'content': 'Retry successful'}}]},
    ]

    llm = LLM(config=default_config)
    response = llm.completion(
        messages=[{'role': 'user', 'content': 'Hello!'}],
        stream=False,
    )

    assert response['choices'][0]['message']['content'] == 'Retry successful'
    assert mock_litellm_completion.call_count == expected_retries


@patch('openhands.llm.llm.litellm_completion')
def test_completion_rate_limit_wait_time(mock_litellm_completion, default_config):
    with patch('time.sleep') as mock_sleep:
        mock_litellm_completion.side_effect = [
            RateLimitError(
                'Rate limit exceeded', llm_provider='test_provider', model='test_model'
            ),
            {'choices': [{'message': {'content': 'Retry successful'}}]},
        ]

        llm = LLM(config=default_config)
        response = llm.completion(
            messages=[{'role': 'user', 'content': 'Hello!'}],
            stream=False,
        )

        assert response['choices'][0]['message']['content'] == 'Retry successful'
        assert mock_litellm_completion.call_count == 2

        mock_sleep.assert_called_once()
        wait_time = mock_sleep.call_args[0][0]
        assert (
            default_config.retry_min_wait <= wait_time <= default_config.retry_max_wait
        ), f'Expected wait time between {default_config.retry_min_wait} and {default_config.retry_max_wait} seconds, but got {wait_time}'


@patch('openhands.llm.llm.litellm_completion')
def test_completion_exhausts_retries(mock_litellm_completion, default_config):
    mock_litellm_completion.side_effect = APIConnectionError(
        'Persistent error', llm_provider='test_provider', model='test_model'
    )

    llm = LLM(config=default_config)
    with pytest.raises(APIConnectionError):
        llm.completion(
            messages=[{'role': 'user', 'content': 'Hello!'}],
            stream=False,
        )

    assert mock_litellm_completion.call_count == llm.config.num_retries


@patch('openhands.llm.llm.litellm_completion')
def test_completion_operation_cancelled(mock_litellm_completion, default_config):
    mock_litellm_completion.side_effect = OperationCancelled('Operation cancelled')

    llm = LLM(config=default_config)
    with pytest.raises(OperationCancelled):
        llm.completion(
            messages=[{'role': 'user', 'content': 'Hello!'}],
            stream=False,
        )

    assert mock_litellm_completion.call_count == 1


@patch('openhands.llm.llm.litellm_completion')
def test_completion_keyboard_interrupt(mock_litellm_completion, default_config):
    def side_effect(*args, **kwargs):
        raise KeyboardInterrupt('Simulated KeyboardInterrupt')

    mock_litellm_completion.side_effect = side_effect

    llm = LLM(config=default_config)
    with pytest.raises(OperationCancelled):
        try:
            llm.completion(
                messages=[{'role': 'user', 'content': 'Hello!'}],
                stream=False,
            )
        except KeyboardInterrupt:
            raise OperationCancelled('Operation cancelled due to KeyboardInterrupt')

    assert mock_litellm_completion.call_count == 1


@patch('openhands.llm.llm.litellm_completion')
def test_completion_keyboard_interrupt_handler(mock_litellm_completion, default_config):
    global _should_exit

    def side_effect(*args, **kwargs):
        global _should_exit
        _should_exit = True
        return {'choices': [{'message': {'content': 'Simulated interrupt response'}}]}

    mock_litellm_completion.side_effect = side_effect

    llm = LLM(config=default_config)
    result = llm.completion(
        messages=[{'role': 'user', 'content': 'Hello!'}],
        stream=False,
    )

    assert mock_litellm_completion.call_count == 1
    assert result['choices'][0]['message']['content'] == 'Simulated interrupt response'
    assert _should_exit

    _should_exit = False


@patch('openhands.llm.llm.litellm_completion')
def test_completion_with_litellm_mock(mock_litellm_completion, default_config):
    mock_response = {
        'choices': [{'message': {'content': 'This is a mocked response.'}}]
    }
    mock_litellm_completion.return_value = mock_response

    test_llm = LLM(config=default_config)
    response = test_llm.completion(
        messages=[{'role': 'user', 'content': 'Hello!'}],
        stream=False,
        drop_params=True,
    )

    # Assertions
    assert response['choices'][0]['message']['content'] == 'This is a mocked response.'
    mock_litellm_completion.assert_called_once()

    # Check if the correct arguments were passed to litellm_completion
    call_args = mock_litellm_completion.call_args[1]  # Get keyword arguments
    assert call_args['model'] == default_config.model
    assert call_args['messages'] == [{'role': 'user', 'content': 'Hello!'}]
    assert not call_args['stream']


@patch('openhands.llm.llm.litellm_completion')
def test_completion_with_two_positional_args(mock_litellm_completion, default_config):
    mock_response = {
        'choices': [{'message': {'content': 'Response to positional args.'}}]
    }
    mock_litellm_completion.return_value = mock_response

    test_llm = LLM(config=default_config)
    response = test_llm.completion(
        'some-model-to-be-ignored',
        [{'role': 'user', 'content': 'Hello from positional args!'}],
        stream=False,
    )

    # Assertions
    assert (
        response['choices'][0]['message']['content'] == 'Response to positional args.'
    )
    mock_litellm_completion.assert_called_once()

    # Check if the correct arguments were passed to litellm_completion
    call_args, call_kwargs = mock_litellm_completion.call_args
    assert (
        call_kwargs['model'] == default_config.model
    )  # Should use the model from config, not the first arg
    assert call_kwargs['messages'] == [
        {'role': 'user', 'content': 'Hello from positional args!'}
    ]
    assert not call_kwargs['stream']

    # Ensure the first positional argument (model) was ignored
    assert (
        len(call_args) == 0
    )  # No positional args should be passed to litellm_completion here


@patch('openhands.llm.llm.litellm_completion')
def test_llm_cloudflare_blockage(mock_litellm_completion, default_config):
    from litellm.exceptions import APIError

    from openhands.core.exceptions import CloudFlareBlockageError

    llm = LLM(default_config)
    mock_litellm_completion.side_effect = APIError(
        message='Attention Required! | Cloudflare',
        llm_provider='test_provider',
        model='test_model',
        status_code=403,
    )

    with pytest.raises(CloudFlareBlockageError, match='Request blocked by CloudFlare'):
        llm.completion(messages=[{'role': 'user', 'content': 'Hello'}])

    # Ensure the completion was called
    mock_litellm_completion.assert_called_once()


@patch('openhands.llm.llm.litellm_completion')
def test_token_count_with_usage_data(mock_litellm_completion, default_config):
    # Mock a response with usage data
    from litellm import Message as LiteLLMMessage
    from litellm.types.utils import Choices, ModelResponse, Usage

    mock_response = ModelResponse(
        id='test-id',
        choices=[
            Choices(
                message=LiteLLMMessage(role='assistant', content='Test response'),
                finish_reason='stop',
                index=0,
            )
        ],
        created=1234567890,
        model='test-model',
        object='chat.completion',
        usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
    )
    mock_litellm_completion.return_value = mock_response

    llm = LLM(default_config)
    response = llm.completion(
        messages=[Message(role='user', content=[TextContent(text='Hello!')])]
    )

    # Verify the response has usage data
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 20
    assert response.usage.total_tokens == 30

    # Verify get_token_count uses the token counts from Usage data
    # The message from the response should have the token counts stored
    assert response.choices[0].message.total_tokens == 30
    assert response.choices[0].message.prompt_tokens == 10
    assert response.choices[0].message.completion_tokens == 20

    # Now verify get_token_count uses these stored token counts
    token_count = llm.get_token_count([response.choices[0].message])
    assert token_count == 30


@patch('openhands.llm.llm.litellm_completion')
@patch('openhands.llm.llm.litellm.utils._select_tokenizer')
@patch('openhands.llm.llm.litellm.token_counter')
def test_token_count_fallback(
    mock_token_counter, mock_select_tokenizer, mock_litellm_completion, default_config
):
    # Mock a response without usage data
    from litellm import Message as LiteLLMMessage
    from litellm.types.utils import Choices, ModelResponse, Usage

    mock_response = ModelResponse(
        id='test-id',
        choices=[
            Choices(
                message=LiteLLMMessage(role='assistant', content='Test response'),
                finish_reason='stop',
                index=0,
            )
        ],
        created=1234567890,
        model='test-model',
        object='chat.completion',
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    )
    mock_litellm_completion.return_value = mock_response
    mock_token_counter.return_value = 42
    mock_select_tokenizer.return_value = {'type': 'openai_tokenizer'}

    llm = LLM(default_config)
    response = llm.completion(
        messages=[Message(role='user', content=[TextContent(text='Hello!')])]
    )

    # Verify the response has empty usage data
    assert response.usage.prompt_tokens == 0
    assert response.usage.completion_tokens == 0
    assert response.usage.total_tokens == 0

    # Verify get_token_count falls back to litellm.token_counter
    messages = [Message(role='user', content=[TextContent(text='Hello!')])]
    token_count = llm.get_token_count(messages)
    assert token_count == 42
    # litellm expects a list of dicts with role and content
    mock_token_counter.assert_called_once_with(
        model=default_config.model,
        messages=[{'role': 'user', 'content': [{'type': 'text', 'text': 'Hello!'}]}],
    )
