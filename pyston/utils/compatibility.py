from __future__ import unicode_literals

from distutils.version import StrictVersion

import django
from django.template import Context
from django.template.loader import get_template
from django.core.exceptions import FieldError

try:
    from django.core.exceptions import FieldDoesNotExist
except ImportError:
    from django.db.models import FieldDoesNotExist


def get_field_or_none(model, field_name):
    try:
        return model._meta.get_field(field_name)
    except FieldDoesNotExist:
        return None


def get_all_related_objects_from_model(model):
    return model._meta.get_all_related_objects() if StrictVersion(django.get_version()) < StrictVersion('1.9') else [
        f for f in model._meta.get_fields()
        if (f.one_to_many or f.one_to_one) and f.auto_created and not f.concrete
    ]


def get_concrete_field(model, field_name):
    field = {f.name: f for f in model._meta.get_fields()
        if f.concrete and (
            not f.is_relation or f.one_to_one or (f.many_to_one and f.related_model)
        )
    }.get(field_name)
    if not field:
        raise FieldDoesNotExist
    else:
        return field


def is_reverse_many_to_one(model, field_name):
    field = get_field_or_none(model, field_name)
    return field and field.auto_created and field.one_to_many


def is_reverse_one_to_one(model, field_name):
    field = get_field_or_none(model, field_name)
    return field and field.auto_created and field.one_to_one


def is_reverse_many_to_many(model, field_name):
    field = get_field_or_none(model, field_name)
    return field and field.auto_created and field.many_to_many


def is_many_to_one(model, field_name):
    field = get_field_or_none(model, field_name)
    return field and not field.auto_created and field.many_to_one


def is_one_to_one(model, field_name):
    field = get_field_or_none(model, field_name)
    return field and not field.auto_created and field.one_to_one


def is_many_to_many(model, field_name):
    field = get_field_or_none(model, field_name)
    return field and not field.auto_created and field.many_to_many


def is_relation(model, field_name):
    return (
        is_one_to_one(model, field_name) or is_many_to_one(model, field_name) or is_many_to_many(model, field_name) or
        is_reverse_one_to_one(model, field_name) or is_reverse_many_to_one(model, field_name) or
        is_reverse_many_to_many(model, field_name)
    )


def is_single_related_descriptor(model, field_name):
    field = get_field_or_none(model, field_name)
    return field and field.auto_created and field.one_to_one


def is_multiple_related_descriptor(model, field_name):
    return field and field.auto_created and (field.many_to_many or field.one_to_many)


def is_related_descriptor(model, field_name):
    return is_single_related_descriptor(model, field_name) or is_multiple_related_descriptor(model, field_name)


def get_model_from_relation(model, field_name):
    if is_many_to_many(model, field_name) or is_one_to_one(model, field_name) or is_many_to_one(model, field_name):
        return getattr(model, field_name).field.rel.to
    elif StrictVersion(django.get_version()) >= StrictVersion('1.9') and is_reverse_many_to_many(model, field_name):
        return getattr(model, field_name).field.model
    elif StrictVersion(django.get_version()) >= StrictVersion('1.9') and is_reverse_many_to_one(model, field_name):
        return getattr(model, field_name).field.model
    elif (is_reverse_many_to_many(model, field_name) or is_reverse_one_to_one(model, field_name) or
            is_reverse_many_to_one(model, field_name)):
        return getattr(model, field_name).related.related_model
    elif (is_reverse_many_to_many(model, field_name) or is_reverse_one_to_one(model, field_name) or
            is_reverse_many_to_one(model, field_name)):
        return getattr(model, field_name).related.model
    else:
        raise FieldError('field {} is not relation'.format(field_name))


def get_model_from_relation_or_none(model, field_name):
    try:
        return get_model_from_relation(model, field_name)
    except FieldError:
        return None


def get_reverse_field_name(model, field_name):
    if is_many_to_many(model, field_name) or is_one_to_one(model, field_name) or is_many_to_one(model, field_name):
        return getattr(model, field_name).field.related_query_name()
    elif StrictVersion(django.get_version()) >= StrictVersion('1.9') and is_reverse_many_to_many(model, field_name):
        return getattr(model, field_name).field.name
    elif StrictVersion(django.get_version()) >= StrictVersion('1.9') and is_reverse_many_to_one(model, field_name):
        return getattr(model, field_name).field.name
    elif is_reverse_one_to_one(model, field_name):
        return getattr(model, field_name).related.field.name
    elif (is_reverse_many_to_many(model, field_name) or is_reverse_one_to_one(model, field_name) or
            is_reverse_many_to_one(model, field_name)):
        return getattr(model, field_name).related.field.name
    else:
        raise FieldError('field {} is not relation'.format(field_name))


def render_template(template_name, context):
    if StrictVersion(django.get_version()) < StrictVersion('1.9'):
        context = Context(context)
    return get_template(template_name).render(context)


def get_last_parent_pk_field_name(obj):
    for field in obj._meta.fields:
        if field.primary_key and (not field.is_relation or not field.auto_created):
            return field.name
    raise RuntimeError('Last parent field name was not found (cannot happen)')
