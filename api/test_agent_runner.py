from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from api.agent_views import (
    _augment_tool_args_from_query,
    _extract_cash_budget_from_query,
    _extract_owned_positions_from_query,
    handle_tool_call,
    run_agent,
)
from api.entitlements import get_plan_context
from api.models import AgentRun
from api.tasks import run_agent_run


class RunAgentTests(TestCase):
    def create_user(self, **overrides):
        defaults = {
            "username": "runner-user",
            "password": "test-pass-123",
            "email": "runner@example.com",
        }
        defaults.update(overrides)
        password = defaults.pop("password")
        user = get_user_model().objects.create_user(**defaults)
        user.set_password(password)
        user.save(update_fields=["password"])
        return user

    def test_extract_owned_positions_from_query_parses_cost_basis_and_shared_shares(self) -> None:
        positions = _extract_owned_positions_from_query(
            "I own B at 42$, PLTR at 70, 100 shares each"
        )

        self.assertEqual(
            positions,
            [
                {"symbol": "B", "cost_basis": 42.0, "shares_owned": 100},
                {"symbol": "PLTR", "cost_basis": 70.0, "shares_owned": 100},
            ],
        )

    def test_augment_tool_args_from_query_applies_follow_up_max_price_filter(self) -> None:
        augmented_args = _augment_tool_args_from_query(
            "scan_put_opportunities",
            {"limit": 10},
            "Provide the companies below 150$",
        )

        self.assertEqual(
            augmented_args,
            {
                "limit": 10,
                "max_price": 150.0,
            },
        )

    def test_augment_tool_args_from_query_applies_oversold_rsi_filter(self) -> None:
        augmented_args = _augment_tool_args_from_query(
            "scan_put_opportunities",
            {"limit": 10},
            "Provide put candidates on oversold companies",
        )

        self.assertEqual(
            augmented_args,
            {
                "limit": 10,
                "max_rsi": 30.0,
            },
        )

    def test_augment_tool_args_from_query_applies_overbought_rsi_filter(self) -> None:
        augmented_args = _augment_tool_args_from_query(
            "scan_put_opportunities",
            {"limit": 10},
            "Provide put candidates on overbough companies",
        )

        self.assertEqual(
            augmented_args,
            {
                "limit": 10,
                "min_rsi": 75.0,
            },
        )

    def test_extract_cash_budget_from_query_parses_cash_account_phrase(self) -> None:
        budget = _extract_cash_budget_from_query(
            "suggest CSPs for my 20000$ cash account"
        )

        self.assertEqual(
            budget,
            {"account_size": 20000.0},
        )

    def test_augment_tool_args_from_query_reuses_previous_scan_pagination_for_show_more(self) -> None:
        augmented_args = _augment_tool_args_from_query(
            "scan_put_opportunities",
            {"limit": 10},
            "show more",
            history=[
                {
                    "role": "meta",
                    "content": {
                        "type": "tool_state",
                        "tools": {
                            "scan_put_opportunities": {
                                "base_arguments": {
                                    "limit": 10,
                                    "max_price": 200.0,
                                },
                                "limit": 10,
                                "offset": 0,
                                "next_offset": 10,
                                "total_results_available": 26,
                                "shown_tickers": ["ON", "FUTU"],
                            }
                        },
                    },
                }
            ],
        )

        self.assertEqual(
            augmented_args,
            {
                "limit": 10,
                "max_price": 200.0,
                "offset": 10,
            },
        )

    def test_augment_tool_args_from_query_leaves_other_tools_unchanged(self) -> None:
        tool_args = {"limit": 10}

        self.assertEqual(
            _augment_tool_args_from_query(
                "compare_put_candidates",
                tool_args,
                "Provide the companies below 150$",
            ),
            tool_args,
        )

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
        response_payload.usage = {
            "prompt_tokens": 120,
            "completion_tokens": 30,
        }

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = response_payload
        mock_openai.return_value = mock_client

        result = run_agent("Analyze AAPL", [{"role": "assistant", "content": "Earlier"}])

        self.assertEqual(result["answer"], "Test answer")
        self.assertEqual(result["history"][-1]["content"], "Test answer")
        self.assertEqual(result["history"][-2]["content"], "Analyze AAPL")
        self.assertEqual(result["used_tools"], [])
        self.assertEqual(
            result["llm_usage"],
            [
                {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "prompt_key": "agent_chat",
                    "iteration": 1,
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "web_search_calls": 0,
                    "estimated_cost_usd": 0.000036,
                }
            ],
        )
        self.assertEqual(result["llm_usage_summary"]["estimated_cost_usd"], 3.6e-05)
        mock_client.chat.completions.create.assert_called_once()
        _, kwargs = mock_openai.call_args
        self.assertEqual(kwargs["timeout"], 45)
        self.assertEqual(kwargs["max_retries"], 1)

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.OpenAI")
    def test_run_agent_sends_off_topic_queries_to_model_with_scope_prompt(
        self,
        mock_openai: MagicMock,
    ) -> None:
        message = MagicMock()
        message.tool_calls = None
        message.content = "PutPulse focuses on stocks and options, not cooking."

        response_payload = MagicMock()
        response_payload.choices = [MagicMock(message=message)]
        response_payload.usage = {
            "prompt_tokens": 120,
            "completion_tokens": 30,
        }

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = response_payload
        mock_openai.return_value = mock_client

        result = run_agent("What is the best pasta recipe?", [])

        self.assertEqual(
            result["answer"],
            "PutPulse focuses on stocks and options, not cooking.",
        )
        self.assertEqual(
            result["history"][-1]["content"],
            "PutPulse focuses on stocks and options, not cooking.",
        )
        self.assertEqual(result["history"][-2]["content"], "What is the best pasta recipe?")
        self.assertEqual(result["used_tools"], [])
        mock_client.chat.completions.create.assert_called_once()
        call_messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
        self.assertIn("Scope handling:", call_messages[0]["content"])
        self.assertIn("clearly unrelated", call_messages[0]["content"])

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.OpenAI")
    def test_run_agent_short_circuits_simple_social_queries(
        self,
        mock_openai: MagicMock,
    ) -> None:
        result = run_agent("Thanks", [])

        self.assertEqual(result["answer"], "You're welcome.")
        self.assertEqual(result["history"][-1]["content"], "You're welcome.")
        self.assertEqual(result["history"][-2]["content"], "Thanks")
        self.assertEqual(result["used_tools"], [])
        self.assertEqual(result["llm_usage"], [])
        self.assertEqual(result["llm_usage_summary"]["estimated_cost_usd"], 0.0)
        mock_openai.assert_not_called()

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
        AGENT_MAX_QUERY_CHARS=5,
    )
    @patch("api.agent_views.OpenAI")
    def test_run_agent_short_circuits_overly_long_queries(
        self,
        mock_openai: MagicMock,
    ) -> None:
        result = run_agent("Analyze AAPL", [])

        self.assertEqual(
            result["answer"],
            "Your message is too long. Please shorten it and focus on one investing question about stocks, options, or fundamentals.",
        )
        self.assertEqual(result["history"][-2]["content"], "Analyze AAPL")
        self.assertEqual(result["used_tools"], [])
        self.assertEqual(result["llm_usage"], [])
        self.assertEqual(result["llm_usage_summary"]["estimated_cost_usd"], 0.0)
        mock_openai.assert_not_called()

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.OpenAI")
    def test_run_agent_uses_free_model_and_trims_history_for_free_users(
        self,
        mock_openai: MagicMock,
    ) -> None:
        user = self.create_user(username="free-user")

        message = MagicMock()
        message.tool_calls = None
        message.content = "Free answer"

        response_payload = MagicMock()
        response_payload.choices = [MagicMock(message=message)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = response_payload
        mock_openai.return_value = mock_client

        history = [
            {"role": "assistant", "content": f"Earlier {index}"}
            for index in range(12)
        ]
        result = run_agent("Analyze AAPL", history, user=user)

        self.assertEqual(result["answer"], "Free answer")
        create_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(create_kwargs["model"], "gpt-4.1-mini")
        self.assertEqual(len(create_kwargs["messages"]), 10)
        self.assertEqual(create_kwargs["messages"][1]["content"], "Earlier 4")
        self.assertEqual(create_kwargs["messages"][-2]["content"], "Earlier 11")

    def test_handle_tool_call_blocks_analyze_stock_after_free_daily_limit(self) -> None:
        user = self.create_user(username="free-analyze")

        AgentRun.objects.create(
            user=user,
            query="Analyze MSFT",
            history_json=[],
            used_tools_json=[
                {"name": "analyze_stock", "arguments": {"symbol": "MSFT"}, "iteration": 1}
            ],
        )

        payload = handle_tool_call(
            "analyze_stock",
            {"symbol": "AAPL"},
            user=user,
            plan_context=get_plan_context(user),
        )

        self.assertIn("daily_analyze_stock_limit_reached", payload)

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.OpenAI")
    def test_run_agent_appends_structured_holdings_context_to_user_message(
        self,
        mock_openai: MagicMock,
    ) -> None:
        message = MagicMock()
        message.tool_calls = None
        message.content = "Test answer"

        response_payload = MagicMock()
        response_payload.choices = [MagicMock(message=message)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = response_payload
        mock_openai.return_value = mock_client

        query = "Build a monthly income plan. I own B at 42$, PLTR at 70, 100 shares each"
        result = run_agent(query, [])

        self.assertEqual(result["history"][-2]["content"], query)
        call_messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
        user_message = call_messages[-1]["content"]
        self.assertIn("Structured holdings extracted from the user's message:", user_message)
        self.assertIn("symbol=B; cost_basis=42.0; shares_owned=100", user_message)
        self.assertIn("symbol=PLTR; cost_basis=70.0; shares_owned=100", user_message)
        self.assertIn("current stock price must come from tool data", user_message)

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.OpenAI")
    def test_run_agent_appends_structured_price_filter_context_to_user_message(
        self,
        mock_openai: MagicMock,
    ) -> None:
        message = MagicMock()
        message.tool_calls = None
        message.content = "Test answer"

        response_payload = MagicMock()
        response_payload.choices = [MagicMock(message=message)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = response_payload
        mock_openai.return_value = mock_client

        run_agent("Provide the companies below 150$", [])

        call_messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
        user_message = call_messages[-1]["content"]
        self.assertIn("Structured screener filters extracted from the user's message:", user_message)
        self.assertIn("max_price=150.0", user_message)
        self.assertIn("apply these underlying price filters as hard tool arguments", user_message)

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.OpenAI")
    def test_run_agent_accepts_plural_csps_with_cash_account_budget(
        self,
        mock_openai: MagicMock,
    ) -> None:
        message = MagicMock()
        message.tool_calls = None
        message.content = "Test answer"

        response_payload = MagicMock()
        response_payload.choices = [MagicMock(message=message)]
        response_payload.usage = {
            "prompt_tokens": 120,
            "completion_tokens": 30,
        }

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = response_payload
        mock_openai.return_value = mock_client

        query = "suggest CSPs for my 20000$ cash account"
        result = run_agent(query, [])

        self.assertEqual(result["answer"], "Test answer")
        call_messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
        user_message = call_messages[-1]["content"]
        self.assertIn("Structured budget extracted from the user's message:", user_message)
        self.assertIn("account_size=20000.0", user_message)

    @override_settings(
        AGENT_MODEL_PROVIDER="gemini",
        AGENT_MODEL="gemini-2.5-flash",
        GEMINI_API_KEY="test-gemini-key",
        GEMINI_OPENAI_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    @patch("api.agent_views.OpenAI")
    def test_run_agent_returns_answer_with_gemini_openai_compat(
        self,
        mock_openai: MagicMock,
    ) -> None:
        message = MagicMock()
        message.tool_calls = None
        message.content = "Gemini answer"

        response_payload = MagicMock()
        response_payload.choices = [MagicMock(message=message)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = response_payload
        mock_openai.return_value = mock_client

        result = run_agent("Analyze AAPL", [{"role": "assistant", "content": "Earlier"}])

        self.assertEqual(result["answer"], "Gemini answer")
        self.assertEqual(result["history"][-1]["content"], "Gemini answer")
        mock_client.chat.completions.create.assert_called_once()
        _, kwargs = mock_openai.call_args
        self.assertEqual(kwargs["api_key"], "test-gemini-key")
        self.assertEqual(
            kwargs["base_url"],
            "https://generativelanguage.googleapis.com/v1beta/openai/",
        )
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

        result = run_agent("Analyze AAPL", [{"role": "assistant", "content": "Earlier"}])

        self.assertEqual(result["answer"], "Anthropic answer")
        self.assertEqual(result["history"][-1]["content"], "Anthropic answer")
        self.assertEqual(result["history"][-2]["content"], "Analyze AAPL")
        self.assertEqual(result["used_tools"], [])
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
        self.assertEqual(
            result["used_tools"],
            [{"name": "analyze_stock", "arguments": {"symbol": "AAPL"}, "iteration": 1}],
        )
        self.assertEqual(mock_client.messages.create.call_count, 2)
        self.assertEqual(
            mock_handle_tool_call.call_args.args,
            ("analyze_stock", {"symbol": "AAPL"}),
        )
        self.assertEqual(mock_handle_tool_call.call_args.kwargs["user"], None)
        self.assertEqual(mock_handle_tool_call.call_args.kwargs["plan_context"]["plan"], "pro")

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
        self.assertEqual(
            result["used_tools"],
            [{"name": "analyze_stock", "arguments": {"symbol": "AAPL"}, "iteration": 1}],
        )
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        self.assertEqual(
            mock_handle_tool_call.call_args.args,
            ("analyze_stock", {"symbol": "AAPL"}),
        )
        self.assertEqual(mock_handle_tool_call.call_args.kwargs["user"], None)
        self.assertEqual(mock_handle_tool_call.call_args.kwargs["plan_context"]["plan"], "pro")

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.handle_tool_call", return_value='{"ok": true}')
    @patch("api.agent_views.OpenAI")
    def test_run_agent_applies_follow_up_price_filter_to_scan_put_tool_calls(
        self,
        mock_openai: MagicMock,
        mock_handle_tool_call: MagicMock,
    ) -> None:
        tool_call = MagicMock()
        tool_call.id = "call_1"
        tool_call.function.name = "scan_put_opportunities"
        tool_call.function.arguments = '{"limit": 5}'

        first_message = MagicMock()
        first_message.tool_calls = [tool_call]
        first_message.model_dump.return_value = {"role": "assistant", "content": None}

        second_message = MagicMock()
        second_message.tool_calls = None
        second_message.content = "Filtered answer"

        first_response = MagicMock()
        first_response.choices = [MagicMock(message=first_message)]
        second_response = MagicMock()
        second_response.choices = [MagicMock(message=second_message)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [first_response, second_response]
        mock_openai.return_value = mock_client

        result = run_agent("Provide the companies below 150$", [])

        self.assertEqual(result["answer"], "Filtered answer")
        self.assertEqual(
            mock_handle_tool_call.call_args.args,
            (
                "scan_put_opportunities",
                {
                    "limit": 5,
                    "max_price": 150.0,
                },
            ),
        )

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.handle_tool_call", return_value='{"ok": true}')
    @patch("api.agent_views.OpenAI")
    def test_run_agent_applies_oversold_rsi_filter_to_scan_put_tool_calls(
        self,
        mock_openai: MagicMock,
        mock_handle_tool_call: MagicMock,
    ) -> None:
        tool_call = MagicMock()
        tool_call.id = "call_1"
        tool_call.function.name = "scan_put_opportunities"
        tool_call.function.arguments = '{"limit": 5}'

        first_message = MagicMock()
        first_message.tool_calls = [tool_call]
        first_message.model_dump.return_value = {"role": "assistant", "content": None}

        second_message = MagicMock()
        second_message.tool_calls = None
        second_message.content = "Filtered answer"

        first_response = MagicMock()
        first_response.choices = [MagicMock(message=first_message)]
        second_response = MagicMock()
        second_response.choices = [MagicMock(message=second_message)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [first_response, second_response]
        mock_openai.return_value = mock_client

        result = run_agent("Provide put candidates on oversold companies", [])

        self.assertEqual(result["answer"], "Filtered answer")
        self.assertEqual(
            mock_handle_tool_call.call_args.args,
            (
                "scan_put_opportunities",
                {
                    "limit": 5,
                    "max_rsi": 30.0,
                },
            ),
        )

    @override_settings(
        AGENT_MODEL_PROVIDER="openai",
        AGENT_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-openai-key",
    )
    @patch("api.agent_views.handle_tool_call", return_value='{"ok": true}')
    @patch("api.agent_views.OpenAI")
    def test_run_agent_reuses_previous_scan_offset_for_show_more(
        self,
        mock_openai: MagicMock,
        mock_handle_tool_call: MagicMock,
    ) -> None:
        tool_call = MagicMock()
        tool_call.id = "call_1"
        tool_call.function.name = "scan_put_opportunities"
        tool_call.function.arguments = '{"limit": 10}'

        first_message = MagicMock()
        first_message.tool_calls = [tool_call]
        first_message.model_dump.return_value = {"role": "assistant", "content": None}

        second_message = MagicMock()
        second_message.tool_calls = None
        second_message.content = "More results"

        first_response = MagicMock()
        first_response.choices = [MagicMock(message=first_message)]
        second_response = MagicMock()
        second_response.choices = [MagicMock(message=second_message)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [first_response, second_response]
        mock_openai.return_value = mock_client

        result = run_agent(
            "show more",
            [
                {
                    "role": "meta",
                    "content": {
                        "type": "tool_state",
                        "tools": {
                            "scan_put_opportunities": {
                                "base_arguments": {
                                    "limit": 10,
                                    "max_price": 200.0,
                                },
                                "limit": 10,
                                "offset": 0,
                                "next_offset": 10,
                                "total_results_available": 26,
                                "shown_tickers": ["ON", "FUTU"],
                            }
                        },
                    },
                },
                {"role": "assistant", "content": "Previous answer"},
            ],
        )

        self.assertEqual(result["answer"], "More results")
        self.assertEqual(
            mock_handle_tool_call.call_args.args,
            (
                "scan_put_opportunities",
                {
                    "limit": 10,
                    "max_price": 200.0,
                    "offset": 10,
                },
            ),
        )

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
            run_agent("Analyze AAPL", [])

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
            "used_tools": [{"name": "analyze_stock", "arguments": {"symbol": "AAPL"}, "iteration": 1}],
            "llm_usage": [
                {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "prompt_key": "agent_chat",
                    "iteration": 1,
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "web_search_calls": 0,
                    "estimated_cost_usd": 0.000036,
                }
            ],
            "llm_usage_summary": {"estimated_cost_usd": 3.6e-05},
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
        self.assertEqual(
            agent_run.used_tools_json,
            [{"name": "analyze_stock", "arguments": {"symbol": "AAPL"}, "iteration": 1}],
        )
        self.assertEqual(
            agent_run.llm_usage_json,
            [
                {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "prompt_key": "agent_chat",
                    "iteration": 1,
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "web_search_calls": 0,
                    "estimated_cost_usd": 0.000036,
                }
            ],
        )
        self.assertEqual(agent_run.llm_usage_summary_json["estimated_cost_usd"], 3.6e-05)
        self.assertIsNotNone(agent_run.started_at)
        self.assertIsNotNone(agent_run.finished_at)
        mock_run_agent.assert_called_once_with(
            agent_run.query,
            agent_run.history_json,
            agent_run_id=agent_run.id,
            user=agent_run.user,
        )

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
        self.assertEqual(agent_run.used_tools_json, [])
        self.assertEqual(agent_run.llm_usage_json, [])
        self.assertEqual(agent_run.llm_usage_summary_json, {})
        self.assertIsNotNone(agent_run.started_at)
        self.assertIsNotNone(agent_run.finished_at)
