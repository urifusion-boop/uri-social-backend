from fastapi import HTTPException
from starlette.responses import JSONResponse
from typing import Any, Dict, Optional, List
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_500_INTERNAL_SERVER_ERROR,
)
from pydantic import BaseModel


class UriResponse:
    @staticmethod
    def get_single_data_response(
        entity_name: str,
        data: Any,
        message: Optional[str] = None,
        code: int = HTTP_200_OK,
    ) -> Any:
        if data:
            return {
                "status": True,
                "responseCode": HTTP_200_OK,
                "responseMessage": message or f"{entity_name} successfully retrieved.",
                "responseData": data,
            }
        else:
            return {
                "status": False,
                "responseCode": HTTP_404_NOT_FOUND,
                "responseMessage": message or f"{entity_name} not found.",
            }

    @staticmethod
    def get_list_data_response(
        entity_name: str, data: List[Any], message: Optional[str] = None
    ):
        return {
            "status": True,
            "responseCode": HTTP_200_OK,
            "responseMessage": message or f"{entity_name}s successfully retrieved.",
            "responseData": data,
        }

    @staticmethod
    def create_response(
        entity_name: str, data: Any = None, message: Optional[str] = None
    ) -> Dict[str, Any]:
        if data:
            return {
                "status": True,
                "responseCode": HTTP_201_CREATED,
                "responseMessage": message or f"{entity_name} successfully created.",
                "responseData": data,
            }
        else:
            return {
                "status": False,
                "responseCode": HTTP_400_BAD_REQUEST,
                "responseMessage": f"Failed to create {entity_name}.",
            }

    @staticmethod
    def update_response(
        entity_name: str, data: Any = None, message: Optional[str] = None
    ):
        if data:
            return {
                "status": True,
                "responseCode": HTTP_200_OK,
                "responseMessage": message or f"{entity_name} successfully updated.",
                "responseData": data,
            }
        else:
            return {
                "status": False,
                "responseCode": HTTP_400_BAD_REQUEST,
                "responseMessage": f"Failed to update {entity_name}.",
            }

    @staticmethod
    def delete_response(
        entity_name: str,
        is_deleted: bool,
        message: Optional[str] = None,
        data: Any = None,
    ):
        if is_deleted:
            return {
                "status": True,
                "responseCode": HTTP_200_OK,
                "responseMessage": message or f"{entity_name} successfully deleted.",
                "responseData": data,
            }
        else:
            return {
                "status": False,
                "responseCode": HTTP_400_BAD_REQUEST,
                "responseMessage": f"Failed to delete {entity_name}.",
                "responseData": data,
            }

    @staticmethod
    def error_response(message: str, code: int = HTTP_500_INTERNAL_SERVER_ERROR):
        return {
            "status": False,
            "responseCode": code,
            "responseMessage": message,
        }

    @staticmethod
    def unauthorized_response(message: Optional[str] = None):
        return {
            "status": False,
            "responseCode": HTTP_401_UNAUTHORIZED,
            "responseMessage": message
            or "You are Unauthorized, Please provide a valid access token",
        }

    @staticmethod
    def conflict_response(entity_name: str, message: Optional[str] = None):
        return {
            "status": False,
            "responseCode": HTTP_409_CONFLICT,
            "responseMessage": message or f"user with {entity_name} already exists.",
        }
