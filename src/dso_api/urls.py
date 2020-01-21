from django.conf import settings
from django.urls import include, path
from rest_framework import response, schemas
from rest_framework.decorators import api_view, renderer_classes
from rest_framework.renderers import CoreJSONRenderer
from rest_framework_swagger.renderers import OpenAPIRenderer
from rest_framework_swagger.renderers import SwaggerUIRenderer

urlpatterns = [
]


if 'debug_toolbar' in settings.INSTALLED_APPS:
    import debug_toolbar

    urlpatterns.extend([
        path('__debug__/', include(debug_toolbar.urls)),
    ])
