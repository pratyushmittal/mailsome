"""Standard bound form for explicit AI consent and connection settings."""

from django import forms


class UserContextForm(forms.Form):
    user_context = forms.CharField(
        label="Your mail context",
        required=False,
        max_length=20_000,
        widget=forms.Textarea(attrs={"rows": 10}),
        help_text=(
            "Explain your mail setup, forwarding addresses, priorities, and classification expectations. "
            "Saved locally and sent to the AI with every classification batch. "
            "Changes apply to future classifications; use Reclassify recent inbox to revisit completed mail."
        ),
    )


class AIForm(UserContextForm):
    enabled = forms.BooleanField(label="Enable AI classification", required=False)
    reasoning = forms.ChoiceField(choices=[("medium", "Medium"), ("high", "High")])
    api_key = forms.CharField(
        label="OpenAI API key",
        max_length=512,
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Leave blank to keep the saved key. Your key stays on this machine.",
    )


class ReclassifyForm(forms.Form):
    confirm = forms.BooleanField(
        label="Reset these classification labels and incur new AI charges",
        help_text="Manual assignments of these labels may be removed, and manually removed labels may be added again. Current sender rules protect their matching assignments.",
    )
