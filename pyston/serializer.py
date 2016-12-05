from __future__ import unicode_literals

import decimal
import datetime
import inspect
import six

from collections import OrderedDict

from django.conf import settings
from django.db.models import Model
from django.db.models.query import QuerySet
from django.db.models.fields.files import FileField

try:
    from django.db.models.fields.related import ForeignRelatedObjectsDescriptor, SingleRelatedObjectDescriptor
except ImportError:
    from django.db.models.fields.related import (ReverseManyToOneDescriptor as ForeignRelatedObjectsDescriptor,
                                                 ReverseOneToOneDescriptor as SingleRelatedObjectDescriptor)

from django.utils import formats, timezone
from django.utils.encoding import force_text
from django.utils.translation import ugettext
from django.utils.html import conditional_escape

from chamber.utils.datastructures import Enum
from chamber.utils import get_class_method

from .exception import MimerDataException, UnsupportedMediaTypeException
from .utils import coerce_put_post, rfs
from .utils.compatibility import get_reverse_field_name, get_last_parent_pk_field_name
from .converter import get_converter_name_from_request, get_converter_from_request, get_converter


DEFAULT_CONVERTER_OPTIONS = {
    'json': {
        'indent': 4
    }
}


value_serializers = []


def register(klass):
    """
    Adds throttling validator to a class.
    """
    for serializer in value_serializers:
        if type(serializer) == klass:
            return None
    value_serializers.insert(0, klass())
    return klass


class RawVerboseValue(object):
    """
    Return RAW, VERBOSE or BOTH values according to serialization type
    """

    def __init__(self, raw_value, verbose_value):
        self.raw_value = raw_value
        self.verbose_value = verbose_value

    def get_value(self, serialization_format):
        if serialization_format == Serializer.SERIALIZATION_TYPES.RAW:
            return self.raw_value
        elif serialization_format == Serializer.SERIALIZATION_TYPES.VERBOSE:
            return self.verbose_value
        elif self.raw_value == self.verbose_value:
            return self.raw_value
        else:
            return {'_raw': self.raw_value, '_verbose': self.verbose_value}


class Serializer(object):
    """
    REST serializer and deserializer, firstly is data serialized to standard python data types and after that is
    used convertor for final serialization
    """

    SERIALIZATION_TYPES = Enum('VERBOSE', 'RAW', 'BOTH')

    def _get_resource(self, request, obj):
        from .resource import typemapper

        resource_class = typemapper.get(type(obj))
        if resource_class:
            return resource_class(request)

    def _to_python_via_resource(self, thing, serialization_format, request=None, **kwargs):
        resource = self._get_resource(request, thing)
        if resource:
            thing._resource = resource
            return resource.serializer(resource)._to_python(thing, serialization_format, request=request, **kwargs)
        else:
            return None

    def _find_to_serializer(self, thing):
        for serializer in value_serializers:
            if serializer._can_transform_to_python(thing):
                return serializer

    def _to_python_chain(self, thing, serialization_format, **kwargs):
        if 'request' in kwargs and not hasattr(thing, '_resource'):
            result = self._to_python_via_resource(thing, serialization_format, **kwargs)
            if result:
                return result
        serializer = self._find_to_serializer(thing)
        if serializer:
            return serializer._to_python(thing, serialization_format, **kwargs)
        raise NotImplementedError('Serializer not found for %s' % thing)

    def _to_python(self, thing, serialization_format, **kwargs):
        return self._to_python_chain(thing, serialization_format, **kwargs)

    def _can_transform_to_python(self, thing):
        raise NotImplementedError


class ResourceSerializer(Serializer):
    """
    Default resource serializer perform serialization to th client format
    """

    def __init__(self, resource):
        self.resource = resource

    def serialize(self, request, result, requested_fieldset, serialization_format,
                  direct_serialization=False):
        detailed = self.resource._is_single_obj_request(result)
        converted_dict = self._to_python(result, serialization_format,
                                         requested_fieldset=requested_fieldset,
                                         detailed=detailed, request=request,
                                         direct_serialization=direct_serialization)
        try:
            converter_name = get_converter_name_from_request(request)
        except ValueError:
            raise UnsupportedMediaTypeException

        converter, ct = get_converter(converter_name)
        converter_options = getattr(settings, 'DEFAULT_CONVERTER_OPTIONS',
                                    DEFAULT_CONVERTER_OPTIONS).get(converter_name, {})
        return converter().encode(converted_dict, converter_options, resource=self.resource,
                                  fields_string=request._rest_context.get('fields')), ct

    def deserialize(self, request):
        rm = request.method.upper()
        # Django's internal mechanism doesn't pick up
        # PUT request, so we trick it a little here.
        if rm == 'PUT':
            coerce_put_post(request)

        if rm in {'POST', 'PUT'}:
            try:
                converter, _ = get_converter_from_request(request, True)
                request.data = converter().decode(force_text(request.body))
            except (TypeError, ValueError):
                raise MimerDataException
            except NotImplementedError:
                raise UnsupportedMediaTypeException
        return request

    def _to_python(self, thing, serialization_format, **kwargs):
        return super(ResourceSerializer, self)._to_python(thing, serialization_format, **kwargs)


@register
class StringSerializer(Serializer):

    def _to_python(self, thing, serialization_format, **kwargs):
        res = force_text(thing, strings_only=True)
        if isinstance(res, six.string_types):
            return conditional_escape(res)
        else:
            return res

    def _can_transform_to_python(self, thing):
        return True


@register
class DateTimeSerializer(Serializer):

    def _to_python(self, thing, serialization_format, **kwargs):
        return timezone.localtime(thing)

    def _can_transform_to_python(self, thing):
        return isinstance(thing, datetime.datetime)


@register
class DictSerializer(Serializer):

    def _to_python(self, thing, serialization_format, requested_fieldset=None,
                   extended_fieldset=None, detailed=False, exclude_fields=None, **kwargs):
        return dict([(k, self._to_python_chain(v, serialization_format, **kwargs))
                     for k, v in thing.items()])

    def _can_transform_to_python(self, thing):
        return isinstance(thing, dict)


@register
class ListSerializer(Serializer):

    def _to_python(self, thing, serialization_format, requested_fieldset=None,
                   extended_fieldset=None, detailed=False, exclude_fields=None, **kwargs):
        return [self._to_python_chain(v, serialization_format, **kwargs) for v in thing]

    def _can_transform_to_python(self, thing):
        return isinstance(thing, (list, tuple, set))


@register
class QuerySetSerializer(Serializer):

    def _to_python(self, thing, serialization_format, **kwargs):
        return [self._to_python_chain(v, serialization_format, **kwargs) for v in thing]

    def _can_transform_to_python(self, thing):
        return isinstance(thing, QuerySet)


@register
class DecimalSerializer(Serializer):

    def _to_python(self, thing, serialization_format, **kwargs):
        return thing

    def _can_transform_to_python(self, thing):
        return isinstance(thing, decimal.Decimal)


@register
class RawVerboseSerializer(Serializer):

    def _to_python(self, thing, serialization_format, **kwargs):
        return self._to_python_chain(thing.get_value(serialization_format), serialization_format, **kwargs)

    def _can_transform_to_python(self, thing):
        return isinstance(thing, RawVerboseValue)


@register
class ModelSerializer(Serializer):

    RESERVED_FIELDS = {'read', 'update', 'create', 'delete', 'model', 'allowed_methods', 'fields', 'exclude'}

    def _get_resource_method_fields(self, resource, fields):
        out = {}
        for field in fields.flat() - self.RESERVED_FIELDS:
            t = getattr(resource, str(field), None)
            if t and callable(t):
                out[field] = t
        return out

    def _get_model_fields(self, obj):
        out = {}
        for f in obj._meta.fields:
            if hasattr(f, 'serialize') and f.serialize:
                out[f.name] = f
        return out

    def _get_m2m_fields(self, obj):
        out = {}
        for mf in obj._meta.many_to_many:
            if mf.serialize:
                out[mf.name] = mf
        return out

    def _get_verbose_value(self, raw, field_or_method, obj, **kwargs):
        if hasattr(field_or_method, 'humanized') and field_or_method.humanized:
            return field_or_method.humanized(raw, obj, **kwargs)
        elif hasattr(field_or_method, 'choices') and field_or_method.choices:
            return getattr(obj, 'get_{}_display'.format(field_or_method.attname))()
        if isinstance(raw, bool):
            return raw and ugettext('Yes') or ugettext('No')
        elif isinstance(raw, datetime.datetime):
            return formats.localize(timezone.template_localtime(raw))
        elif isinstance(raw, (datetime.date, datetime.time)):
            return formats.localize(raw)
        else:
            return raw

    def _value_to_raw_verbose(self, val, field_or_method, obj, **kwargs):
        return RawVerboseValue(val, self._get_verbose_value(val, field_or_method, obj, **kwargs))

    def _method_to_python(self, method, obj, serialization_format, **kwargs):
        method_kwargs_names = inspect.getargspec(method)[0][1:]

        method_kwargs = {}

        fun_kwargs = {'request': kwargs.get('request'), 'obj': obj} if 'request' in kwargs else {'obj': obj}

        for arg_name in method_kwargs_names:
            if arg_name in fun_kwargs:
                method_kwargs[arg_name] = fun_kwargs[arg_name]

        if len(method_kwargs_names) == len(method_kwargs):
            return self._to_python_chain(
                self._value_to_raw_verbose(method(**method_kwargs), method, obj,
                                         **{k: v for k, v in method_kwargs.items() if k != 'obj'}),
                serialization_format, allow_tags=getattr(method, 'allow_tags', False), **kwargs
            )

    def _model_field_to_python(self, field, obj, serialization_format, **kwargs):
        return self._to_python_chain(self._value_to_raw_verbose(self._get_model_field_raw_value(obj, field), field, obj)
                                     if not field.rel else getattr(obj, field.name),
                                     serialization_format, allow_tags=getattr(field, 'allow_tags', False), **kwargs)

    def _m2m_field_to_python(self, field, obj, serialization_format, **kwargs):
        return [self._to_python_chain(m, serialization_format, allow_tags=getattr(field, 'allow_tags', False), **kwargs)
                for m in getattr(obj, field.name).all()]

    def _get_reverse_excluded_fields(self, field, obj):
        model = obj.__class__
        exclude_fields = []
        if hasattr(model, field) and isinstance(getattr(model, field, None),
                                                (ForeignRelatedObjectsDescriptor, SingleRelatedObjectDescriptor)):
            exclude_fields.append(get_reverse_field_name(model, field))

        return exclude_fields

    def _reverse_qs_to_python(self, val, field, obj, serialization_format, **kwargs):
        kwargs['exclude_fields'] = self._get_reverse_excluded_fields(field, obj)
        return [self._to_python_chain(m, serialization_format, **kwargs) for m in val.all()]

    def _reverse_to_python(self, val, field, obj, serialization_format, **kwargs):
        kwargs['exclude_fields'] = self._get_reverse_excluded_fields(field, obj)
        return self._to_python_chain(val, serialization_format, **kwargs)

    def _copy_kwargs(self, resource, kwargs):
        subkwargs = kwargs.copy()
        subkwargs['exclude_fields'] = None
        subkwargs['via'] = resource._get_via(kwargs.get('via')) if resource else kwargs.get('via')
        return subkwargs

    def _get_field_name(self, field, requested_field, subkwargs):
        if field.subfieldset:
            field_name, subkwargs['extended_fieldset'] = field.name, field.subfieldset
        else:
            field_name, subkwargs['extended_fieldset'] = field.name, None

        if requested_field and requested_field.subfieldset:
            subkwargs['requested_fieldset'] = requested_field.subfieldset
        elif field.subfieldset:
            subkwargs['requested_fieldset'] = field.subfieldset

        return field_name

    def _get_model_field_raw_value(self, obj, field):
        val = getattr(obj, field.attname)
        if isinstance(field, FileField):
            # FileField returns blank string if file does not exists, None is better
            val = val and val.url or None
        return val

    def _field_to_python(self, field_name, resource_method_fields, model_fields, m2m_fields,
                         obj, serialization_format, **kwargs):
        if field_name == '_obj_name':
            return force_text(obj)
        elif field_name in resource_method_fields:
            return self._method_to_python(resource_method_fields[field_name], obj, serialization_format,
                                          **kwargs)
        elif field_name in m2m_fields:
            return self._m2m_field_to_python(m2m_fields[field_name], obj, serialization_format, **kwargs)
        elif field_name in model_fields:
            return self._model_field_to_python(model_fields[field_name], obj, serialization_format, **kwargs)
        else:
            val = getattr(obj, field_name, None) if hasattr(obj, field_name) else None
            if hasattr(val, 'all'):
                return self._reverse_qs_to_python(val, field_name, obj, serialization_format, **kwargs)
            elif isinstance(val, Model):
                return self._reverse_to_python(val, field_name, obj, serialization_format, **kwargs)
            elif callable(val):
                return self._method_to_python(val, obj, serialization_format, **kwargs)
            else:
                method = get_class_method(obj, field_name)
                return self._to_python_chain(self._value_to_raw_verbose(val, method, obj), serialization_format,
                                             allow_tags=method is not None and getattr(method, 'allow_tags', False),
                                             **kwargs)

    def _fields_to_python(self, obj, serialization_format, fieldset, requested_fieldset, **kwargs):
        model_resource = self._get_model_resource(obj)
        resource_method_fields = self._get_resource_method_fields(model_resource, fieldset)
        model_fields = self._get_model_fields(obj)
        m2m_fields = self._get_m2m_fields(obj)

        out = OrderedDict()

        for field in fieldset.fields:
            subkwargs = self._copy_kwargs(model_resource, kwargs)
            requested_field = None
            if requested_fieldset:
                requested_field = requested_fieldset.get(field.name)
            field_name = self._get_field_name(field, requested_field, subkwargs)
            out[field_name] = self._field_to_python(
                field_name, resource_method_fields, model_fields, m2m_fields, obj, serialization_format, **subkwargs
            )

        return out

    def _get_model_resource(self, obj):
        if hasattr(obj, '_resource'):
            return obj._resource

    def _get_fieldset_from_resource(self, model_resource, obj, via, detailed, has_get_permission):
        if not has_get_permission:
            return model_resource.get_guest_fields(obj)
        elif detailed:
            return model_resource.get_default_detailed_fields(obj)
        else:
            return model_resource.get_default_general_fields(obj)

    def _get_allowed_fieldset_from_resource(self, model_resource, obj, via, has_get_permission):
        if not has_get_permission:
            return model_resource.get_guest_fields(obj)
        else:
            return model_resource.get_fields(obj)

    def _get_fieldset(self, obj, extended_fieldset, requested_fieldset, exclude_fields, via, detailed,
                      direct_serialization, serialized_objects):

        if self._get_obj_serialization_name(obj) in serialized_objects:
            return rfs((get_last_parent_pk_field_name(obj),))

        model_resource = self._get_model_resource(obj)

        if model_resource:
            has_get_permission = (model_resource.has_get_permission(obj, via) or
                                  model_resource.has_post_permission(obj, via) or
                                  model_resource.has_put_permission(obj, via))
            default_fieldset = self._get_fieldset_from_resource(model_resource, obj, via, detailed, has_get_permission)
            allowed_fieldset = self._get_allowed_fieldset_from_resource(model_resource, obj, via, has_get_permission)
        else:
            allowed_fieldset = (
                (requested_fieldset if requested_fieldset else rfs(
                    obj._rest_meta.extra_fields
                 ).join(rfs(obj._rest_meta.default_general_fields)).join(
                    rfs(obj._rest_meta.default_detailed_fields)
                 ).join(rfs(obj._rest_meta.direct_serialization_fields)))
                if direct_serialization else rfs(obj._rest_meta.guest_fields)
            )
            default_fieldset = (
                rfs(obj._rest_meta.direct_serialization_fields)
                if direct_serialization else rfs(obj._rest_meta.guest_fields)
            )

        if extended_fieldset:
            default_fieldset.join(extended_fieldset)
            allowed_fieldset.join(extended_fieldset)

        if requested_fieldset:
            # requested_fieldset must be cloned because RFS is not immutable and intersection change it
            fieldset = rfs(requested_fieldset).intersection(allowed_fieldset).extend_fields_fieldsets(default_fieldset)
        else:
            fieldset = default_fieldset.intersection(allowed_fieldset)

        if exclude_fields:
            fieldset.subtract(exclude_fields)
        return fieldset

    def _get_obj_serialization_name(self, obj):
        return '{}__{}'.format(obj._meta.db_table, obj.pk)

    def _to_python(self, obj, serialization_format, requested_fieldset=None, extended_fieldset=None, detailed=False,
                   exclude_fields=None, allow_tags=False, direct_serialization=False, serialized_objects=None,
                   **kwargs):
        exclude_fields = [] if exclude_fields is None else exclude_fields
        serialized_objects = set() if serialized_objects is None else set(serialized_objects)
        fieldset = self._get_fieldset(obj, extended_fieldset, requested_fieldset, exclude_fields,
                                      kwargs.get('via'), detailed, direct_serialization, serialized_objects)
        serialized_objects.add(self._get_obj_serialization_name(obj))
        return self._fields_to_python(obj, serialization_format, fieldset, requested_fieldset,
                                      serialized_objects=serialized_objects,
                                      direct_serialization=direct_serialization, **kwargs)

    def _can_transform_to_python(self, thing):
        return isinstance(thing, Model)


def serialize(data, requested_fieldset=None, serialization_format=Serializer.SERIALIZATION_TYPES.RAW,
              converter_name=None, converter_options=None):
    converter_name = (
        converter_name if converter_name is not None else getattr(settings, 'PYSTON_DEFAULT_CONVERTER', 'json')
    )
    requested_fieldset = rfs(requested_fieldset) if requested_fieldset is not None else None
    converted_dict = Serializer()._to_python(data, serialization_format, requested_fieldset=requested_fieldset,
                                             detailed=True, direct_serialization=True)
    if converter_name == 'python':
        return converted_dict
    else:
        try:
            converter, _ = get_converter(converter_name)
        except ValueError:
            raise UnsupportedMediaTypeException
        converter_options = (
            converter_options if converter_options is not None
            else getattr(settings, 'DEFAULT_DIRECT_SERIALIZATION_CONVERTER_OPTIONS', {}).get(converter_name, {})
        )
        return converter().encode(converted_dict, converter_options)
