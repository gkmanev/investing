from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from api.models import AgentRun


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
        self.assertEqual(agent_run.query, "Hello")
        self.assertEqual(
            agent_run.history_json,
            [{"role": "assistant", "content": "Earlier"}],
        )
        self.assertEqual(agent_run.status, AgentRun.Status.PENDING)
        mock_delay.assert_called_once_with(agent_run.id)

    def test_post_agent_rejects_non_list_history(self) -> None:
        response = self.client.post(
            reverse("agent"),
            {"query": "Hello", "history": {"bad": "shape"}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"], "history must be a list")

    def test_get_agent_detail_returns_own_run(self) -> None:
        agent_run = AgentRun.objects.create(
            user=self.user,
            query="Hello",
            history_json=[],
            status=AgentRun.Status.COMPLETED,
            result_text="Finished answer",
        )

        response = self.client.get(reverse("agent-detail", args=[agent_run.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["job_id"], agent_run.id)
        self.assertEqual(response.data["status"], AgentRun.Status.COMPLETED)
        self.assertEqual(response.data["answer"], "Finished answer")
        self.assertEqual(response.data["error"], "")

    def test_get_agent_detail_hides_other_users_runs(self) -> None:
        agent_run = AgentRun.objects.create(
            user=self.other_user,
            query="Secret",
            history_json=[],
        )

        response = self.client.get(reverse("agent-detail", args=[agent_run.id]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
