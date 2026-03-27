from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from rest_framework import serializers

from .models import EmailVerificationToken


User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "is_staff"]
        read_only_fields = fields


class RegisterSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ["username", "email", "password", "first_name", "last_name"]

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
