"""Official QQ bot API transport (gateway WS + REST + reply budget)."""

from .budget import ReplyBudget
from .events import normalize_event
from .gateway import DEFAULT_INTENTS, OfficialGateway
from .rest import OfficialApiError, OfficialRestClient
from .sender import OfficialSender
from .tokens import AccessTokenManager

__all__ = [
    "AccessTokenManager",
    "OfficialRestClient",
    "OfficialApiError",
    "OfficialGateway",
    "OfficialSender",
    "ReplyBudget",
    "normalize_event",
    "DEFAULT_INTENTS",
]