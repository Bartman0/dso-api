import json
from inspect import isgeneratorfunction
from types import GeneratorType
from typing import Optional, Type, Union

from django.http import HttpResponseNotFound
from rest_framework.exceptions import ErrorDetail, NotAcceptable, ValidationError
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.views import exception_handler as drf_exception_handler
from schematools.types import DatasetTableSchema

from dso_api.lib.exceptions import RemoteAPIException
from rest_framework_dso import crs, filters, parsers
from rest_framework_dso.exceptions import PreconditionFailed
from rest_framework_dso.pagination import DSOPageNumberPagination
from rest_framework_dso.response import StreamingResponse
from rest_framework_dso.serializer_helpers import ReturnGenerator


def multiple_slashes(request):
    response_data = {
        "type": "error",
        "code": "HTTP_404_NOT_FOUND",
        "title": "Multiple slashes not supported",
        "status": "404",
        "instance": request.path,
    }

    return HttpResponseNotFound(json.dumps(response_data), content_type="application/json")


def exception_handler(exc, context):
    """Return the exceptions as 'application/problem+json'.

    See: https://tools.ietf.org/html/rfc7807
    """
    request = context.get("request")
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    # Instead of implementing output formats for all media types (e.g. CSV/GeoJSON/Shapefile),
    # all these exotic formats get the same generic error page (e.g. 404),
    # as not every format can provide a proper error page.
    if isinstance(request, Request) and request.accepted_renderer.media_type != "text/html":
        # The accepted_renderer is assigned to the response in finalize_response()
        request.accepted_renderer = JSONRenderer()

    # For HAL-JSON responses (and browsable HTML), the content-type should be changed.
    # Instead of "application/hal+json", the content type becomes "application/problem+json".
    #
    # Only response.content_type is set, and response['content-type'] is untouched,
    # so it remains text/html for the browsable API. It would break browsing otherwise.
    response.content_type = "application/problem+json"

    if isinstance(exc, ValidationError):
        # response.data are the fields
        response.data = {
            "type": f"urn:apiexception:{exc.default_code}",
            "title": str(exc.default_detail),
            "status": response.status_code,
            "instance": request.build_absolute_uri() if request else None,
            "invalid-params": get_invalid_params(exc, exc.detail),
            # Also include the whole tree of recursive errors that DRF generates
            "x-validation-errors": response.data,
        }
    elif isinstance(exc, RemoteAPIException):
        # Raw problem json response forwarded (for RemoteViewSet)
        # Normalize the problem+json fields to be identical to how
        # our own API's would return these.
        normalized_fields = {
            "type": f"urn:apiexception:{exc.code}",
            "code": exc.code,
            "title": exc.default_detail,
            "status": int(exc.status_code),
            "instance": request.build_absolute_uri() if request else None,
        }
        # This merge stategy puts the normal fields first:
        response.data = {**normalized_fields, **response.data}
        response.data.update(normalized_fields)
        response.status_code = int(exc.status_code)
    elif isinstance(response.data.get("detail"), ErrorDetail):
        # DRF parsed the exception as API
        detail = response.data["detail"]
        response.data = {
            "type": f"urn:apiexception:{detail.code}",
            "title": exc.default_detail if hasattr(exc, "default_detail") else str(exc),
            "detail": str(detail),
            "status": response.status_code,
        }
    else:
        # Unknown exception format, pass native JSON what DRF has generated. Make sure
        # neither application/hal+json nor application/problem+json is returned here.
        response.content_type = "application/json; charset=utf-8"

    return response


def get_invalid_params(
    exc: ValidationError, detail: Union[ErrorDetail, dict, list], field_name=None
) -> list:
    """Flatten the entire chain of DRF messages.
    This can be a recursive tree for POST requests with complex serializer data.
    """
    result = []
    if isinstance(detail, dict):
        for name, errors in detail.items():
            full_name = f"{field_name}.{name}" if field_name else name
            result.extend(get_invalid_params(exc, errors, field_name=full_name))
    elif isinstance(detail, list):
        for i, error in enumerate(detail):
            full_name = f"{field_name}[{i}]" if isinstance(error, dict) else field_name
            result.extend(get_invalid_params(exc, error, field_name=full_name))
    elif isinstance(detail, ErrorDetail):
        if field_name is None:
            field_name = detail.code
        # flattened is now RFC7807 mandates it
        result.append(
            {
                "type": f"urn:apiexception:{exc.default_code}:{detail.code}",
                "name": field_name,
                "reason": str(detail),
            }
        )
    else:
        raise TypeError(f"Invalid value for _get_invalid_params(): {detail!r}")

    return result


class AutoSelectPaginationClass:
    """An @classproperty for the pagination"""

    def __init__(self, default=None):
        self.default = default

    def __get__(self, instance, owner):
        if instance is not None:
            # Automatically select a paginator class that matches the rendered format.
            # After all, it's not possible to output CSV or GeoJSON pagination using the
            # standard DSOPageNumberPagination class.
            request = instance.request  # instance == view
            accepted_renderer = getattr(request, "accepted_renderer", None)
            allowed = getattr(accepted_renderer, "compatible_paginator_classes", None)
            if allowed is not None:
                return allowed[0]

        return self.default


class DSOViewMixin:
    """View/Viewset mixin that adds DSO-compatible API's

    This adds:
    * HTTP Accept-Crs and HTTP POST Content-Crs support.
    * Default filter backends in the view for sorting and filtering.
      The filtering logic is delegated to a ``filterset_class`` by django-filter.

    Usage:
    * Set ``filterset_class`` to enable filtering on fields.
    * The ``ordering_fields`` can be set on the view as well.
      By default, it accepts all serializer field names as input.
    """

    #: The list of allowed coordinate reference systems for the request header
    accept_crs = {crs.RD_NEW, crs.WEB_MERCATOR, crs.ETRS89, crs.WGS84}

    #: If there is a geo field, DSO requires that Accept-Crs is set.
    accept_crs_required = False

    # Using standard fields
    filter_backends = [filters.DSOFilterSetBackend, filters.DSOOrderingFilter]

    #: Class to configure the filterset
    #: (auto-generated when filterset_fields is defined, but this is slower).
    filterset_class: Type[filters.DSOFilterSet] = None

    #: Enforce parsing Content-Crs for POST requests:
    parser_classes = [parsers.DSOJsonParser]

    #: Paginator class
    pagination_class = AutoSelectPaginationClass(default=DSOPageNumberPagination)

    def initial(self, request, *args, **kwargs):
        request.accept_crs = None
        request.response_content_crs = None
        super().initial(request, *args, **kwargs)

        # DSO spec allows clients to define the desired CRS.
        accept_crs = request.META.get("HTTP_ACCEPT_CRS")
        if not accept_crs:
            # Allow the output format to overrule the default CRS.
            # e.g. GeoJSON defaults to WGS84, but we still allow the override.
            accept_crs = getattr(request.accepted_renderer, "default_crs", None)
            if accept_crs:
                request.accept_crs = accept_crs
        else:
            request.accept_crs = self._parse_accept_crs(accept_crs)

    @property
    def table_schema(self) -> DatasetTableSchema:
        return self.model._table_schema

    def _parse_accept_crs(self, http_value) -> Optional[crs.CRS]:
        """Parse the HTTP Accept-Crs header.

        Clients provide this header to indicate which CRS
        they would like to have in the response.
        """
        if not http_value:
            if self.accept_crs_required:
                # This makes Accept-Crs mandatory
                raise PreconditionFailed("The HTTP Accept-Crs header is required")
            else:
                return None

        try:
            accept_crs = crs.CRS.from_string(http_value)
        except ValueError as e:
            raise NotAcceptable(f"Chosen CRS is invalid: {e}") from e

        if accept_crs not in self.accept_crs:
            raise NotAcceptable(f"Chosen CRS is not supported: {accept_crs}")
        return accept_crs

    def finalize_response(self, request, response, *args, **kwargs):
        """Set the Content-Crs header if there was a geometry field.

        Also restore streaming support if the output media uses generators.
        """
        # The logic from initial() won't be executed if there is an early parser exception.
        accept_crs = getattr(request, "accept_crs", None)
        content_crs = getattr(request, "response_content_crs", None) or accept_crs
        if content_crs is not None:
            response["Content-Crs"] = str(content_crs)

        response = super().finalize_response(request, response, *args, **kwargs)

        # Workaround for DRF bug. When the response produces a generator, make sure the
        # Django middleware doesn't concat the stream. Unfortunately, it's not safe to
        # check what 'response.rendered_content' returns as that invokes the rendering.
        data = response.data
        if isinstance(data, (ReturnGenerator, GeneratorType)) and isgeneratorfunction(
            response.accepted_renderer.render
        ):
            response = StreamingResponse.from_response(response)

        return response

    def get_view_description(self, **kwargs):
        if self.action == "retrieve":
            return ""
        return super().get_view_description(**kwargs)
