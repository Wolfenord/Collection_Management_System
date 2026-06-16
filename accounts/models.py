from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user model.

    Extending AbstractUser from the start keeps the project flexible: profile
    fields, preferences (e.g. UI language, dashboard layout) or avatars can be
    added later without a painful user-model swap.
    """

    display_name = models.CharField('Anzeigename', max_length=150, blank=True)

    def __str__(self) -> str:
        return self.display_name or self.get_username()
