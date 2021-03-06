from __future__ import unicode_literals

import types
import sys
import six

from six import BytesIO

from collections import OrderedDict

from django.utils.encoding import force_bytes
from django.conf import settings


class QuerysetIteratorHelper(object):

    def __init__(self, queryset):
        self.queryset = queryset

    def iterator(self):
        return iter(self.queryset.iterator())

    @property
    def model(self):
        return self.queryset.model


class UniversalBytesIO(object):

    def __init__(self, container=None, charset=None):
        self.charset = charset or settings.DEFAULT_CHARSET
        self._container = BytesIO() if container is None else container

    # These methods partially implement the file-like object interface.
    # See https://docs.python.org/3/library/io.html#io.IOBase

    def close(self):
        self._container.close()

    def write(self, content):
        self._container.write(self.make_bytes(content))

    def flush(self):
        self._container.flush()

    def tell(self):
        return self._container.tell()

    def readable(self):
        return False

    def seekable(self):
        return False

    def writable(self):
        return True

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def make_bytes(self, value):
        """Turn a value into a bytestring encoded in the output charset."""
        if isinstance(value, bytes):
            return bytes(value)
        if isinstance(value, six.text_type):
            return bytes(value.encode(self.charset))

        # Handle non-string types
        return force_bytes(value, self.charset)

    def get_string_value(self):
        return self._container.getvalue().decode(self.charset)

    def getvalue(self):
        return self._container.getvalue()

    if sys.version_info[0:2] < (3, 5):
        def seek(self, *args, **kwargs):
            pass


def serialized_data_to_python(data):
    from pyston.serializer import LazySerializedData

    if isinstance(data, (types.GeneratorType, list, tuple)):
        return [serialized_data_to_python(val) for val in data]
    elif isinstance(data, LazySerializedData):
        return serialized_data_to_python(data.serialize())
    elif isinstance(data, dict):
        return OrderedDict(((key, serialized_data_to_python(val)) for key, val in data.items()))
    else:
        return data