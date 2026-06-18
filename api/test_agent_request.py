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

    def test_route_request_builds_monthly_plan_when_follow_up_provides_cash_in_k_format(self) -> None:
        context = agent_request.build_request_context(
            "I have 10K$ in cash",
            history=[{"role": "user", "content": "Build a monthly income plan"}],
        )

        decision = agent_request.route_request(context)

        self.assertEqual(context.active_intent, "monthly_income_plan")
        self.assertEqual(context.cash_budget, 10000.0)
        self.assertEqual(decision.kind, "tool")
        self.assertEqual(decision.tool_name, "build_monthly_income_plan")
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

    def test_route_request_defaults_generic_put_request_to_market_scan(self) -> None:
        context = agent_request.build_request_context("Show me puts")

        decision = agent_request.route_request(context)

        self.assertEqual(decision.kind, "tool")
        self.assertEqual(decision.tool_name, "scan_put_opportunities")
        self.assertIn("scope=market_scan", decision.defaults_applied)

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
        self.assertEqual(decision.tool_args["risk_profile"], "balanced")
        self.assertEqual(decision.tool_args["max_risk"], 500.0)
        self.assertEqual(decision.tool_args["max_dte"], 30)
        self.assertIn("risk_profile=balanced", decision.defaults_applied)

    def test_route_request_defaults_single_spread_request_without_extra_inputs(self) -> None:
        context = agent_request.build_request_context("Need a spread idea for NVDA")

        decision = agent_request.route_request(context)

        self.assertEqual(decision.kind, "tool")
        self.assertEqual(decision.tool_name, "get_spread_opportunity")
        self.assertEqual(decision.tool_args["symbol"], "NVDA")
        self.assertEqual(decision.tool_args["spread_type"], "auto")
        self.assertEqual(decision.tool_args["directional_view"], "auto")
        self.assertEqual(decision.tool_args["risk_profile"], "balanced")
        self.assertEqual(decision.tool_args["max_dte"], 45)

    def test_route_request_sends_educational_covered_call_query_to_llm(self) -> None:
        context = agent_request.build_request_context("How do covered calls work?")

        decision = agent_request.route_request(context)

        self.assertEqual(decision.kind, "llm")
        self.assertEqual(decision.reason, "policy_covered_call_explanation")

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

    def test_augment_tool_args_from_query_applies_spread_defaults(self) -> None:
        augmented = agent_request.augment_tool_args_from_query(
            "get_spread_opportunity",
            {"symbol": "NVDA"},
            "Need a spread idea for NVDA",
        )

        self.assertEqual(augmented["spread_type"], "auto")
        self.assertEqual(augmented["directional_view"], "auto")
        self.assertEqual(augmented["risk_profile"], "balanced")
        self.assertEqual(augmented["max_dte"], 45)

    def test_extract_cash_budget_from_query_parses_k_suffix_and_cash_phrase(self) -> None:
        self.assertEqual(agent_request.extract_cash_budget_from_query("I have 10K$ in cash"), 10000.0)
