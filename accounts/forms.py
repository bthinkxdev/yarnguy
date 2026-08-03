"""Django forms for the accounts app."""

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from recurring.models import RecurrenceFrequency 
from accounts.models import Address


class EmailLoginForm(AuthenticationForm):
    """Email/password login form using email as the username field."""

    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "class": "form-control"}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={"autocomplete": "current-password", "class": "form-control"}
        ),
    )


class OTPRequestForm(forms.Form):
    """Form to request an OTP for phone-based authentication."""

    phone = forms.CharField(
        max_length=20,
        label="Phone number",
        widget=forms.TextInput(attrs={"autocomplete": "tel"}),
    )
    purpose = forms.ChoiceField(
        choices=[
            ("login", "Login"),
            ("signup", "Sign Up"),
            ("password_reset", "Password Reset"),
        ],
        label="Purpose",
    )


class OTPVerifyForm(forms.Form):
    """Form to verify a submitted OTP code."""

    phone = forms.CharField(max_length=20, label="Phone number")
    otp_code = forms.CharField(max_length=6, min_length=6, label="OTP code")
    purpose = forms.ChoiceField(
        choices=[
            ("login", "Login"),
            ("signup", "Sign Up"),
            ("password_reset", "Password Reset"),
        ],
        label="Purpose",
    )


class EmailRegistrationForm(forms.Form):
    """Form for email-based customer registration."""

    email = forms.EmailField(label="Email")
    password = forms.CharField(
        widget=forms.PasswordInput,
        min_length=8,
        label="Password",
    )
    name = forms.CharField(max_length=150, label="Full name")


class GoogleLoginForm(forms.Form):
    """Form accepting a Google ID token from the client."""

    id_token = forms.CharField(widget=forms.HiddenInput)


class GuestCheckoutForm(forms.Form):
    """Form to issue a guest checkout token for a cart session."""

    cart_id = forms.CharField(max_length=64, label="Cart ID")


class ForgotPasswordForm(forms.Form):
    """Form to initiate password reset via OTP."""

    phone = forms.CharField(max_length=20, label="Phone number")


class ResetPasswordForm(forms.Form):
    """Form to set a new password after OTP verification."""

    phone = forms.CharField(max_length=20, label="Phone number")
    otp_code = forms.CharField(max_length=6, min_length=6, label="OTP code")
    new_password = forms.CharField(widget=forms.PasswordInput, min_length=8, label="New password")


class ForgotPasswordEmailForm(forms.Form):
    """Form to initiate password reset via email OTP."""

    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(attrs={"class": "form-control"}),
    )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        from django.contrib.auth import get_user_model
        User = get_user_model()
        if not User.objects.filter(email=email).exists():
            raise forms.ValidationError("No account is registered with this email address.")
        return email


class ResetPasswordEmailForm(forms.Form):
    """Form to reset password after email OTP verification."""

    email = forms.EmailField(widget=forms.HiddenInput())
    otp_code = forms.CharField(
        max_length=4,
        min_length=4,
        label="4-Digit OTP Code",
        widget=forms.TextInput(attrs={
            "class": "form-control text-center fs-2 letter-spacing-lg",
            "placeholder": "• • • •",
            "autocomplete": "one-time-code",
            "maxlength": "4",
        }),
    )
    new_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "New Password"}),
        min_length=8,
        label="New Password",
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Confirm Password"}),
        min_length=8,
        label="Confirm Password",
    )

    def clean(self):
        cleaned_data = super().clean()
        new_password = cleaned_data.get("new_password")
        confirm_password = cleaned_data.get("confirm_password")
        if new_password and confirm_password and new_password != confirm_password:
            self.add_error("confirm_password", "Passwords do not match.")
        return cleaned_data


class AddressForm(forms.ModelForm):
    """Create or update a customer delivery address."""

    class Meta:
        model = Address
        fields = ("label", "line1", "line2", "city", "is_default")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        from delivery.models import City

        self.fields["city"].queryset = City.objects.filter(is_active=True)


class SubscriptionCreateForm(forms.Form):
    product_id = forms.IntegerField(widget=forms.HiddenInput)
    delivery_address_id = forms.ModelChoiceField(
        queryset=Address.objects.none(),
        label="Delivery address",
    )
    frequency = forms.ChoiceField(choices=RecurrenceFrequency.choices, label="Frequency")
    next_run_date = forms.DateField(
        label="First delivery date",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    quantity = forms.IntegerField(min_value=1, initial=1, label="Quantity")

    def __init__(self, *args, customer_profile=None, **kwargs):
        super().__init__(*args, **kwargs)
        if customer_profile is not None:
            self.fields["delivery_address_id"].queryset = Address.objects.filter(
                customer_profile=customer_profile
            )


class EmailOTPRequestForm(forms.Form):
    """Form to request an OTP for email-based login/signup."""

    email = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(attrs={"class": "form-control", "placeholder": "you@example.com"}),
    )


class EmailOTPVerifyForm(forms.Form):
    """Form to verify an email OTP code."""

    email = forms.EmailField(widget=forms.HiddenInput())
    otp_code = forms.CharField(
        max_length=4,
        min_length=4,
        label="4-Digit OTP Code",
        widget=forms.TextInput(attrs={
            "class": "form-control text-center fs-2 letter-spacing-lg",
            "placeholder": "• • • •",
            "autocomplete": "one-time-code",
            "maxlength": "4",
        }),
    )

class CustomerProfileEditForm(forms.Form):
    """Form to edit retail customer profile and default address."""
    
    name = forms.CharField(max_length=150, label="Full name", widget=forms.TextInput(attrs={"class": "form-control"}))
    email = forms.EmailField(label="Email", required=False, widget=forms.EmailInput(attrs={"class": "form-control", "readonly": True}))
    phone = forms.CharField(max_length=20, label="Phone number", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    
    address_line1 = forms.CharField(max_length=255, label="Address Line 1", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    address_line2 = forms.CharField(max_length=255, label="Address Line 2", required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    city_id = forms.ChoiceField(label="City", required=False, widget=forms.Select(attrs={"class": "form-select"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from delivery.models import City
        self.fields["city_id"].choices = [("", "Select City")] + [(c.pk, c.name) for c in City.objects.filter(is_active=True)]
