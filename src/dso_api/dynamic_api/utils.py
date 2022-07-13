from __future__ import annotations

import re
from functools import lru_cache

from django.db import models
from django.db.models import Model
from django.db.models.fields.related import RelatedField
from django.db.models.fields.reverse_related import ForeignObjectRel
from rest_framework import serializers
from schematools.contrib.django.models import LooseRelationField
from schematools.contrib.django.signals import dynamic_models_removed

from rest_framework_dso.fields import GeoJSONIdentifierField

PK_SPLIT = re.compile("[_.]")


def split_on_separator(value):
    """Split on the last separator, which can be a dot or underscore."""
    # reversal is king
    return [part[::-1] for part in PK_SPLIT.split(value[::-1], 1)][::-1]


# When models are removed, clear the cache.
dynamic_models_removed.connect(lambda **kwargs: resolve_model_lookup.cache_clear())


@lru_cache
def resolve_model_lookup(
    model: type[Model], lookup: str
) -> list[RelatedField | ForeignObjectRel | LooseRelationField]:
    """Find which fields a lookup points to.
    :returns: The model fields that the relation traverses.
    """
    if not lookup:
        raise ValueError("Empty lookup can't be resolved")

    fields = []
    for field_name in lookup.split("__"):
        field = model._meta.get_field(field_name)

        if isinstance(field, (RelatedField, ForeignObjectRel, LooseRelationField)):
            # RelatedField covers all forwards relations (ForeignKey, ManyToMany, OneToOne)
            # ForeignObjectRel covers backward relations (ManyToOneRel, ManyToManyRel, OneToOneRel)
            model = field.related_model
        else:
            raise ValueError(f"Field '{field}' is not a relation in lookup '{lookup}'")

        fields.append(field)

    return fields


def get_serializer_source_fields(  # noqa: C901
    serializer: serializers.BaseSerializer, prefix=""
) -> list[str]:
    """Find which ORM fields a serializer instance would request.

    It checks "serializer.fields", and analyzes each "field.source" to find
    the model attributes that would be read.
    This allows to prepare an ``only()`` call on the queryset.
    """
    # Unwrap the list serializer construct for the one-to-many relationships.
    if isinstance(serializer, serializers.ListSerializer):
        serializer = serializer.child

    lookups = []

    for field in serializer.fields.values():  # type: serializers.Field
        if field.source == "*":
            # Field takes the full object, only some cases are supported:
            if isinstance(field, serializers.BaseSerializer):
                # When a serializer receives the same data as the parent instance, it can be
                # seen as being a part of the parent. The _links field is implemented this way.
                lookups.extend(get_serializer_source_fields(field, prefix=prefix))
            elif isinstance(field, serializers.HyperlinkedRelatedField):
                # Links to an object e.g. TemporalHyperlinkedRelatedField
                # When the lookup_field matches the identifier, there is no need
                # to add the field because the parent name is already includes it.
                if field.lookup_field != "pk":
                    lookups.append(f"{prefix}{field.lookup_field}")
            elif field.field_name == "schema" or isinstance(field, GeoJSONIdentifierField):
                # Fields that do have "source=*", but don't read any additional fields.
                # e.g. SerializerMethodField for schema, and GeoJSON ID field.
                continue
            elif isinstance(field, serializers.CharField):
                # A CharField(source="*") that either receives a str from its parent
                # (e.g. _loose_link_serializer_factory() receives a str as data),
                # or it's reading DynamicModel.__str__().
                if isinstance(serializer, serializers.ModelSerializer) and (
                    display_field := serializer.Meta.model.table_schema().display_field
                ):
                    lookups.append(f"{prefix}{display_field}")
                continue
            else:
                raise NotImplementedError(
                    f"Can't determine .only() for {prefix}{field.field_name}"
                    f" ({field.__class__.__name__})"
                )
        else:
            # Check the field isn't a reverse field, this should not be mentioned at all
            # because such field doesn't access a local field (besides the primary key).
            model_fields = get_source_model_fields(serializer, field.field_name, field)
            if isinstance(model_fields[0], models.ForeignObjectRel):
                continue

            # Regular field: add the source value to the list.
            lookup = f"{prefix}{'__'.join(field.source_attrs)}"
            lookups.append(lookup)

            if isinstance(field, (serializers.ModelSerializer, serializers.ListSerializer)):
                lookups.extend(get_serializer_source_fields(field, prefix=f"{lookup}__"))

    # Deduplicate the final result, as embedded fields could overlap with _links.
    return sorted(set(lookups)) if not prefix else lookups


def get_source_model_fields(
    serializer: serializers.ModelSerializer, field_name: str, field: serializers.Field
) -> list[models.Field]:
    """Find the model fields that the serializer field points to.
    Typically this is only one field, but `field.source` could be set to a dotted path.
    """
    if field.source == "*":
        # These fields are special: they receive the entire model instead of a attribute value.
        raise ValueError("Unable to detect source for field.source == '*'")

    orm_path = []

    # Typically, `field.parent` and `field.source_attrs` are already set, making those arguments
    # unnecessary. However, most use-cases of this function involve inspecting model data earlier
    # in an override of serializer.get_fields(), which is before field.bind() is called.
    model = serializer.Meta.model
    source_attrs = getattr(field, "source_attrs", None) or (field.source or field_name).split(".")

    for attr in source_attrs:
        model_field = model._meta.get_field(attr)
        model = model_field.related_model
        orm_path.append(model_field)

    return orm_path
