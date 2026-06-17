from django.test import TestCase

from api import agent_request, agent_response
from api.models import Symbol


class AgentResponseTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        for ticker in ["TSLA", "AMD", "AAPL"]:
            Symbol.objects.create(ticker=ticker)

    def test_validate_answer_flags_missing_requested_symbol(self) -> None:
        context = agent_request.build_request_context("Show me put ideas for TSLA")

        validation = agent_response.validate_answer(
            answer="This put setup looks attractive based on the available data.",
            context=context,
            tool_name="get_put_wheel_opportunity",
            tool_result='{"symbol": "TSLA"}',
        )

        self.assertFalse(validation.is_valid)
        self.assertIn("TSLA", " ".join(validation.reasons))

    def test_validate_answer_flags_unrelated_known_symbol(self) -> None:
        context = agent_request.build_request_context("Show me put ideas for TSLA")

        validation = agent_response.validate_answer(
            answer="AMD looks like the strongest put candidate here.",
            context=context,
            tool_name="get_put_wheel_opportunity",
            tool_result='{"symbol": "TSLA"}',
        )

        self.assertFalse(validation.is_valid)
        self.assertIn("AMD", " ".join(validation.reasons))

    def test_validate_answer_allows_symbols_present_in_tool_result(self) -> None:
        context = agent_request.build_request_context("Compare TSLA and AMD for wheel")

        validation = agent_response.validate_answer(
            answer="TSLA offers more premium, while AMD looks more conservative.",
            context=context,
            tool_name="compare_put_candidates",
            tool_result='{"symbols_requested": ["TSLA", "AMD"]}',
        )

        self.assertTrue(validation.is_valid)

    def test_validate_answer_flags_invalid_ticker_i_in_monthly_income_workflow(self) -> None:
        context = agent_request.build_request_context(
            "I have 500$ monthly income target",
            history=[{"role": "user", "content": "Build a monthly income plan"}],
        )

        validation = agent_response.validate_answer(
            answer="It appears that I couldn't find any viable monthly income plan candidates for the ticker I.",
            context=context,
            tool_name="build_monthly_income_plan",
            tool_result='{"skipped_positions": [{"symbol": "I"}]}',
        )

        self.assertFalse(validation.is_valid)
        self.assertIn("invalid ticker I", " ".join(validation.reasons))
