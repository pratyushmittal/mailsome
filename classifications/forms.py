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
            "Saved locally and sent to the AI with every email classification. "
            "Changes apply to future classifications; use Reclassify recent inbox to revisit completed mail."
        ),
    )


class AIForm(UserContextForm):
    enabled = forms.BooleanField(label="Enable AI classification", required=False)
    api_key = forms.CharField(
        label="TypeSafe API key",
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


class ImportanceForm(forms.Form):
    importance_threshold = forms.FloatField(
        label="Important badge threshold",
        min_value=0,
        max_value=1,
        widget=forms.NumberInput(attrs={"step": "0.01"}),
        help_text="Show Important in the email list when the score is above this value. Applies immediately to stored scores.",
    )
    importance_levels = forms.CharField(
        label="Importance levels, from lowest to highest",
        max_length=12_000,
        widget=forms.Textarea(attrs={"rows": 10}),
        help_text="One description per line, from lowest to highest importance. Use at least two levels. Changes apply to future classifications; reclassify to update stored scores.",
    )

    def clean_importance_levels(self) -> list[str]:
        levels = [
            line.strip()
            for line in self.cleaned_data["importance_levels"].splitlines()
            if line.strip()
        ]
        if len(levels) < 2:
            raise forms.ValidationError("Describe at least two importance levels.")
        return levels
