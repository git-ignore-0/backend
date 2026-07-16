from django.contrib.auth.backends import ModelBackend


class CoreAuthenticationBackend(ModelBackend):
    def user_can_authenticate(self, user) -> bool:
        return user.can_authenticate()
