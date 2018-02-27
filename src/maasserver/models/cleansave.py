# Copyright 2012-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Model mixin: track field states and `full_clean` on every `save`."""

from copy import copy

from django.core.exceptions import FieldDoesNotExist


__all__ = [
    'CleanSave',
    ]


# Used to track that the field was unset.
FieldUnset = object()


class CleanSaveState:
    """Provides provide state info for a models fields."""

    def __init__(self, obj):
        self._obj = obj

    def get_changed(self):
        """Return set of all fields that have changed."""
        return set(self._obj._state._changed_fields)

    def has_changed(self, name):
        """Return `True` if field with `name` has changed."""
        return name in self._obj._state._changed_fields

    def has_any_changed(self, names):
        """Return `True` if any of the provided field names have changed."""
        return max([
            name in self._obj._state._changed_fields
            for name in names
        ])

    def get_old_value(self, name):
        """
        Return the old value for the field with `name`.

        This is the value that was in the database when this object was loaded.
        If the field has not changed it returns the fields current value.
        """
        if self.has_changed(name):
            return self._obj._state._changed_fields[name]
        return getattr(self._obj, name)


class CleanSave:
    """Mixin for model classes.

    This adds field state tracking, a call to `self.full_clean` to every
    `save` and only saving changed fields to the database. With tracking
    code can perform actions and checks based on what has changed instead
    of against the whole model. The `self.full_clean` before save ensures that
    a model cannot be saved in a bad state. `self.full_clean` should only
    do checks based on changed fields to reduce the required work and the
    query count.

    Derive your model from :class:`CleanSave` before deriving from
    :class:`django.db.models.Model` if you need field state tracking and for
    `self.full_clean` to happen before the real `save` to the database.

    .. _compatibility: https://code.djangoproject.com/ticket/13100#comment:2
    """

    def __marked_changed(self, name, old_value, new_value):
        """Marks the field changed or not depending on the values."""
        #print('%s - %s = %s -> %s' % (type(self).__name__, name, old_value, new_value))
        if old_value != new_value:
            if name in self._state._changed_fields:
                if self._state._changed_fields[name] == new_value:
                    # Reverted, no longer changed.
                    self._state._changed_fields.pop(name)
            else:
                try:
                    self._state._changed_fields[name] = copy(old_value)
                except TypeError:
                    # Object cannot be copied so just set it to the old value.
                    self._state._changed_fields[name] = old_value
        #print('%s - changed %s' % (type(self).__name__, self._state._changed_fields))

    def __setattr__(self, name, value):
        """Track the fields that have changed."""
        # Prepare the field tracking inside the `_state`. Don't track until
        # the `_state` is set on the model.
        if name == '_state':
            value._changed_fields = {}
            return super(CleanSave, self).__setattr__(name, value)
        if not hasattr(self, '_state'):
            return super(CleanSave, self).__setattr__(name, value)

        #print('%s - %s:%s' % (type(self).__name__, name, value))
        try:
            field = self._meta.get_field(name)
        except FieldDoesNotExist:
            prop_obj = getattr(self.__class__, name, None)
            if isinstance(prop_obj, property):
                if prop_obj.fset is None:
                    raise AttributeError("can't set attribute")
                prop_obj.fset(self, value)
            else:
                super(CleanSave, self).__setattr__(name, value)
        else:
            def _wrap_setattr():
                # Wrap `__setattr__` to track the changes.
                if self._state.adding:
                    # Adding a new model so no old values exist in the
                    # database, so all previous values are None.
                    super(CleanSave, self).__setattr__(name, value)
                    self.__marked_changed(name, None, value)
                else:
                    old = getattr(self, name, FieldUnset)
                    super(CleanSave, self).__setattr__(name, value)
                    new = getattr(self, name, FieldUnset)
                    self.__marked_changed(name, old, new)

            if not field.is_relation:
                # Simple field that just stores a value and is not related
                # to another model. Just track the difference between the
                # new and old value.
                _wrap_setattr()
            elif (field.one_to_one or
                    (field.many_to_one and field.related_model)):
                if name == field.attname:
                    # Field that stores the relation field ending in `_id`.
                    # This is updated just like a non-relational field.
                    _wrap_setattr()
                elif name == field.name:
                    # Field that holds the actual referenced objects. This
                    # needs to be handled with more care so that query is
                    # not performed trying to fetch the old value object.
                    if not isinstance(value, field.related_model):
                        # The actual type of object being saved to this
                        # field is different. This is probably bad and
                        # Django will handle the validation. But to be sure
                        # we update the changed field just like a normal
                        # field.
                        # 
                        # Note: This will cause a query to get the old
                        # value that was referenced from this object.
                        _wrap_setattr()
                    else:
                        # Do the tracking using the `_id` field instead of
                        # the actual objects. We don't store the old object
                        # in _changed_fields as that would required a query
                        # to get that object and that might not be needed.
                        # Instead we just track the attname value.
                        if self._state.adding:
                            old_id = None
                        else:
                            old_id = getattr(self, field.attname)
                        related_pk_field = (
                            field.related_model._meta.pk.name)
                        new_id = getattr(value, related_pk_field)
                        if new_id is None or old_id != new_id:
                            # Either the object has changed or its a new
                            # object. So we update
                            _wrap_setattr()
                else:
                    raise AttributeError(
                        'Unknown field(%s) for: %s' % (name, field))
            else:
                super(CleanSave, self).__setattr__(name, value)

    def save(self, *args, **kwargs):
        """Perform `full_clean` before save and only save changed fields."""
        # Validating relations will ensure that the objects exist in the
        # database, why? That is what relational database are for! Skip
        # validating relations postgresql will do that for us, because its an
        # actual database!
        related_fields = [
            field.name
            for field in self._meta.fields
            if field.is_relation
        ]
        self.full_clean(
            exclude=[self._meta.pk.name] + related_fields,
            validate_unique=False)
        #print('%s - [SAVE] changed %s' % (type(self).__name__, self._state._changed_fields))
        import pdb; pdb.set_trace()
        if self._state._changed_fields:
            if ('update_fields' not in kwargs and
                    not kwargs.get('force_insert', False) and
                    not kwargs.get('force_update', False) and
                    self.pk is not None and
                    self._meta.pk.attname not in self._state._changed_fields):
                kwargs['update_fields'] = [
                    key
                    for key, value in self._state._changed_fields.items()
                    if value is not FieldUnset
                ]
                #print('%s - [SAVE] update_fields %s' % (type(self).__name__, kwargs['update_fields']))
                obj = super(CleanSave, self).save(*args, **kwargs)
            else:
                obj = super(CleanSave, self).save(*args, **kwargs)
            self._state._changed_fields = {}
            return obj
        elif self.pk is None:
            return super(CleanSave, self).save(*args, **kwargs)
        else:
            # Nothing changed so nothing needs to be saved.
            return self
