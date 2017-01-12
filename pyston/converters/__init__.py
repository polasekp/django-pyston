from __future__ import unicode_literals

import types
import json

from six.moves import cStringIO

from collections import OrderedDict

from django.core.serializers.json import DateTimeAwareJSONEncoder
from django.http.response import HttpResponseBase
from django.template.loader import get_template
from django.utils.encoding import force_text
from django.utils.xmlutils import SimplerXMLGenerator
from django.utils.module_loading import import_string
from django.utils.html import format_html

from pyston.utils.helpers import UniversalBytesIO, serialized_data_to_python
from pyston.utils.datastructures import FieldsetGenerator
from pyston.conf import settings

from .file_generators import CSVGenerator, XLSXGenerator, PDFGenerator


converters = OrderedDict()


def register_converters():
    """
    Register all converters from settings configuration.
    """
    for converter_class_path in settings.CONVERTERS:
        converter_class = import_string(converter_class_path)()
        converters[converter_class.format] = converter_class


def get_default_converter_name():
    """
    Gets default converter name
    """
    if not converters:
        register_converters()

    return list(converters.keys())[0]


def get_converter(result_format):
    """
    Gets an converter, returns the class and a content-type.
    """
    if not converters:
        register_converters()

    if result_format in converters:
        return converters.get(result_format)
    else:
        raise ValueError('No converter found for type {}'.format(result_format))


def get_converter_name_from_request(request, input_serialization=False):
    """
    Function for determining which converter name to use
    for output.
    """
    if not converters:
        register_converters()

    try:
        import mimeparse
    except ImportError:
        mimeparse = None

    context_key = 'accept'
    if input_serialization:
        context_key = 'content_type'

    default_converter_name = get_default_converter_name()

    if mimeparse and context_key in request._rest_context:
        supported_mime_types = set()
        converter_map = {}
        preferred_content_type = None
        for name, converter_class in converters.items():
            if name == default_converter_name:
                preferred_content_type = converter_class.media_type
            supported_mime_types.add(converter_class.media_type)
            converter_map[converter_class.media_type] = name
        supported_mime_types = list(supported_mime_types)
        if preferred_content_type:
            supported_mime_types.append(preferred_content_type)
        try:
            preferred_content_type = mimeparse.best_match(supported_mime_types,
                                                          request._rest_context[context_key])
        except ValueError:
            pass
        default_converter_name = converter_map.get(preferred_content_type, default_converter_name)
    return default_converter_name


def get_converter_from_request(request, input_serialization=False):
    """
    Function for determining which converter name to use
    for output.
    """

    return get_converter(get_converter_name_from_request(request, input_serialization))


def get_supported_mime_types():
    return [converter.media_type for _, converter in converters.items()]


class Converter(object):
    """
    Converter from standard data types to output format (JSON,YAML, Pickle) and from input to python objects
    """
    charset = 'utf-8'
    media_type = None
    format = None

    @property
    def content_type(self):
        return '; '.join((self.media_type, self.charset))

    def _encode(self, data, options=None, **kwargs):
        """
        Encodes data to output string. You must implement this method or change implementation encode_to_stream method.
        """
        raise NotImplementedError

    def _decode(self, data, **kwargs):
        """
        Decodes data to string input
        """
        raise NotImplementedError

    def _encode_to_stream(self, os, data, options=None, **kwargs):
        """
        Encodes data and writes it to the output stream
        """
        os.write(self._encode(data, options=options, **kwargs))

    def encode_to_stream(self, os, data, options=None, **kwargs):
        self._encode_to_stream(self._get_output_stream(os), data, options=options, **kwargs)

    def decode(self, data, **kwargs):
        return self._decode(data, **kwargs)

    def _get_output_stream(self, os):
        return os if isinstance(os, UniversalBytesIO) else UniversalBytesIO(os)


class XMLConverter(Converter):
    """
    Converter for XML.
    Supports only output conversion
    """
    media_type = 'text/xml'
    format = 'xml'

    def _to_xml(self, xml, data):
        from pyston.serializer import LazySerializedData

        if isinstance(data, LazySerializedData):
            self._to_xml(xml, data.serialize())
        elif isinstance(data, (list, tuple, set, types.GeneratorType)):
            for item in data:
                xml.startElement('resource', {})
                self._to_xml(xml, item)
                xml.endElement('resource')
        elif isinstance(data, dict):
            for key, value in data.items():
                xml.startElement(key, {})
                self._to_xml(xml, value)
                xml.endElement(key)
        else:
            xml.characters(force_text(data))

    def _encode(self, data, **kwargs):
        if data is not None:
            stream = cStringIO()

            xml = SimplerXMLGenerator(stream, 'utf-8')
            xml.startDocument()
            xml.startElement('response', {})

            self._to_xml(xml, data)

            xml.endElement('response')
            xml.endDocument()

            return stream.getvalue()
        else:
            return ''


class LazyDateTimeAwareJSONEncoder(DateTimeAwareJSONEncoder):

    def default(self, o):
        from pyston.serializer import LazySerializedData

        if isinstance(o, types.GeneratorType):
            return tuple(o)
        elif isinstance(o, LazySerializedData):
            return o.serialize()
        else:
            return super(LazyDateTimeAwareJSONEncoder, self).default(o)


class JSONConverter(Converter):
    """
    JSON emitter, understands timestamps.
    """
    media_type = 'application/json'
    format = 'json'

    def _encode_to_stream(self, os, data, options=None, **kwargs):
        options = settings.JSON_CONVERTER_OPTIONS if options is None else options
        if data is not None:
            json.dump(data, os, cls=LazyDateTimeAwareJSONEncoder, ensure_ascii=False, **options)

    def _decode(self, data, **kwargs):
        return json.loads(data)


class GeneratorConverter(Converter):
    """
    Generator converter is more complicated.
    Contains user readable informations (headers).
    Supports only output.
    Output is flat.

    It is necessary set generator_class as class attribute

    This class contains little bit low-level implementation
    """

    generator_class = None

    def _render_headers(self, field_name_list):
        result = []
        if len(field_name_list) == 1 and '' in field_name_list:
            return result

        for field_name in field_name_list:
            result.append(field_name)
        return result

    def _get_recursive_value_from_row(self, data, key_path):
        from pyston.serializer import LazySerializedData

        if isinstance(data, LazySerializedData):
            return self._get_recursive_value_from_row(data.serialize(), key_path)
        elif len(key_path) == 0:
            return data
        elif isinstance(data, dict):
            return self._get_recursive_value_from_row(data.get(key_path[0], ''), key_path[1:])
        elif isinstance(data, (list, tuple, set)):
            return [self._get_recursive_value_from_row(val, key_path) for val in data]
        else:
            return ''

    def render_value(self, value, first=True):
        if isinstance(value, dict):
            return '(%s)' % ', '.join(['%s: %s' % (key, self.render_value(val, False)) for key, val in value.items()])
        elif isinstance(value, (list, tuple, set)):
            if first:
                return '\n'.join([self.render_value(val, False) for val in value])
            else:
                return '(%s)' % ', '.join([self.render_value(val, False) for val in value])
        else:
            return force_text(value)

    def _get_value_from_row(self, data, field):
        return self.render_value(self._get_recursive_value_from_row(data, field.key_path) or '')

    def _render_row(self, row, field_name_list):
        return (self._get_value_from_row(row, field) for field in field_name_list)

    def _render_content(self, field_name_list, converted_data):
        constructed_data = converted_data
        if not isinstance(constructed_data, (list, tuple, set, types.GeneratorType)):
            constructed_data = [constructed_data]

        return (self._render_row(row, field_name_list) for row in constructed_data)

    def _encode_to_stream(self, os, data, resource=None, fields_string=None, **kwargs):
        fieldset = FieldsetGenerator(resource, fields_string).generate()
        self.generator_class().generate(
            self._render_headers(fieldset),
            self._render_content(fieldset, data),
            os
        )


class CSVConverter(GeneratorConverter):
    """
    Converter for CSV response.
    Supports only output conversion
    """

    generator_class = CSVGenerator
    media_type = 'text/csv'
    format = 'csv'


class XLSXConverter(GeneratorConverter):
    """
    Converter for XLSX response.
    For its use must be installed library xlsxwriter
    Supports only output conversion
    """

    generator_class = XLSXGenerator
    media_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    format = 'xlsx'


class PDFConverter(GeneratorConverter):
    """
    Converter for PDF response.
    For its use must be installed library pisa
    Supports only output conversion
    """

    generator_class = PDFGenerator
    media_type = 'application/pdf'
    format = 'pdf'


class HTMLConverter(Converter):
    """
    Converter for HTML.
    Supports only output conversion and should be used only for debug
    """

    media_type = 'text/html'
    format = 'html'
    template_name = 'pyston/html_converter.html'

    def _get_put_form(self, resource, obj):
        from pyston.resource import BaseObjectResource

        return (
            resource._get_form(inst=obj)
            if isinstance(resource, BaseObjectResource) and resource.has_put_permission(obj=obj)
            else None
        )

    def _get_post_form(self, resource, obj):
        from pyston.resource import BaseObjectResource

        return (
            resource._get_form(inst=obj)
            if isinstance(resource, BaseObjectResource) and resource.has_post_permission(obj=obj)
            else None
        )

    def _get_forms(self, resource, obj):
        return {
            'post': self._get_post_form(resource, obj),
            'put': self._get_put_form(resource, obj),
        }

    def _get_converter(self, resource):
        return JSONConverter()

    def _get_permissions(self, resource, obj):
        return {
            'post': resource.has_post_permission(obj=obj),
            'get': resource.has_get_permission(obj=obj),
            'put': resource.has_put_permission(obj=obj),
            'delete': resource.has_delete_permission(obj=obj),
            'head': resource.has_head_permission(obj=obj),
            'options': resource.has_options_permission(obj=obj),
        }

    def _update_headers(self, http_headers, resource, converter):
        http_headers['Content-Type'] = converter.content_type
        return http_headers

    def encode_to_stream(self, os, data, options=None, **kwargs):
        assert os is not HttpResponseBase, 'Output stream must be http response'

        self._get_output_stream(os).write(self._encode(data, response=os, options=options, **kwargs))

    def _convert_url_to_links(self, data):
        if isinstance(data, list):
            return [self._convert_url_to_links(val) for val in data]
        elif isinstance(data, dict):
            return OrderedDict((
                (key, format_html('<a href=\'{0}\'>{0}</a>', val) if key == 'url' else self._convert_url_to_links(val))
                for key, val in data.items()
            ))
        else:
            return data

    def _encode(self, data, response=None, http_headers=None, resource=None, result=None, **kwargs):
        assert resource is not None, 'HTML converter requires resource and cannot be used as a direct serializer'

        http_headers = {} if http_headers is None else http_headers.copy()
        converter = self._get_converter(resource)
        http_headers = self._update_headers(http_headers, resource, converter)
        obj = resource._get_obj_or_none()

        kwargs.update({
            'http_headers': http_headers,
            'resource': resource,
        })

        data_stream = UniversalBytesIO()
        converter._encode_to_stream(data_stream, self._convert_url_to_links(serialized_data_to_python(data)), **kwargs)

        context = kwargs.copy()
        context.update({
            'permissions': self._get_permissions(resource, obj),
            'forms': self._get_forms(resource, obj),
            'output': data_stream.get_string_value(),
        })

        # All responses has set 200 response code, because response can return status code without content (204) and
        # browser doesn't render it
        response.status_code = 200
        return get_template(self.template_name).render(context, request=resource.request)