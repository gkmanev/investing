from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from api.agent_views import run_agent
from api.models import AgentRun
from api.tasks import run_agent_run


class RunAgentTests(TestCase):
    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.OpenAI")
    def test_run_agent_returns_answer_and_history(self, mock_openai: MagicMock) -> None:
        message = MagicMock()
        message.tool_calls = None
        message.content = "Test answer"

        response_payload = MagicMock()
        response_payload.choices = [MagicMock(message=message)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = response_payload
        mock_openai.return_value = mock_client

        result = run_agent("Hello", [{"role": "assistant", "content": "Earlier"}])

        self.assertEqual(result["answer"], "Test answer")
        self.assertEqual(result["history"][-1]["content"], "Test answer")
        self.assertEqual(result["history"][-2]["content"], "Hello")
        mock_client.chat.completions.create.assert_called_once()
        _, kwargs = mock_openai.call_args
        self.assertEqual(kwargs["timeout"], 45)
        self.assertEqual(kwargs["max_retries"], 1)

    @override_settings(
        AGENT_MODEL_PROVIDER="anthropic",
        AGENT_MODEL="claude-sonnet-4-5",
        ANTHROPIC_API_KEY="test-anthropic-key",
        AGENT_ANTHROPIC_MAX_TOKENS=2048,
    )
    @patch("api.agent_views.Anthropic")
    def test_run_agent_returns_answer_with_anthropic(
        self,
        mock_anthropic: MagicMock,
    ) -> None:
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Anthropic answer"

        response_payload = MagicMock()
        response_payload.content = [text_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = response_payload
        mock_anthropic.return_value = mock_client

        result = run_agent("Hello", [{"role": "assistant", "content": "Earlier"}])

        self.assertEqual(result["answer"], "Anthropic answer")
        self.assertEqual(result["history"][-1]["content"], "Anthropic answer")
        self.assertEqual(result["history"][-2]["content"], "Hello")
        mock_client.messages.create.assert_called_once()
        _, kwargs = mock_anthropic.call_args
        self.assertEqual(kwargs["timeout"], 45)

    @override_settings(
        AGENT_MODEL_PROVIDER="anthropic",
        AGENT_MODEL="claude-sonnet-4-5",
        ANTHROPIC_API_KEY="test-anthropic-key",
        AGENT_ANTHROPIC_MAX_TOKENS=2048,
    )
    @patch("api.agent_views.handle_tool_call", return_value='{"ok": true}')
    @patch("api.agent_views.Anthropic")
    def test_run_agent_executes_anthropic_tool_calls_before_returning_answer(
        self,
        mock_anthropic: MagicMock,
        mock_handle_tool_call: MagicMock,
    ) -> None:
        tool_use = MagicMock()
        tool_use.type = "tool_use"
        tool_use.id = "toolu_1"
        tool_use.name = "analyze_stock"
        tool_use.input = {"symbol": "AAPL"}

        first_response = MagicMock()
        first_response.content = [tool_use]

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Final answer"

        second_response = MagicMock()
        second_response.content = [text_block]

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [first_response, second_response]
        mock_anthropic.return_value = mock_client

        result = run_agent("Analyze AAPL", [])

        self.assertEqual(result["answer"], "Final answer")
        self.assertEqual(mock_client.messages.create.call_count, 2)
        mock_handle_tool_call.assert_called_once_with("analyze_stock", {"symbol": "AAPL"})

        second_call_messages = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        self.assertEqual(second_call_messages[-1]["role"], "user")
        self.assertEqual(
            second_call_messages[-1]["content"][0]["tool_use_id"],
            "toolu_1",
        )

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.handle_tool_call", return_value='{"ok": true}')
    @patch("api.agent_views.OpenAI")
    def test_run_agent_executes_tool_calls_before_returning_answer(
        self,
        mock_openai: MagicMock,
        mock_handle_tool_call: MagicMock,
    ) -> None:
        tool_call = MagicMock()
        tool_call.id = "call_1"
        tool_call.function.name = "analyze_stock"
        tool_call.function.arguments = '{"symbol": "AAPL"}'

        first_message = MagicMock()
        first_message.tool_calls = [tool_call]
        first_message.model_dump.return_value = {"role": "assistant", "content": None}

        second_message = MagicMock()
        second_message.tool_calls = None
        second_message.content = "Final answer"

        first_response = MagicMock()
        first_response.choices = [MagicMock(message=first_message)]
        second_response = MagicMock()
        second_response.choices = [MagicMock(message=second_message)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [first_response, second_response]
        mock_openai.return_value = mock_client

        result = run_agent("Analyze AAPL", [])

        self.assertEqual(result["answer"], "Final answer")
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        mock_handle_tool_call.assert_called_once_with("analyze_stock", {"symbol": "AAPL"})

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @override_settings(AGENT_MAX_ITERATIONS=2)
    @patch("api.agent_views.OpenAI")
    def test_run_agent_enforces_max_iterations(self, mock_openai: MagicMock) -> None:
        tool_call = MagicMock()
        tool_call.id = "call_1"
        tool_call.function.name = "analyze_stock"
        tool_call.function.arguments = '{"symbol": "AAPL"}'

        looping_message = MagicMock()
        looping_message.tool_calls = [tool_call]
        looping_message.model_dump.return_value = {"role": "assistant", "content": None}

        looping_response = MagicMock()
        looping_response.choices = [MagicMock(message=looping_message)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [looping_response, looping_response]
        mock_openai.return_value = mock_client

        with self.assertRaises(RuntimeError) as exc:
            run_agent("Analyze AAPL", [])

        self.assertIn("maximum iterations (2)", str(exc.exception))

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
        AGENT_OPENAI_TIMEOUT_SECONDS=5,
        AGENT_OVERALL_TIMEOUT_SECONDS=7,
        AGENT_OPENAI_MAX_RETRIES=3,
    )
    @patch("api.agent_views.time.monotonic", side_effect=[0, 8])
    @patch("api.agent_views.OpenAI")
    def test_run_agent_enforces_overall_timeout_before_request(
        self,
        mock_openai: MagicMock,
        mock_monotonic: MagicMock,
    ) -> None:
        with self.assertRaises(RuntimeError) as exc:
            run_agent("Hello", [])

        self.assertIn("overall timeout (7s)", str(exc.exception))
        mock_openai.return_value.chat.completions.create.assert_not_called()
        _, kwargs = mock_openai.call_args
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["max_retries"], 3)


class AgentRunTaskTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="agent-user",
            password="test-pass-123",
        )

    @patch("api.agent_views.run_agent")
    def test_run_agent_run_marks_completed_and_stores_result(
        self,
        mock_run_agent: MagicMock,
    ) -> None:
        mock_run_agent.return_value = {
            "answer": "Stored answer",
            "history": [{"role": "assistant", "content": "Stored answer"}],
        }
        agent_run = AgentRun.objects.create(
            user=self.user,
            query="Hello",
            history_json=[],
        )

        run_agent_run(agent_run.id)

        agent_run.refresh_from_db()
        self.assertEqual(agent_run.status, AgentRun.Status.COMPLETED)
        self.assertEqual(agent_run.result_text, "Stored answer")
        self.assertEqual(agent_run.error_text, "")
        self.assertIsNotNone(agent_run.started_at)
        self.assertIsNotNone(agent_run.finished_at)

    @patch("api.agent_views.run_agent", side_effect=RuntimeError("boom"))
    def test_run_agent_run_marks_failed_and_stores_error(
        self,
        mock_run_agent: MagicMock,
    ) -> None:
        agent_run = AgentRun.objects.create(
            user=self.user,
            query="Hello",
            history_json=[],
        )

        with self.assertRaises(RuntimeError):
            run_agent_run(agent_run.id)

        agent_run.refresh_from_db()
        self.assertEqual(agent_run.status, AgentRun.Status.FAILED)
        self.assertEqual(agent_run.error_text, "boom")
        self.assertEqual(agent_run.result_text, "")
        self.assertIsNotNone(agent_run.started_at)
        self.assertIsNotNone(agent_run.finished_at)
