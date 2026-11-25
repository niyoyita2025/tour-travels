from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class EmailOrUsernameBackend(ModelBackend):
    """Sign in with a username or with the email address the account was registered with.

    Inactive accounts (for example, sign-ups that have not verified their email yet) cannot sign in,
    exactly as with Django's default backend.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        identifier = (username or kwargs.get(UserModel.USERNAME_FIELD) or '').strip()
        if not identifier or password is None:
            return None
        candidates = list(
            UserModel.objects.filter(Q(username=identifier) | Q(email__iexact=identifier)).order_by('pk')[:5]
        )
        # An exact username match is tried before an email match.
        candidates.sort(key=lambda user: user.username != identifier)
        for user in candidates:
            if user.check_password(password) and self.user_can_authenticate(user):
                return user
        # Hash once even when nothing matches, so response times do not reveal which accounts exist.
        UserModel().set_password(password)
        return None
