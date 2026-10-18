from os import environ

from fastapi import FastAPI

from antifraud.configuration import DEFAULT_POLICY_PATH, load_policy_catalog
from constants import INTERNAL_TOKEN, check_variables
from controllers.pix_api import PixController
from controllers.pix_storage import PixStorage
from database import PixSessionLocal
from errors import register_error_handlers
from errors.base_error import error_verification
from middlewares import (
    register_internal_token_middleware,
    register_request_context_middleware,
    register_request_logger_middleware,
    register_session_manager_middleware,
)
from providers.configuration import load_fixture_providers
from resources import HealthCheckResource, SampleEntityResource
from resources.pix import PixResource
from utils.logger import setup_logging
from utils.pix_tenants import load_tenants


def create_app() -> FastAPI:
    """Builds the application: configuration, middlewares, routes and error handlers."""
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    application.state.pix_policies = load_policy_catalog(environ.get("PIX_POLICY_FILE", DEFAULT_POLICY_PATH))
    application.state.pix_tenants = load_tenants(environ.get("PIX_TENANTS"), INTERNAL_TOKEN, application.state.pix_policies)
    bank, reputation = load_fixture_providers(environ.get("PIX_FIXTURE_FILE"))
    application.state.pix_controller = PixController(PixStorage(PixSessionLocal), bank, reputation)

    # The last registered middleware runs first. Request order:
    # request_context -> request_logger -> internal_token -> session_manager -> route.
    register_session_manager_middleware(application)
    register_internal_token_middleware(application)
    register_request_logger_middleware(application)
    register_request_context_middleware(application)

    health_check_resource = HealthCheckResource()
    sample_entity_resource = SampleEntityResource()
    pix_resource = PixResource()

    application.add_api_route("/", health_check_resource.on_get_home, methods=["GET"])
    application.add_api_route("/health_check", health_check_resource.on_get_health_check, methods=["GET"])
    application.add_api_route("/sample_entity", sample_entity_resource.on_post, methods=["POST"])
    application.add_api_route("/sample_entity/{sample_entity_key}", sample_entity_resource.on_get_by_key, methods=["GET"])
    application.add_api_route("/sample_entity/{sample_entity_key}", sample_entity_resource.on_put_by_key, methods=["PUT"])
    application.add_api_route("/webhook/sample_entity/{sample_entity_key}/increment_counter",
                              sample_entity_resource.on_put_increment_counter, methods=["PUT"])
    application.add_api_route("/sample_entities", sample_entity_resource.on_get_list, methods=["GET"])
    application.add_api_route("/pix/transaction", pix_resource.on_post, methods=["POST"])
    application.add_api_route("/pix/transaction/{id}", pix_resource.on_get, methods=["GET"])

    register_error_handlers(application)
    return application


def main() -> FastAPI:
    check_variables()
    error_verification()
    setup_logging()
    return create_app()


app = main()
