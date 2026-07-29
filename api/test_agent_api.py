from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from api.models import AgentConversation, AgentRun


class AgentApiTests(APITestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="agent-user",
            password="test-pass-123",
        )
        self.other_user = get_user_model().objects.create_user(
            username="other-user",
            password="test-pass-456",
        )
        self.client.force_authenticate(user=self.user)

    @patch("api.tasks.run_agent_run.delay")
    def test_post_agent_creates_run_and_enqueues_task(
        self,
        mock_delay: MagicMock,
    ) -> None:
        response = self.client.post(
            reverse("agent"),
            {"query": "Hello", "history": [{"role": "assistant", "content": "Earlier"}]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        agent_run = AgentRun.objects.get(pk=response.data["job_id"])
        self.assertEqual(agent_run.user, self.user)
        self.assertIsNotNone(agent_run.conversation_id)
        self.assertEqual(response.data["conversation_id"], str(agent_run.conversation_id))
        self.assertEqual(agent_run.query, "Hello")
        self.assertEqual(
            agent_run.history_json,
            [{"role": "assistant", "content": "Earlier"}],
        )
        self.assertEqual(agent_run.status, AgentRun.Status.PENDING)
        self.assertEqual(response.data["query"], "Hello")
        self.assertEqual(
            response.data["history"],
            [{"role": "assistant", "content": "Earlier"}],
        )
        self.assertEqual(response.data["used_tools"], [])
        self.assertIsNone(response.data["used_tool"])
        self.assertEqual(response.data["llm_usage"], [])
        self.assertEqual(response.data["llm_usage_summary"]["estimated_cost_usd"], 0.0)
        self.assertEqual(response.data["plan"], "free")
        self.assertIsNone(response.data["trial_days_left"])
        mock_delay.assert_called_once_with(agent_run.id)

    def test_post_agent_rejects_non_list_history(self) -> None:
        response = self.client.post(
            reverse("agent"),
            {"query": "Hello", "history": {"bad": "shape"}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"], "history must be a list")

    @patch("api.tasks.run_agent_run.delay")
    def test_post_agent_restores_the_last_five_completed_turns(
        self,
        mock_delay: MagicMock,
    ) -> None:
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        conversation = AgentConversation.objects.create(user=self.user, title="Existing chat")
        for index in range(6):
            AgentRun.objects.create(
                user=self.user,
                conversation=conversation,
                query=f"Earlier prompt {index}",
                result_text=f"Earlier answer {index}",
                status=AgentRun.Status.COMPLETED,
            )

        response = self.client.post(
            reverse("agent"),
            {"query": "Follow-up", "conversation_id": str(conversation.id), "history": []},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        agent_run = AgentRun.objects.get(pk=response.data["job_id"])
        self.assertEqual(
            agent_run.history_json,
            [
                item
                for index in range(1, 6)
                for item in (
                    {"role": "user", "content": f"Earlier prompt {index}"},
                    {"role": "assistant", "content": f"Earlier answer {index}"},
                )
            ],
        )
        mock_delay.assert_called_once_with(agent_run.id)

    @patch("api.tasks.run_agent_run.delay")
    def test_anonymous_user_can_submit_three_weekly_queries(
        self,
        mock_delay: MagicMock,
    ) -> None:
        self.client.force_authenticate(user=None)

        for index in range(3):
            response = self.client.post(
                reverse("agent"),
                {"query": f"Question {index}", "history": []},
                format="json",
                HTTP_X_DEVICE_FINGERPRINT="anonymous-test-device",
            )
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        response = self.client.post(
            reverse("agent"),
            {"query": "One too many", "history": []},
            format="json",
            HTTP_X_DEVICE_FINGERPRINT="anonymous-test-device",
        )

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data["error"], "weekly_limit_reached")
        self.assertEqual(response.data["limit"], 3)
        self.assertEqual(response.data["limit_scope"], "weekly")
        self.assertEqual(
            AgentRun.objects.filter(user__isnull=True).count(),
            3,
        )
        self.assertEqual(mock_delay.call_count, 3)

    @patch("api.tasks.run_agent_run.delay")
    def test_anonymous_user_can_only_retrieve_own_runs(
        self,
        mock_delay: MagicMock,
    ) -> None:
        self.client.force_authenticate(user=None)
        response = self.client.post(
            reverse("agent"),
            {"query": "Anonymous question", "history": []},
            format="json",
            HTTP_X_DEVICE_FINGERPRINT="anonymous-test-device",
        )

        detail_response = self.client.get(
            reverse("agent-detail", args=[response.data["job_id"]]),
            HTTP_X_DEVICE_FINGERPRINT="anonymous-test-device",
        )

        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.data["query"], "Anonymous question")
        mock_delay.assert_called_once()

    @patch("api.tasks.run_agent_run.delay")
    def test_anonymous_run_lookup_is_stable_when_proxy_ip_changes(
        self,
        mock_delay: MagicMock,
    ) -> None:
        self.client.force_authenticate(user=None)
        response = self.client.post(
            reverse("agent"),
            {"query": "Anonymous question", "history": []},
            format="json",
            HTTP_X_DEVICE_FINGERPRINT="anonymous-test-device",
            REMOTE_ADDR="10.0.0.10",
        )

        detail_response = self.client.get(
            reverse("agent-detail", args=[response.data["job_id"]]),
            HTTP_X_DEVICE_FINGERPRINT="anonymous-test-device",
            REMOTE_ADDR="10.0.0.11",
        )

        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        mock_delay.assert_called_once()

    @patch("api.tasks.run_agent_run.delay")
    def test_post_agent_enforces_free_daily_query_limit(
        self,
        mock_delay: MagicMock,
    ) -> None:
        for index in range(4):
            AgentRun.objects.create(
                user=self.user,
                query=f"Earlier query {index}",
                history_json=[],
            )

        response = self.client.post(
            reverse("agent"),
            {"query": "Hello again", "history": []},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data["error"], "daily_limit_reached")
        self.assertEqual(response.data["plan"], "free")
        self.assertEqual(response.data["limit"], 4)
        self.assertEqual(response.data["limit_scope"], "daily")
        self.assertFalse(response.data["trial_expired"])
        mock_delay.assert_not_called()

    @patch("api.tasks.run_agent_run.delay")
    def test_free_user_must_verify_their_email_before_using_the_agent(
        self,
        mock_delay: MagicMock,
    ) -> None:
        unverified_user = get_user_model().objects.create_user(
            username="unverified-agent-user",
            password="test-pass-789",
            is_active=False,
        )
        self.client.force_authenticate(user=unverified_user)

        response = self.client.post(
            reverse("agent"),
            {"query": "Can I use the agent?", "history": []},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["error"], "email_verification_required")
        mock_delay.assert_not_called()

    def test_anonymous_user_must_supply_a_device_fingerprint(self) -> None:
        self.client.force_authenticate(user=None)

        response = self.client.post(
            reverse("agent"),
            {"query": "Can I use the agent?", "history": []},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"], "device_fingerprint_required")

    @patch("api.tasks.run_agent_run.delay")
    def test_anonymous_user_with_a_session_cookie_does_not_require_csrf(
        self,
        mock_delay: MagicMock,
    ) -> None:
        client = APIClient(enforce_csrf_checks=True)
        client.login(username="agent-user", password="test-pass-123")

        response = client.post(
            reverse("agent"),
            {"query": "Anonymous question", "history": []},
            format="json",
            HTTP_X_DEVICE_FINGERPRINT="anonymous-session-cookie-device",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        agent_run = AgentRun.objects.get(pk=response.data["job_id"])
        self.assertIsNone(agent_run.user)
        mock_delay.assert_called_once_with(agent_run.id)

    @patch("api.tasks.run_agent_run.delay")
    def test_pro_user_is_not_limited_by_the_free_daily_allowance(
        self,
        mock_delay: MagicMock,
    ) -> None:
        self.other_user.is_staff = True
        self.other_user.save(update_fields=["is_staff"])
        self.client.force_authenticate(user=self.other_user)

        for index in range(5):
            response = self.client.post(
                reverse("agent"),
                {"query": f"Pro question {index}", "history": []},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        self.assertEqual(mock_delay.call_count, 5)

    @patch("api.tasks.run_agent_run.delay")
    def test_pro_user_is_limited_to_sixty_runs_per_rolling_hour(
        self,
        mock_delay: MagicMock,
    ) -> None:
        self.other_user.is_staff = True
        self.other_user.save(update_fields=["is_staff"])
        self.client.force_authenticate(user=self.other_user)
        AgentRun.objects.bulk_create(
            [
                AgentRun(user=self.other_user, query=f"Earlier pro query {index}")
                for index in range(60)
            ]
        )

        response = self.client.post(
            reverse("agent"),
            {"query": "One too many this hour", "history": []},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data["error"], "hourly_limit_reached")
        self.assertEqual(response.data["limit"], 60)
        self.assertEqual(response.data["used_in_window"], 60)
        self.assertEqual(response.data["limit_scope"], "hourly")
        mock_delay.assert_not_called()

    @patch("api.tasks.run_agent_run.delay")
    def test_pro_user_is_limited_to_two_hundred_runs_per_day(
        self,
        mock_delay: MagicMock,
    ) -> None:
        self.other_user.is_staff = True
        self.other_user.save(update_fields=["is_staff"])
        self.client.force_authenticate(user=self.other_user)
        AgentRun.objects.bulk_create(
            [
                AgentRun(user=self.other_user, query=f"Earlier pro query {index}")
                for index in range(200)
            ]
        )

        response = self.client.post(
            reverse("agent"),
            {"query": "Daily circuit breaker", "history": []},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data["error"], "daily_limit_reached")
        self.assertEqual(response.data["limit"], 200)
        self.assertEqual(response.data["limit_scope"], "daily")
        mock_delay.assert_not_called()

    def test_get_agent_detail_returns_own_run(self) -> None:
        agent_run = AgentRun.objects.create(
            user=self.user,
            query="Hello",
            history_json=[],
            status=AgentRun.Status.COMPLETED,
            result_text="Finished answer",
            result_blocks_json=[
                {"type": "table", "version": 1, "columns": [], "rows": []},
            ],
            used_tools_json=[
                {"name": "analyze_stock", "arguments": {"symbol": "AAPL"}, "iteration": 1},
            ],
            llm_usage_json=[
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
            llm_usage_summary_json={"estimated_cost_usd": 3.6e-05},
        )

        response = self.client.get(reverse("agent-detail", args=[agent_run.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["job_id"], agent_run.id)
        self.assertEqual(response.data["query"], "Hello")
        self.assertEqual(response.data["history"], [])
        self.assertEqual(response.data["status"], AgentRun.Status.COMPLETED)
        self.assertEqual(response.data["answer"], "Finished answer")
        self.assertEqual(
            response.data["blocks"],
            [{"type": "table", "version": 1, "columns": [], "rows": []}],
        )
        self.assertEqual(response.data["error"], "")
        self.assertEqual(
            response.data["used_tools"],
            [{"name": "analyze_stock", "arguments": {"symbol": "AAPL"}, "iteration": 1}],
        )
        self.assertEqual(
            response.data["used_tool"],
            {"name": "analyze_stock", "arguments": {"symbol": "AAPL"}, "iteration": 1},
        )
        self.assertEqual(response.data["llm_usage"][0]["model"], "gpt-4o-mini")
        self.assertEqual(response.data["llm_usage_summary"]["estimated_cost_usd"], 3.6e-05)
        self.assertEqual(response.data["plan"], "free")
        self.assertIsNone(response.data["trial_days_left"])

    def test_get_agent_detail_hides_other_users_runs(self) -> None:
        agent_run = AgentRun.objects.create(
            user=self.other_user,
            query="Secret",
            history_json=[],
        )

        response = self.client.get(reverse("agent-detail", args=[agent_run.id]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_get_agent_list_returns_own_conversation_summaries(self) -> None:
        older_conversation = AgentConversation.objects.create(
            user=self.user,
            title="Older prompt",
            preview="Older answer",
        )
        newer_conversation = AgentConversation.objects.create(
            user=self.user,
            title="Newest prompt",
            preview="Newest answer",
        )
        AgentConversation.objects.create(
            user=self.other_user,
            title="Other user prompt",
        )

        response = self.client.get(reverse("agent"), {"limit": 10})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            {item["conversation_id"] for item in response.data["results"]},
            {str(older_conversation.id), str(newer_conversation.id)},
        )
        self.assertEqual(
            {item["preview"] for item in response.data["results"]},
            {"Older answer", "Newest answer"},
        )

    def test_get_conversation_returns_messages_in_chronological_order(self) -> None:
        conversation = AgentConversation.objects.create(user=self.user, title="Income plan")
        AgentRun.objects.create(
            user=self.user,
            conversation=conversation,
            query="Build an income plan",
            result_text="Start with diversified CSPs.",
            status=AgentRun.Status.COMPLETED,
        )
        AgentRun.objects.create(
            user=self.user,
            conversation=conversation,
            query="Make it more conservative",
            result_text="Lower the allocation per position.",
            status=AgentRun.Status.COMPLETED,
        )

        response = self.client.get(reverse("agent"), {"conversation_id": str(conversation.id)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["results"],
            [
                {"role": "user", "content": "Build an income plan"},
                {"role": "assistant", "content": "Start with diversified CSPs."},
                {"role": "user", "content": "Make it more conservative"},
                {"role": "assistant", "content": "Lower the allocation per position."},
            ],
        )

    def test_get_conversation_hides_other_users_messages(self) -> None:
        conversation = AgentConversation.objects.create(user=self.other_user, title="Private")

        response = self.client.get(reverse("agent"), {"conversation_id": str(conversation.id)})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("api.tasks.run_agent_run.delay")
    def test_post_cannot_add_to_another_users_conversation(self, mock_delay: MagicMock) -> None:
        conversation = AgentConversation.objects.create(user=self.other_user, title="Private")

        response = self.client.post(
            reverse("agent"),
            {"query": "Attempted follow-up", "conversation_id": str(conversation.id)},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["error"], "conversation_not_found")
        mock_delay.assert_not_called()
