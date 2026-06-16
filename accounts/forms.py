from django.contrib.auth.forms import UserCreationForm

from .models import User


class RegistrationForm(UserCreationForm):
    """Sign-up form: username, e-mail and password (with confirmation)."""

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].required = True
        # Bootstrap styling for all fields.
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')
