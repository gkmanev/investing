from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from rest_framework import serializers

from .models import EmailVerificationToken


User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    has_full_access = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_staff",
            "is_superuser",
            "has_full_access",
        ]
        read_only_fields = fields

    def get_has_full_access(self, obj) -> bool:
        if getattr(obj, "is_staff", False):
            return True
        try:
            return bool(obj.premium_subscription.is_active)
        except ObjectDoesNotExist:
            return False


class RegisterSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    daily_brief_opt_in = serializers.BooleanField(
        required=False,
        default=False,
        write_only=True,
    )

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "password",
            "first_name",
            "last_name",
            "daily_brief_opt_in",
        ]

    def validate_username(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Username cannot be empty.")
        if User.objects.filter(username__iexact=cleaned).exists():
            raise serializers.ValidationError("A user with that username already exists.")
        return cleaned

    def validate_email(self, value: str) -> str:
        cleaned = value.strip().lower()
        if not cleaned:
            raise serializers.ValidationError("Email cannot be empty.")
        if User.objects.filter(email__iexact=cleaned).exists():
            raise serializers.ValidationError("A user with that email already exists.")
        return cleaned

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def create(self, validated_data):
        validated_data.pop("daily_brief_opt_in", False)
        password = validated_data.pop("password")
        user = User(**validated_data, is_active=False)
        user.set_password(password)
        user.save()
        return user


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    default_error_messages = {"invalid_credentials": "Invalid username/email or password."}

    def validate(self, attrs):
        request = self.context.get("request")
        identifier = attrs["identifier"].strip()
        password = attrs["password"]

        if not identifier:
            raise serializers.ValidationError({"identifier": "This field may not be blank."})

        user = User.objects.filter(
            Q(username__iexact=identifier) | Q(email__iexact=identifier)
        ).first()
        if user is not None and not user.is_active and user.check_password(password):
            raise serializers.ValidationError(
                {"identifier": "Email address is not verified."}
            )

        username = user.get_username() if user else identifier
        authenticated_user = authenticate(request=request, username=username, password=password)

        if authenticated_user is None:
            self.fail("invalid_credentials")

        attrs["user"] = authenticated_user
        return attrs


class VerifyEmailSerializer(serializers.Serializer):
    token = serializers.UUIDField()

    def validate_token(self, value):
        try:
            token_obj = EmailVerificationToken.objects.select_related("user").get(token=value)
        except EmailVerificationToken.DoesNotExist as exc:
            raise serializers.ValidationError("Invalid verification token.") from exc
        self.context["token_obj"] = token_obj
        return value


class ResendVerificationSerializer(serializers.Serializer):
    identifier = serializers.CharField()

    def validate_identifier(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("This field may not be blank.")
        return cleaned
