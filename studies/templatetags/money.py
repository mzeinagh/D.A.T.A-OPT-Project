"""Shows cost estimates (stored in USD) in Canadian dollars too, when
settings.COST_USD_TO_CAD_RATE is set. Display only."""
from django import template
from django.conf import settings

register = template.Library()


@register.filter
def cost(usd, places=4):
    """'CA$0.2134 (US$0.1550)' with a rate set, 'US$0.1550' without one,
    or '—' when the cost is unknown."""
    if usd is None or usd == "":
        return "—"
    try:
        usd = float(usd)
        places = int(places)
    except (TypeError, ValueError):
        return "—"
    rate = getattr(settings, "COST_USD_TO_CAD_RATE", None)
    if rate:
        return f"CA${usd * rate:.{places}f} (US${usd:.{places}f})"
    return f"US${usd:.{places}f}"
