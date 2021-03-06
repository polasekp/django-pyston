from __future__ import unicode_literals

from chamber.patch import Options
from django.db import models

from pyston.utils.compatibility import get_last_parent_pk_field_name


def merge_iterable(a, b):
    c = list(a) + list(b)
    return sorted(set(c), key=lambda x: c.index(x))


class RESTOptions(Options):

    model_class = models.Model
    meta_class_name = 'RESTMeta'
    meta_name = '_rest_meta'
    attributes = {}

    def __init__(self, model):
        super(RESTOptions, self).__init__(model)
        pk_field_name = get_last_parent_pk_field_name(model)

        fields = self._getattr('fields', ())

        self.default_fields = self._getattr('default_fields', (pk_field_name, '_obj_name'))
        self.detailed_fields = self._getattr('detailed_fields', fields)
        self.general_fields = self._getattr('general_fields', fields)
        self.direct_serialization_fields = self._getattr('direct_serialization_fields', fields)
        self.extra_fields = merge_iterable(
            self._getattr('extra_fields', ()),
            set(fields) - set(self.detailed_fields) - set(self.general_fields) - set(self.default_fields)
        )
        self.guest_fields = self._getattr('guest_fields', (pk_field_name, '_obj_name'))
