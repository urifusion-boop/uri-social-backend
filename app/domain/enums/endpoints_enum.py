from enum import Enum


class UriBackendEndpointsEnum(Enum):
    URI_BACKEND_OAUTH_TOKEN = "/oauth/token"
    TRIAL_USAGE_INCREMENT = "/api/v1/trial/usage/increment"
