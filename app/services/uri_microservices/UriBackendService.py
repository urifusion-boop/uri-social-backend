from app.core.config import settings
from app.domain.enums.urigatewayendpoints_enum import UriGatewayEndpointsEnum
from app.domain.enums.endpoints_enum import UriBackendEndpointsEnum
from app.services.uri_microservices.UriGatewayService import UriGatewayService


class UriBackendService:
    base_url = UriGatewayEndpointsEnum.URI_BACKEND.value

    @staticmethod
    async def get_user_details(user_id: str):
        url = UriBackendService.base_url + settings.URI_BACKEND_USER_DETAILS + user_id
        try:
            result = await UriGatewayService.get(url)
            return result.get("responseData") if result else None
        except Exception as e:
            print("Exception occurred in get_user_details: ", e)
            return None

    @staticmethod
    async def get_trial_status(user_id: str):
        url = f"{UriBackendService.base_url}/trial/status/{user_id}"
        try:
            result = await UriGatewayService.get(url)
            return result
        except Exception as e:
            print(f"Exception occurred getting trial status: {e}")
            return None
