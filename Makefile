# Uri Social – Test Runner
# Usage:
#   make test                    # run all tests against staging
#   make test url=http://localhost:9003  # run against local
#   make test-auth               # run auth tests only
#   make test-onboarding         # run onboarding tests only
#   make test-trial              # run trial tests only
#   make test-content            # run content tests only
#   make test-whatsapp           # run WhatsApp tests only
#   make test-billing            # run billing tests only

TEST_API_URL ?= https://api-staging.urisocial.com

test:
	TEST_API_URL=$(TEST_API_URL) pytest tests/ -v

test-auth:
	TEST_API_URL=$(TEST_API_URL) pytest tests/test_01_auth.py -v

test-onboarding:
	TEST_API_URL=$(TEST_API_URL) pytest tests/test_02_onboarding.py -v

test-trial:
	TEST_API_URL=$(TEST_API_URL) pytest tests/test_03_trial.py -v

test-content:
	TEST_API_URL=$(TEST_API_URL) pytest tests/test_04_content.py -v

test-whatsapp:
	TEST_API_URL=$(TEST_API_URL) pytest tests/test_05_whatsapp.py -v

test-social:
	TEST_API_URL=$(TEST_API_URL) pytest tests/test_06_social_connections.py -v

test-billing:
	TEST_API_URL=$(TEST_API_URL) pytest tests/test_07_billing.py -v

test-notifications:
	TEST_API_URL=$(TEST_API_URL) pytest tests/test_08_notifications.py -v
