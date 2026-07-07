# app/agents/social_media_manager/services/user_media_storage_service.py

import base64
import re
from typing import List
from app.utils.cloudinary_upload import upload_bytes


class UserMediaStorageService:
    """
    Service for uploading user-provided media (images/videos) to Cloudinary storage.
    """

    @staticmethod
    async def upload_user_media(base64_data_urls: List[str], user_id: str) -> List[str]:
        """
        Upload multiple user media files (images or videos) to Cloudinary.

        Args:
            base64_data_urls: List of base64 data URLs (e.g., "data:image/png;base64,...")
            user_id: User ID for organizing uploads in Cloudinary folder

        Returns:
            List of public Cloudinary URLs

        Raises:
            ValueError: If data URL format is invalid or file type unsupported
        """
        uploaded_urls = []

        for idx, data_url in enumerate(base64_data_urls):
            # Parse data URL: data:image/png;base64,iVBORw0KGgo...
            match = re.match(r'data:([^;]+);base64,(.+)', data_url)
            if not match:
                raise ValueError(f"Invalid data URL format at index {idx}")

            mime_type = match.group(1)
            base64_data = match.group(2)

            # Validate file type
            allowed_image_types = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
            allowed_video_types = {"video/mp4", "video/quicktime", "video/x-m4v"}

            is_video = mime_type in allowed_video_types
            is_image = mime_type in allowed_image_types

            if not is_image and not is_video:
                raise ValueError(f"Unsupported file type: {mime_type}. Use PNG, JPG, WEBP, MP4, MOV.")

            # Decode base64
            try:
                file_bytes = base64.b64decode(base64_data)
            except Exception as e:
                raise ValueError(f"Failed to decode base64 data at index {idx}: {e}")

            # Size limits
            if is_image and len(file_bytes) > 10 * 1024 * 1024:
                raise ValueError(f"Image at index {idx} exceeds 10MB limit")
            if is_video and len(file_bytes) > 100 * 1024 * 1024:
                raise ValueError(f"Video at index {idx} exceeds 100MB limit")

            # Upload to Cloudinary
            resource_type = "video" if is_video else "image"
            folder = f"uri-social/user-uploads/{user_id}"

            try:
                url = await upload_bytes(
                    file_bytes,
                    folder=folder,
                    resource_type=resource_type
                )
                uploaded_urls.append(url)
                print(f"✅ Uploaded user media {idx + 1}/{len(base64_data_urls)}: {url[:100]}...")
            except Exception as e:
                print(f"❌ Failed to upload media at index {idx}: {e}")
                raise ValueError(f"Failed to upload media at index {idx}: {e}")

        return uploaded_urls
