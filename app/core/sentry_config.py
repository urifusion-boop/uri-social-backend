"""
Sentry configuration for uri-agent service
"""
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.integrations.pymongo import PyMongoIntegration
import os


def initialize_sentry():
    sentry_dsn = os.getenv("SENTRY_DSN")

    if not sentry_dsn:
        print("[Sentry] SENTRY_DSN not found in environment, skipping initialization")
        return

    environment = os.getenv("ENV", "Development")

    sentry_sdk.init(
        dsn=sentry_dsn,
        send_default_pii=True,
        environment=environment,
        traces_sample_rate=1.0,
        profile_session_sample_rate=1.0,
        profile_lifecycle="trace",
        enable_logs=True,
        integrations=[
            StarletteIntegration(
                transaction_style="endpoint",
                failed_request_status_codes={*range(500, 599)},
                http_methods_to_capture=("GET", "POST", "PUT", "DELETE", "PATCH"),
            ),
            FastApiIntegration(
                transaction_style="endpoint",
                failed_request_status_codes={*range(500, 599)},
                http_methods_to_capture=("GET", "POST", "PUT", "DELETE", "PATCH"),
            ),
            PyMongoIntegration(),
        ],
    )

    sentry_sdk.set_tag("service", "uri-agent")
    sentry_sdk.set_tag("service_type", "python-fastapi")

    print(f"[Sentry] Initialized successfully for uri-agent (env: {environment})")
