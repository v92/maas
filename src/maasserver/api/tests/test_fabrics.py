# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for Fabric API."""

__all__ = []

import http.client
import random

from django.core.urlresolvers import reverse
from maasserver.models.fabric import Fabric
from maasserver.testing.api import APITestCase
from maasserver.testing.factory import factory
from maasserver.utils.converters import json_load_bytes
from maasserver.utils.orm import reload_object
from testtools.matchers import (
    ContainsDict,
    Equals,
)


def get_fabrics_uri():
    """Return a Fabric's URI on the API."""
    return reverse('fabrics_handler', args=[])


def get_fabric_uri(fabric):
    """Return a Fabric URI on the API."""
    return reverse(
        'fabric_handler', args=[fabric.id])


class TestFabricsAPI(APITestCase):

    def test_handler_path(self):
        self.assertEqual(
            '/api/2.0/fabrics/', get_fabrics_uri())

    def test_read(self):
        for _ in range(3):
            factory.make_Fabric()
        uri = get_fabrics_uri()
        response = self.client.get(uri)

        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        expected_ids = [
            fabric.id
            for fabric in Fabric.objects.all()
            ]
        result_ids = [
            fabric["id"]
            for fabric in json_load_bytes(response.content)
            ]
        self.assertItemsEqual(expected_ids, result_ids)

    def test_create(self):
        self.become_admin()
        fabric_name = factory.make_name("fabric")
        uri = get_fabrics_uri()
        response = self.client.post(uri, {
            "name": fabric_name,
        })
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertEqual(
            fabric_name, json_load_bytes(response.content)['name'])

    def test_create_admin_only(self):
        fabric_name = factory.make_name("fabric")
        uri = get_fabrics_uri()
        response = self.client.post(uri, {
            "name": fabric_name,
        })
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)


class TestFabricAPI(APITestCase):

    def test_handler_path(self):
        fabric = factory.make_Fabric()
        self.assertEqual(
            '/api/2.0/fabrics/%s/' % fabric.id,
            get_fabric_uri(fabric))

    def test_read(self):
        class_type = factory.make_name("class")
        fabric = factory.make_Fabric(class_type=class_type)
        for vid in range(1, 4):
            factory.make_VLAN(fabric=fabric, vid=vid).id
        uri = get_fabric_uri(fabric)
        response = self.client.get(uri)

        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_fabric = json_load_bytes(response.content)
        self.assertThat(parsed_fabric, ContainsDict({
            "id": Equals(fabric.id),
            "name": Equals(fabric.get_name()),
            "class_type": Equals(class_type),
            }))
        self.assertItemsEqual([
            vlan.id
            for vlan in fabric.vlan_set.all()
            ], [
            vlan["id"]
            for vlan in parsed_fabric["vlans"]
            ])

    def test_read_404_when_bad_id(self):
        uri = reverse(
            'fabric_handler', args=[random.randint(100, 1000)])
        response = self.client.get(uri)
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)

    def test_update(self):
        self.become_admin()
        fabric = factory.make_Fabric()
        new_name = factory.make_name("fabric")
        uri = get_fabric_uri(fabric)
        response = self.client.put(uri, {
            "name": new_name,
        })
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertEqual(new_name, json_load_bytes(response.content)['name'])
        self.assertEqual(new_name, reload_object(fabric).name)

    def test_update_admin_only(self):
        fabric = factory.make_Fabric()
        new_name = factory.make_name("fabric")
        uri = get_fabric_uri(fabric)
        response = self.client.put(uri, {
            "name": new_name,
        })
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)

    def test_delete_deletes_fabric(self):
        self.become_admin()
        fabric = factory.make_Fabric()
        uri = get_fabric_uri(fabric)
        response = self.client.delete(uri)
        self.assertEqual(
            http.client.NO_CONTENT, response.status_code, response.content)
        self.assertIsNone(reload_object(fabric))

    def test_delete_403_when_not_admin(self):
        fabric = factory.make_Fabric()
        uri = get_fabric_uri(fabric)
        response = self.client.delete(uri)
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)
        self.assertIsNotNone(reload_object(fabric))

    def test_delete_404_when_invalid_id(self):
        self.become_admin()
        uri = reverse(
            'fabric_handler', args=[random.randint(100, 1000)])
        response = self.client.delete(uri)
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)