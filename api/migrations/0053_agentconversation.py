# Generated manually for persisted AI-chat conversations.

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def create_legacy_conversations(apps, schema_editor):
    AgentConversation = apps.get_model("api", "AgentConversation")
    AgentRun = apps.get_model("api", "AgentRun")

    # Runs created before conversations existed are kept visible as individual
    # legacy chats. Their original grouping cannot be inferred reliably.
    for run in AgentRun.objects.filter(conversation__isnull=True).iterator():
        conversation = AgentConversation.objects.create(
            user_id=run.user_id,
            anonymous_session_key=run.anonymous_session_key,
            title=" ".join((run.query or "").split())[:160],
            preview=(run.result_text or "")[:500],
        )
        AgentConversation.objects.filter(pk=conversation.pk).update(
            created_at=run.created_at,
            updated_at=run.updated_at,
        )
        AgentRun.objects.filter(pk=run.pk).update(conversation_id=conversation.pk)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("api", "0052_watchlistitem"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentConversation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("anonymous_session_key", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("title", models.CharField(blank=True, default="", max_length=160)),
                ("preview", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="agent_conversations", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-updated_at", "-created_at"]},
        ),
        migrations.AddField(
            model_name="agentrun",
            name="conversation",
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="runs", to="api.agentconversation"),
        ),
        migrations.RunPython(create_legacy_conversations, migrations.RunPython.noop),
    ]
