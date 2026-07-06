from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views
from .forms import ApprovalAuthenticationForm, StyledPasswordResetForm, StyledSetPasswordForm

urlpatterns = [
    path(
        'login/',
        auth_views.LoginView.as_view(authentication_form=ApprovalAuthenticationForm),
        name='login',
    ),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('register/', views.register, name='register'),
    path('profile/', views.profile, name='profile'),
    path('profile/tokens/new/', views.token_create, name='token_create'),
    path('profile/tokens/<int:token_pk>/delete/', views.token_delete, name='token_delete'),
    path('password/', views.password_change, name='password_change'),

    # Password reset by e-mail ("Passwort vergessen?"). Django's stock views
    # with styled forms; the mail backend is configured in settings (console
    # locally, SMTP via environment in production).
    path('password-reset/', auth_views.PasswordResetView.as_view(
        form_class=StyledPasswordResetForm,
        template_name='registration/password_reset_form.html',
        email_template_name='registration/password_reset_email.txt',
        subject_template_name='registration/password_reset_subject.txt',
        success_url=reverse_lazy('password_reset_done'),
    ), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='registration/password_reset_done.html',
    ), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        form_class=StyledSetPasswordForm,
        template_name='registration/password_reset_confirm.html',
        success_url=reverse_lazy('password_reset_complete'),
    ), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(
        template_name='registration/password_reset_complete.html',
    ), name='password_reset_complete'),
]
