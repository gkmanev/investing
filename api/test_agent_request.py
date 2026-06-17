from django.test import TestCase

from api import agent_request
from api.models import Symbol


class AgentRequestTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        for ticker in ["AAPL", "MSFT", "TSLA", "NVDA"]:
            Symbol.objects.create(ticker=ticker)

    def test_build_request_context_carries_monthly_plan_intent_across_turns(self) -> None:
        context = agent_request.build_request_context(
            "I have 500$ monthly income target",
            history=[{"role": "user", "content": "Build a monthly income plan"}],
        )

        self.assertEqual(context.active_intent, "monthly_income_plan")
        self.assertEqual(context.monthly_income_target, 500.0)
        self.assertEqual(context.explicit_symbols, [])

    def test_route_request_clarifies_when_monthly_plan_lacks_positions_and_cash(self) -> None:
        context = agent_request.build_request_context(
            "I have 500$ monthly income target",
            history=[{"role": "user", "content": "Build a monthly income plan"}],
        )

        decision = agent_request.route_request(context)

        self.assertEqual(decision.kind, "clarification")
        self.assertIn("monthly income plan", decision.clarification_message)
        self.assertIn("$500", decision.clarification_message)

    def test_route_request_builds_monthly_plan_when_cash_budget_is_provided(self) -> None:
        context = agent_request.build_request_context(
            "I have 500$ monthly income target and 10000 available cash",
            history=[{"role": "user", "content": "Build a monthly income plan"}],
        )

        decision = agent_request.route_request(context)

        self.assertEqual(decision.kind, "tool")
        self.assertEqual(decision.tool_name, "build_monthly_income_plan")
        self.assertEqual(decision.tool_args["monthly_income_target"], 500.0)
        self.assertEqual(decision.tool_args["account_size"], 10000.0)
        self.assertEqual(decision.tool_args["max_cash_required"], 10000.0)

    def test_build_request_context_extracts_ambiguous_common_word_symbol(self) -> None:
        context = agent_request.build_request_context("Show me put ideas for I")

        self.assertEqual(context.active_intent, "put_options")
        self.assertEqual(context.ambiguous_symbols, ["I"])

    def test_route_request_clarifies_ambiguous_common_word_symbol_for_puts(self) -> None:
        context = agent_request.build_request_context("Show me put ideas for I")

        decision = agent_request.route_request(context)

        self.assertEqual(decision.kind, "clarification")
        self.assertEqual(
            decision.reason,
            "ambiguous_common_word_symbol_requires_confirmation",
        )
        self.assertIn("ambiguous ticker text: I", decision.clarification_message)

    def test_route_request_compares_covered_calls_for_multiple_symbols(self) -> None:
        context = agent_request.build_request_context(
            "Compare covered calls for AAPL and MSFT with max delta 0.25 and min ROI 2",
        )

        decision = agent_request.route_request(context)

        self.assertEqual(decision.kind, "tool")
        self.assertEqual(decision.tool_name, "compare_covered_call_candidates")
        self.assertEqual(decision.tool_args["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(decision.tool_args["max_delta"], 0.25)
        self.assertEqual(decision.tool_args["min_roi"], 2.0)

    def test_route_request_scans_spreads_with_extracted_filters(self) -> None:
        context = agent_request.build_request_context(
            "Show the best bullish credit spreads across the market with max risk 500 and max dte 30",
        )

        decision = agent_request.route_request(context)

        self.assertEqual(decision.kind, "tool")
        self.assertEqual(decision.tool_name, "scan_spread_opportunities")
        self.assertEqual(decision.tool_args["directional_view"], "bullish")
        self.assertEqual(decision.tool_args["spread_type"], "auto")
        self.assertEqual(decision.tool_args["max_risk"], 500.0)
        self.assertEqual(decision.tool_args["max_dte"], 30)

    def test_augment_tool_args_from_query_applies_spread_filters(self) -> None:
        augmented = agent_request.augment_tool_args_from_query(
            "get_spread_opportunity",
            {"symbol": "NVDA"},
            "Need a bearish spread with max risk 250 and max dte 21",
        )

        self.assertEqual(augmented["symbol"], "NVDA")
        self.assertEqual(augmented["directional_view"], "bearish")
        self.assertEqual(augmented["max_risk"], 250.0)
        self.assertEqual(augmented["max_dte"], 21)
