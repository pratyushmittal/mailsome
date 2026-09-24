"""Ordinary bound forms for local label and exact-sender preferences."""

from email.utils import parseaddr
from typing import Any, cast

from django import forms


class TabForm(forms.Form):
    name = forms.CharField(
        max_length=225,
        widget=forms.TextInput(attrs={"list": "gmail-labels"}),
        help_text="Choose an existing Gmail label or enter a new label name.",
    )
    description = forms.CharField(
        max_length=2000, required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    people = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Full sender email addresses, separated by commas or new lines. Maximum 100. Gmail's native sender matching labels new arrivals, even when Mailsome is closed. Adding a sender also applies this label within the latest 1,000 inbox emails. Removing one only stops future assignments.",
    )
    auto_classify = forms.BooleanField(required=False, label="Allow AI classification")

    acceptance_threshold = forms.FloatField(
        label="AI label acceptance threshold",
        required=False,
        initial=0.75,
        min_value=0,
        max_value=1,
        widget=forms.NumberInput(attrs={"step": "0.01"}),
        help_text="Apply this label when Jev's match probability meets this threshold (0 to 1).",
    )

    def clean_acceptance_threshold(self) -> float:
        value = self.cleaned_data["acceptance_threshold"]
        return (
            self.initial.get("acceptance_threshold", 0.75) if value is None else value
        )

    def clean_people(self) -> list[str]:
        people = [
            item.strip().casefold()
            for item in self.cleaned_data["people"].replace(",", "\n").splitlines()
            if item.strip()
        ]
        # Sender rules are exact addresses, never display names or Gmail search expressions.
        if any(
            len(email) > 254
            or parseaddr(email)[1] != email
            or email.count("@") != 1
            or not all(email.split("@"))
            or any(c.isspace() for c in email)
            for email in people
        ):
            raise forms.ValidationError(
                "Enter sender email addresses without display names."
            )
        # Bound rule lists keep deterministic classification work finite.
        if len(set(people)) > 100:
            raise forms.ValidationError("Choose at most 100 sender addresses.")
        return sorted(set(people))

    def clean(self) -> dict[str, Any]:
        values = super().clean() or {}
        # AI labels need a description; sender-only labels do not.
        if values.get("auto_classify") and not values.get("description"):
            self.add_error(
                "description", "Describe which messages AI should assign to this label."
            )
        return values


class SenderForm(forms.Form):
    note = forms.CharField(
        max_length=4000, required=False, widget=forms.Textarea(attrs={"rows": 6})
    )
    labels = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="New selections also apply within the latest 1,000 inbox emails. Deselecting stops future assignments; historical labels stay.",
    )

    def __init__(
        self, *args: Any, field: str, choices: list[tuple[str, str]], **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.MultipleChoiceField, self.fields["labels"]).choices = choices
        # Each editor saves only its advertised field, even if extra POST keys are supplied.
        del self.fields["labels" if field == "note" else "note"]


class UnsubscribeForm(forms.Form):
    confirmed = forms.BooleanField(
        label="I completed the advertised unsubscribe process; remember this sender as unsubscribed."
    )
