import functools
import json
import os

from jsonschema import RefResolver, ValidationError, validate

from constants import SCHEMA_PATH
from errors import InvalidSchema


class SchemaCache:
    """Schemas read from disk once and reused for the lifetime of the process."""

    schemas = {}

    @staticmethod
    def get_schema(schema_file_name: str) -> dict:
        schema_path = os.path.join(SCHEMA_PATH, schema_file_name)
        if schema_path in SchemaCache.schemas:
            return SchemaCache.schemas[schema_path]
        if not os.path.isfile(schema_path):
            raise Exception(f"Schema '{schema_file_name}' not found in {SCHEMA_PATH}.")
        with open(schema_path, "r", encoding="utf-8") as schema_file:
            schema = json.loads(schema_file.read())
        SchemaCache.schemas[schema_path] = schema
        return schema


class SchemaHandler:
    """Validates request bodies and query strings against the JSON Schema files in src/schemas."""

    @staticmethod
    def validate(schema_file_name: str):
        """Decorator for resource methods that receive the body as the `payload` keyword argument."""

        def decorator_validate(func):
            @functools.wraps(func)
            def wrapper_validate(*args, **kwargs):
                if "payload" not in kwargs:
                    raise Exception(f"Route '{func.__name__}' uses SchemaHandler but has no 'payload' parameter.")
                schema = SchemaCache.get_schema(schema_file_name)
                resolver = RefResolver(f"file://{SCHEMA_PATH}/", None)
                try:
                    validate(kwargs["payload"], schema, resolver=resolver)
                except ValidationError as error:
                    raise InvalidSchema(describe_schema_error(error))
                return func(*args, **kwargs)

            return wrapper_validate

        return decorator_validate

    @staticmethod
    def validate_query_params(schema_file_name: str):
        """Decorator for resource methods that receive the whole `request`; validates the raw query string.

        Reading the raw query string lets `additionalProperties: false` catch misspelled parameters.
        Fields declared as arrays in the schema are read with getlist.
        """

        def decorator_validate(func):
            @functools.wraps(func)
            def wrapper_validate(*args, **kwargs):
                request = kwargs.get("request")
                if request is None:
                    raise Exception(f"Route '{func.__name__}' uses validate_query_params but has no 'request' parameter.")
                schema = SchemaCache.get_schema(schema_file_name)
                resolver = RefResolver(f"file://{SCHEMA_PATH}/", None)
                query_params = query_params_to_dict(request.query_params, schema)
                try:
                    validate(query_params, schema, resolver=resolver)
                except ValidationError as error:
                    raise InvalidSchema(describe_schema_error(error))
                return func(*args, **kwargs)

            return wrapper_validate

        return decorator_validate


def query_params_to_dict(query_params, schema: dict) -> dict:
    """Query string as a dict; absent fields are not included, array fields come from getlist."""
    properties = schema.get("properties", {})
    parsed_params = {}
    for param_name in query_params.keys():
        declared = properties.get(param_name, {})
        if declared.get("type") == "array":
            parsed_params[param_name] = query_params.getlist(param_name)
        else:
            parsed_params[param_name] = query_params[param_name]
    return parsed_params


def describe_schema_error(error: ValidationError) -> str:
    """The jsonschema message plus the path of the offending field, when nested."""
    location = [str(part) for part in error.absolute_path]
    if location:
        return f"{error.message} in {'.'.join(location)}"
    return error.message
