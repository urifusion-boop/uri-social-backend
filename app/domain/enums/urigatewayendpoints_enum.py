from enum import Enum

from app.core.config import settings


class UriGatewayEndpointsEnum(Enum):
    URI_BACKEND = settings.URI_BACKEND_BASE_URL
    TASK_MANAGER = settings.URI_TASK_MANAGER_BASE_URL
    URI_TRANSACTIONS = settings.URI_TRANSACTIONS_BASE_URL
