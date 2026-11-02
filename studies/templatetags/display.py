"""How names and times are shown to people. Display only: nothing stored
is changed."""
import datetime

from django import template
from django.utils import dateformat, timezone

register = template.Library()

# e.g. "Nov 2, 2026, 1:52 PM EST" (or EDT in summer)
DATETIME_DISPLAY = "M j, Y, g:i A T"


def _capitalize_word(word: str) -> str:
    return word[:1].upper() + word[1:]


@register.filter
def display_name(user) -> str:
    """The person's name with each part capitalized: their first and last
    name when set ("aida motamedi" -> "Aida Motamedi"), otherwise their
    username split on dots, dashes and underscores ("aida" -> "Aida",
    "john.smith" -> "John Smith")."""
    if user is None:
        return ""
    full = (getattr(user, "get_full_name", lambda: "")() or "").strip()
    source = full or str(getattr(user, "get_username", lambda: user)())
    words = source.replace(".", " ").replace("_", " ").replace("-", " ").split() if not full else full.split()
    return " ".join(_capitalize_word(w) for w in words) or source


@register.filter
def when(value) -> str:
    """A date and time in the site's time zone (Ottawa time, with EST/EDT
    handled automatically), always in the same format. Accepts a datetime
    or an ISO 8601 string, as stored in the detection failure history."""
    if value in (None, ""):
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime.datetime):
        if timezone.is_naive(value):
            value = timezone.make_aware(value, datetime.timezone.utc)
        return dateformat.format(timezone.localtime(value), DATETIME_DISPLAY)
    return str(value)
