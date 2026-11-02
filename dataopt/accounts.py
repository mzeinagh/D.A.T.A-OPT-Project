"""Self-service account creation (the "Create account" page).

Open sign-up is for the test environment: anyone who can reach the site can
make an account and start using it straight away. Set DJANGO_ALLOW_SIGNUP=false
to switch the page off; the links to it disappear and the URL returns 404.
"""
from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.forms import UserCreationForm
from django.http import Http404
from django.shortcuts import redirect, render


class SignUpForm(UserCreationForm):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    email = forms.EmailField()

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("first_name", "last_name", "email", "username")

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists. Try logging in instead.")
        return email


def signup_view(request):
    if not settings.ALLOW_SIGNUP:
        raise Http404
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)

    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect(settings.LOGIN_REDIRECT_URL)
    else:
        form = SignUpForm()
    return render(request, "registration/signup.html", {"form": form})


def signup_flag(request):
    """Template context: whether to show "Create account" links."""
    return {"allow_signup": settings.ALLOW_SIGNUP}
