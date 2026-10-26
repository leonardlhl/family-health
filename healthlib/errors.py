class ValidationError(ValueError):
    """A safe, actionable local validation message."""


class PrivacyReviewRequired(ValidationError):
    """Unreviewed sources cannot be exported or used for family previews."""
