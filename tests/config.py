import os
import uuid

BASE_URL = os.getenv("TEST_API_URL", "https://api-staging.urisocial.com")

TEST_EMAIL = f"qa+{uuid.uuid4().hex[:8]}@urisocial.com"
TEST_PASSWORD = "TestPass123!"
TEST_FIRST = "QA"
TEST_LAST = "Bot"
