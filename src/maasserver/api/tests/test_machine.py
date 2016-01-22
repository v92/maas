# Copyright 2013-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the Machine API."""

__all__ = []

from base64 import b64encode
import http.client
from io import StringIO
import sys

import bson
from django.conf import settings
from django.core.urlresolvers import reverse
from django.db import transaction
from maasserver import forms
from maasserver.api import machines as machines_module
from maasserver.enum import (
    INTERFACE_TYPE,
    IPADDRESS_TYPE,
    NODE_STATUS,
    NODE_STATUS_CHOICES,
    NODE_STATUS_CHOICES_DICT,
    NODE_TYPE,
    POWER_STATE,
)
from maasserver.fields import MAC_ERROR_MSG
from maasserver.models import (
    Config,
    Domain,
    interface as interface_module,
    Machine,
    Node,
    node as node_module,
    NodeGroup,
    StaticIPAddress,
)
from maasserver.models.node import RELEASABLE_STATUSES
from maasserver.storage_layouts import (
    MIN_BOOT_PARTITION_SIZE,
    StorageLayoutError,
)
from maasserver.testing.api import (
    APITestCase,
    APITransactionTestCase,
)
from maasserver.testing.architecture import make_usable_architecture
from maasserver.testing.factory import factory
from maasserver.testing.oauthclient import OAuthAuthenticatedClient
from maasserver.testing.orm import (
    reload_object,
    reload_objects,
)
from maasserver.testing.osystems import make_usable_osystem
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.converters import json_load_bytes
from maasserver.utils.orm import post_commit
from maastesting.matchers import (
    Equals,
    MockCalledOnceWith,
    MockNotCalled,
)
from metadataserver.models import (
    commissioningscript,
    NodeKey,
    NodeUserData,
)
from metadataserver.nodeinituser import get_node_init_user
from mock import ANY
from netaddr import IPAddress
from provisioningserver.rpc.exceptions import PowerActionAlreadyInProgress
from provisioningserver.utils.enum import map_enum
from testtools.matchers import (
    HasLength,
    Not,
)
import yaml


class MachineAnonAPITest(MAASServerTestCase):

    def setUp(self):
        super(MachineAnonAPITest, self).setUp()
        self.patch(node_module, 'power_on_node')
        self.patch(node_module, 'power_off_node')
        self.patch(node_module, 'power_driver_check')

    def test_anon_api_doc(self):
        # The documentation is accessible to anon users.
        self.patch(sys, "stderr", StringIO())
        response = self.client.get(reverse('api-doc'))
        self.assertEqual(http.client.OK, response.status_code)
        # No error or warning are emitted by docutils.
        self.assertEqual("", sys.stderr.getvalue())

    def test_machine_init_user_cannot_access(self):
        token = NodeKey.objects.get_token_for_node(factory.make_Node())
        client = OAuthAuthenticatedClient(get_node_init_user(), token)
        response = client.get(reverse('machines_handler'))
        self.assertEqual(http.client.FORBIDDEN, response.status_code)


class MachinesAPILoggedInTest(MAASServerTestCase):

    def setUp(self):
        super(MachinesAPILoggedInTest, self).setUp()
        self.patch(node_module, 'wait_for_power_command')

    def test_machines_GET_logged_in(self):
        # A (Django) logged-in user can access the API.
        self.client_log_in()
        machine = factory.make_Node()
        response = self.client.get(reverse('machines_handler'))
        parsed_result = json_load_bytes(response.content)

        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            [machine.system_id],
            [parsed_machine.get('system_id')
             for parsed_machine in parsed_result])


class TestMachineAPI(APITestCase):
    """Tests for /api/1.0/machines/<machine>/."""

    def setUp(self):
        super(TestMachineAPI, self).setUp()
        self.patch(node_module, 'power_on_node')
        self.patch(node_module, 'power_off_node')
        self.patch(node_module, 'power_driver_check')

    def test_handler_path(self):
        self.assertEqual(
            '/api/1.0/machines/machine-name/',
            reverse('machine_handler', args=['machine-name']))

    @staticmethod
    def get_machine_uri(machine):
        """Get the API URI for `machine`."""
        return reverse('machine_handler', args=[machine.system_id])

    def test_GET_returns_machine(self):
        # The api allows for fetching a single Machine (using system_id).
        machine = factory.make_Node()
        response = self.client.get(self.get_machine_uri(machine))

        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        NodeGroup.objects.ensure_master()
        domain_name = Domain.objects.get_default_domain().name
        self.assertEqual(
            "%s.%s" % (machine.hostname, domain_name),
            parsed_result['hostname'])
        self.assertEqual(machine.system_id, parsed_result['system_id'])

    def test_GET_returns_associated_tag(self):
        machine = factory.make_Node()
        tag = factory.make_Tag()
        machine.tags.add(tag)
        response = self.client.get(self.get_machine_uri(machine))

        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        self.assertEqual([tag.name], parsed_result['tag_names'])

    def test_GET_returns_associated_ip_addresses(self):
        machine = factory.make_Node(disable_ipv4=False)
        nic = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=machine)
        subnet = factory.make_Subnet()
        ip = factory.pick_ip_in_network(subnet.get_ipnetwork())
        lease = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, ip=ip,
            interface=nic, subnet=subnet)
        response = self.client.get(self.get_machine_uri(machine))

        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_result = json_load_bytes(response.content)
        self.assertEqual([lease.ip], parsed_result['ip_addresses'])

    def test_GET_returns_interface_set(self):
        machine = factory.make_Node()
        response = self.client.get(self.get_machine_uri(machine))
        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        self.assertIn('interface_set', parsed_result)

    def test_GET_returns_zone(self):
        machine = factory.make_Node()
        response = self.client.get(self.get_machine_uri(machine))
        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(
            [machine.zone.name, machine.zone.description],
            [
                parsed_result['zone']['name'],
                parsed_result['zone']['description']])

    def test_GET_returns_pxe_mac(self):
        machine = factory.make_Node(interface=True)
        machine.boot_interface = machine.interface_set.first()
        machine.save()
        response = self.client.get(self.get_machine_uri(machine))
        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        expected_result = {
            'mac_address': machine.boot_interface.mac_address.get_raw(),
        }
        self.assertEqual(
            expected_result, parsed_result['pxe_mac'])

    def test_GET_refuses_to_access_nonexistent_machine(self):
        # When fetching a Machine, the api returns a 'Not Found' (404) error
        # if no machine is found.
        url = reverse('machine_handler', args=['invalid-uuid'])

        response = self.client.get(url)

        self.assertEqual(http.client.NOT_FOUND, response.status_code)
        self.assertEqual(
            "Not Found", response.content.decode(settings.DEFAULT_CHARSET))

    def test_GET_returns_404_if_machineOA_name_contains_invld_characters(self):
        # When the requested name contains characters that are invalid for
        # a hostname, the result of the request is a 404 response.
        url = reverse('machine_handler', args=['invalid-uuid-#...'])

        response = self.client.get(url)

        self.assertEqual(http.client.NOT_FOUND, response.status_code)
        self.assertEqual(
            "Not Found", response.content.decode(settings.DEFAULT_CHARSET))

    def test_GET_returns_owner_name_when_allocated_to_self(self):
        machine = factory.make_Node(
            status=NODE_STATUS.ALLOCATED, owner=self.logged_in_user)
        response = self.client.get(self.get_machine_uri(machine))
        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(machine.owner.username, parsed_result["owner"])

    def test_GET_returns_owner_name_when_allocated_to_other_user(self):
        machine = factory.make_Node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_User())
        response = self.client.get(self.get_machine_uri(machine))
        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(machine.owner.username, parsed_result["owner"])

    def test_GET_returns_empty_owner_when_not_allocated(self):
        machine = factory.make_Node(status=NODE_STATUS.READY)
        response = self.client.get(self.get_machine_uri(machine))
        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(None, parsed_result["owner"])

    def test_GET_returns_physical_block_devices(self):
        machine = factory.make_Node(with_boot_disk=False)
        devices = [
            factory.make_PhysicalBlockDevice(node=machine)
            for _ in range(3)
        ]
        response = self.client.get(self.get_machine_uri(machine))
        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        parsed_devices = [
            device['name']
            for device in parsed_result['physicalblockdevice_set']
        ]
        self.assertItemsEqual(
            [device.name for device in devices], parsed_devices)

    def test_GET_rejects_device(self):
        device = factory.make_Device(owner=self.logged_in_user)
        response = self.client.get(self.get_machine_uri(device))
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)

    def test_GET_returns_min_hwe_kernel_and_hwe_kernel(self):
        machine = factory.make_Node()
        response = self.client.get(self.get_machine_uri(machine))

        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(None, parsed_result['min_hwe_kernel'])
        self.assertEqual(None, parsed_result['hwe_kernel'])

    def test_GET_returns_min_hwe_kernel(self):
        machine = factory.make_Node(min_hwe_kernel="hwe-v")
        response = self.client.get(self.get_machine_uri(machine))

        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)
        self.assertEqual("hwe-v", parsed_result['min_hwe_kernel'])

    def test_GET_returns_substatus_message_with_most_recent_event(self):
        """Makes sure the most recent event from this machine is shown in the
        substatus_message attribute."""
        # The first event won't be returned.
        event = factory.make_Event(description="Uninteresting event")
        machine = event.node
        # The second (and last) event will be returned.
        message = "Interesting event"
        factory.make_Event(description=message, node=machine)
        response = self.client.get(self.get_machine_uri(machine))
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(message, parsed_result['substatus_message'])

    def test_GET_returns_substatus_name(self):
        """GET should display the machine status as a user-friendly string."""
        for status in NODE_STATUS_CHOICES_DICT:
            machine = factory.make_Node(status=status)
            response = self.client.get(self.get_machine_uri(machine))
            parsed_result = json_load_bytes(response.content)
            self.assertEqual(NODE_STATUS_CHOICES_DICT[status],
                             parsed_result['substatus_name'])

    def test_POST_stop_checks_permission(self):
        machine = factory.make_Node()
        machine_stop = self.patch(machine, 'stop')
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'stop'})
        self.assertEqual(http.client.FORBIDDEN, response.status_code)
        self.assertThat(machine_stop, MockNotCalled())

    def test_POST_stop_rejects_device(self):
        device = factory.make_Device(owner=self.logged_in_user)
        response = self.client.post(
            self.get_machine_uri(device), {'op': 'stop'})
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)

    def test_POST_stop_returns_nothing_if_machine_was_not_stopped(self):
        # The machine may not be stopped because, for example, its power type
        # does not support it. In this case the machine is not returned to the
        # caller.
        machine = factory.make_Node(owner=self.logged_in_user)
        machine_stop = self.patch(node_module.Machine, 'stop')
        machine_stop.return_value = False
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'stop'})
        self.assertEqual(http.client.OK, response.status_code)
        self.assertIsNone(json_load_bytes(response.content))
        self.assertThat(machine_stop, MockCalledOnceWith(
            ANY, stop_mode=ANY, comment=None))

    def test_POST_stop_returns_machine(self):
        machine = factory.make_Node(owner=self.logged_in_user)
        self.patch(node_module.Machine, 'stop').return_value = True
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'stop'})
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            machine.system_id, json_load_bytes(response.content)['system_id'])

    def test_POST_stop_may_be_repeated(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake')
        self.patch(machine, 'stop')
        self.client.post(self.get_machine_uri(machine), {'op': 'stop'})
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'stop'})
        self.assertEqual(http.client.OK, response.status_code)

    def test_POST_stop_stops_machines(self):
        machine = factory.make_Node(owner=self.logged_in_user)
        machine_stop = self.patch(node_module.Machine, 'stop')
        stop_mode = factory.make_name('stop_mode')
        comment = factory.make_name('comment')
        self.client.post(
            self.get_machine_uri(machine),
            {'op': 'stop', 'stop_mode': stop_mode, 'comment': comment})
        self.assertThat(
            machine_stop,
            MockCalledOnceWith(
                self.logged_in_user, stop_mode=stop_mode, comment=comment))

    def test_POST_stop_handles_missing_comment(self):
        machine = factory.make_Node(owner=self.logged_in_user)
        machine_stop = self.patch(node_module.Node, 'stop')
        stop_mode = factory.make_name('stop_mode')
        self.client.post(
            self.get_machine_uri(machine),
            {'op': 'stop', 'stop_mode': stop_mode})
        self.assertThat(
            machine_stop,
            MockCalledOnceWith(
                self.logged_in_user, stop_mode=stop_mode, comment=None))

    def test_POST_stop_returns_503_when_power_op_already_in_progress(self):
        machine = factory.make_Node(owner=self.logged_in_user)
        exc_text = factory.make_name("exc_text")
        self.patch(
            node_module.Node,
            'stop').side_effect = PowerActionAlreadyInProgress(exc_text)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'stop'})
        self.assertResponseCode(http.client.SERVICE_UNAVAILABLE, response)
        self.assertIn(
            exc_text, response.content.decode(settings.DEFAULT_CHARSET))

    def test_POST_start_checks_permission(self):
        machine = factory.make_Node(owner=factory.make_User())
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'start'})
        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_POST_start_checks_ownership(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.READY)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'start'})
        self.assertEqual(http.client.CONFLICT, response.status_code)
        self.assertEqual(
            "Can't start machine: it hasn't been allocated.",
            response.content.decode(settings.DEFAULT_CHARSET))

    def test_POST_start_returns_machine(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        osystem = make_usable_osystem(self)
        distro_series = osystem['default_release']
        response = self.client.post(
            self.get_machine_uri(machine),
            {
                'op': 'start',
                'distro_series': distro_series,
            })
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            machine.system_id, json_load_bytes(response.content)['system_id'])

    def test_POST_start_rejects_device(self):
        device = factory.make_Device(owner=self.logged_in_user)
        response = self.client.post(
            self.get_machine_uri(device), {'op': 'start'})
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)

    def test_POST_start_sets_osystem_and_distro_series(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        osystem = make_usable_osystem(self)
        distro_series = osystem['default_release']
        response = self.client.post(
            self.get_machine_uri(machine), {
                'op': 'start',
                'distro_series': distro_series
            })
        self.assertEqual(
            (http.client.OK, machine.system_id),
            (response.status_code,
             json_load_bytes(response.content)['system_id']))
        self.assertEqual(
            osystem['name'], reload_object(machine).osystem)
        self.assertEqual(
            distro_series, reload_object(machine).distro_series)

    def test_POST_start_validates_distro_series(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        invalid_distro_series = factory.make_string()
        response = self.client.post(
            self.get_machine_uri(machine),
            {'op': 'start', 'distro_series': invalid_distro_series})
        self.assertEqual(
            (
                http.client.BAD_REQUEST,
                {'distro_series': [
                    "'%s' is not a valid distro_series.  "
                    "It should be one of: ''." %
                    invalid_distro_series]}
            ),
            (response.status_code, json_load_bytes(response.content)))

    def test_POST_start_sets_license_key(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        osystem = make_usable_osystem(self)
        distro_series = osystem['default_release']
        license_key = factory.make_string()
        self.patch(forms, 'validate_license_key_for').return_value = True
        response = self.client.post(
            self.get_machine_uri(machine), {
                'op': 'start',
                'osystem': osystem['name'],
                'distro_series': distro_series,
                'license_key': license_key,
            })
        self.assertEqual(
            (http.client.OK, machine.system_id),
            (response.status_code,
             json_load_bytes(response.content)['system_id']))
        self.assertEqual(
            license_key, reload_object(machine).license_key)

    def test_POST_start_validates_license_key(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        osystem = make_usable_osystem(self)
        distro_series = osystem['default_release']
        license_key = factory.make_string()
        self.patch(forms, 'validate_license_key_for').return_value = False
        response = self.client.post(
            self.get_machine_uri(machine), {
                'op': 'start',
                'osystem': osystem['name'],
                'distro_series': distro_series,
                'license_key': license_key,
            })
        self.assertEqual(
            (
                http.client.BAD_REQUEST,
                {'license_key': [
                    "Invalid license key."]}
            ),
            (response.status_code, json_load_bytes(response.content)))

    def test_POST_start_sets_default_distro_series(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        osystem = Config.objects.get_config('default_osystem')
        distro_series = Config.objects.get_config('default_distro_series')
        make_usable_osystem(
            self, osystem_name=osystem, releases=[distro_series])
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'start'})
        response_info = json_load_bytes(response.content)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(response_info['osystem'], osystem)
        self.assertEqual(response_info['distro_series'], distro_series)

    def test_POST_start_fails_with_no_boot_source(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'start'})
        self.assertEqual(
            (
                http.client.BAD_REQUEST,
                {'distro_series': [
                    "'%s' is not a valid distro_series.  "
                    "It should be one of: ''." %
                    Config.objects.get_config('default_distro_series')]}
            ),
            (response.status_code, json_load_bytes(response.content)))

    def test_POST_start_validates_hwe_kernel_with_default_distro_series(self):
        architecture = make_usable_architecture(self, subarch_name="generic")
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=architecture)
        osystem = Config.objects.get_config('default_osystem')
        distro_series = Config.objects.get_config('default_distro_series')
        make_usable_osystem(
            self, osystem_name=osystem, releases=[distro_series])
        bad_hwe_kernel = 'hwe-' + chr(ord(distro_series[0]) - 1)
        response = self.client.post(
            self.get_machine_uri(machine),
            {
                'op': 'start',
                'hwe_kernel': bad_hwe_kernel,
            })
        self.assertEqual(
            (
                http.client.BAD_REQUEST,
                {'hwe_kernel': [
                    "%s is not available for %s/%s on %s."
                    % (bad_hwe_kernel, osystem, distro_series, architecture)]}
            ),
            (response.status_code, json_load_bytes(response.content)))

    def test_POST_start_may_be_repeated(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        osystem = make_usable_osystem(self)
        distro_series = osystem['default_release']
        request = {
            'op': 'start',
            'distro_series': distro_series,
            }
        self.client.post(self.get_machine_uri(machine), request)
        response = self.client.post(self.get_machine_uri(machine), request)
        self.assertEqual(http.client.OK, response.status_code)

    def test_POST_start_stores_user_data(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        osystem = make_usable_osystem(self)
        distro_series = osystem['default_release']
        user_data = (
            b'\xff\x00\xff\xfe\xff\xff\xfe' +
            factory.make_string().encode('ascii'))
        response = self.client.post(
            self.get_machine_uri(machine), {
                'op': 'start',
                'user_data': b64encode(user_data).decode('ascii'),
                'distro_series': distro_series,
            })
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            user_data, NodeUserData.objects.get_user_data(machine))

    def test_POST_start_passes_comment(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        osystem = make_usable_osystem(self)
        distro_series = osystem['default_release']
        comment = factory.make_name('comment')
        machine_start = self.patch(node_module.Machine, 'start')
        machine_start.return_value = False
        self.client.post(
            self.get_machine_uri(machine), {
                'op': 'start',
                'user_data': None,
                'distro_series': distro_series,
                'comment': comment,
            })
        self.assertThat(machine_start, MockCalledOnceWith(
            self.logged_in_user, user_data=ANY, comment=comment))

    def test_POST_start_handles_missing_comment(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, interface=True,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        osystem = make_usable_osystem(self)
        distro_series = osystem['default_release']
        machine_start = self.patch(node_module.Machine, 'start')
        machine_start.return_value = False
        self.client.post(
            self.get_machine_uri(machine), {
                'op': 'start',
                'user_data': None,
                'distro_series': distro_series,
            })
        self.assertThat(machine_start, MockCalledOnceWith(
            self.logged_in_user, user_data=ANY, comment=None))

    def test_POST_release_releases_owned_machine(self):
        self.patch(node_module, 'power_off_node')
        self.patch(node_module.Node, 'start_transition_monitor')
        owned_statuses = [
            NODE_STATUS.RESERVED,
            NODE_STATUS.ALLOCATED,
        ]
        owned_machines = [
            factory.make_Node(
                owner=self.logged_in_user, status=status, power_type='ipmi',
                power_state=POWER_STATE.ON)
            for status in owned_statuses]
        responses = [
            self.client.post(self.get_machine_uri(machine), {'op': 'release'})
            for machine in owned_machines]
        self.assertEqual(
            [http.client.OK] * len(owned_machines),
            [response.status_code for response in responses])
        self.assertItemsEqual(
            [NODE_STATUS.RELEASING] * len(owned_machines),
            [machine.status
             for machine in reload_objects(Node, owned_machines)])

    def test_POST_release_releases_failed_machine(self):
        self.patch(node_module, 'power_off_node')
        self.patch(node_module.Machine, 'start_transition_monitor')
        owned_machine = factory.make_Node(
            owner=self.logged_in_user,
            status=NODE_STATUS.FAILED_DEPLOYMENT,
            power_type='ipmi', power_state=POWER_STATE.ON)
        response = self.client.post(
            self.get_machine_uri(owned_machine), {'op': 'release'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        owned_machine = Machine.objects.get(id=owned_machine.id)
        self.expectThat(owned_machine.status, Equals(NODE_STATUS.RELEASING))
        self.expectThat(owned_machine.owner, Equals(self.logged_in_user))

    def test_POST_release_does_nothing_for_unowned_machine(self):
        machine = factory.make_Node(
            status=NODE_STATUS.READY, owner=self.logged_in_user)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'release'})
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.READY, reload_object(machine).status)

    def test_POST_release_rejects_device(self):
        device = factory.make_Device(owner=self.logged_in_user)
        response = self.client.post(
            self.get_machine_uri(device), {'op': 'release'})
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)

    def test_POST_release_forbidden_if_user_cannot_edit_machine(self):
        machine = factory.make_Node(status=NODE_STATUS.READY)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'release'})
        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_POST_release_fails_for_other_machine_states(self):
        releasable_statuses = (
            RELEASABLE_STATUSES + [
                NODE_STATUS.RELEASING,
                NODE_STATUS.READY
            ])
        unreleasable_statuses = [
            status
            for status in map_enum(NODE_STATUS).values()
            if status not in releasable_statuses
        ]
        machines = [
            factory.make_Node(status=status, owner=self.logged_in_user)
            for status in unreleasable_statuses]
        responses = [
            self.client.post(self.get_machine_uri(machine), {'op': 'release'})
            for machine in machines]
        self.assertEqual(
            [http.client.CONFLICT] * len(unreleasable_statuses),
            [response.status_code for response in responses])
        self.assertItemsEqual(
            unreleasable_statuses,
            [machine.status for machine in reload_objects(Node, machines)])

    def test_POST_release_in_wrong_state_reports_current_state(self):
        machine = factory.make_Node(
            status=NODE_STATUS.RETIRED, owner=self.logged_in_user)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'release'})
        self.assertEqual(
            (
                http.client.CONFLICT,
                "Machine cannot be released in its current state ('Retired').",
            ),
            (response.status_code,
             response.content.decode(settings.DEFAULT_CHARSET)))

    def test_POST_release_rejects_request_from_unauthorized_user(self):
        machine = factory.make_Node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_User())
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'release'})
        self.assertEqual(http.client.FORBIDDEN, response.status_code)
        self.assertEqual(NODE_STATUS.ALLOCATED, reload_object(machine).status)

    def test_POST_release_allows_admin_to_release_anyones_machine(self):
        self.patch(node_module, 'power_off_node')
        self.patch(node_module.Machine, 'start_transition_monitor')
        machine = factory.make_Node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_User(),
            power_type='ipmi', power_state=POWER_STATE.ON)
        self.become_admin()
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'release'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertEqual(NODE_STATUS.RELEASING, reload_object(machine).status)

    def test_POST_release_combines_with_acquire(self):
        self.patch(node_module, 'power_off_node')
        self.patch(node_module.Machine, 'start_transition_monitor')
        machine = factory.make_Node(
            status=NODE_STATUS.READY, power_type='ipmi',
            power_state=POWER_STATE.ON, with_boot_disk=True)
        response = self.client.post(
            reverse('machines_handler'), {'op': 'acquire'})
        self.assertEqual(NODE_STATUS.ALLOCATED, reload_object(machine).status)
        machine_uri = json_load_bytes(response.content)['resource_uri']
        response = self.client.post(machine_uri, {'op': 'release'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertEqual(NODE_STATUS.RELEASING, reload_object(machine).status)

    def test_POST_acquire_passes_comment(self):
        factory.make_Node(
            status=NODE_STATUS.READY, power_type='ipmi',
            power_state=POWER_STATE.ON, with_boot_disk=True)
        machine_method = self.patch(node_module.Machine, 'acquire')
        comment = factory.make_name('comment')
        self.client.post(
            reverse('machines_handler'),
            {'op': 'acquire', 'comment': comment})
        self.assertThat(
            machine_method, MockCalledOnceWith(
                ANY, ANY, agent_name=ANY, comment=comment))

    def test_POST_acquire_handles_missing_comment(self):
        factory.make_Node(
            status=NODE_STATUS.READY, power_type='ipmi',
            power_state=POWER_STATE.ON, with_boot_disk=True)
        machine_method = self.patch(node_module.Machine, 'acquire')
        self.client.post(
            reverse('machines_handler'), {'op': 'acquire'})
        self.assertThat(
            machine_method, MockCalledOnceWith(
                ANY, ANY, agent_name=ANY, comment=None))

    def test_POST_release_frees_hwe_kernel(self):
        self.patch(node_module, 'power_off_node')
        self.patch(node_module.Machine, 'start_transition_monitor')
        machine = factory.make_Node(
            owner=self.logged_in_user, status=NODE_STATUS.ALLOCATED,
            power_type='ipmi', power_state=POWER_STATE.ON,
            hwe_kernel='hwe-v')
        self.assertEqual('hwe-v', reload_object(machine).hwe_kernel)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'release'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertEqual(NODE_STATUS.RELEASING, reload_object(machine).status)
        self.assertEqual(None, reload_object(machine).hwe_kernel)

    def test_POST_release_passes_comment(self):
        machine = factory.make_Node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_User(),
            power_type='ipmi', power_state=POWER_STATE.OFF)
        self.become_admin()
        comment = factory.make_name('comment')
        machine_release = self.patch(node_module.Machine, 'release_or_erase')
        self.client.post(
            self.get_machine_uri(machine),
            {'op': 'release', 'comment': comment})
        self.assertThat(
            machine_release,
            MockCalledOnceWith(self.logged_in_user, comment))

    def test_POST_release_handles_missing_comment(self):
        machine = factory.make_Node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_User(),
            power_type='ipmi', power_state=POWER_STATE.OFF)
        self.become_admin()
        machine_release = self.patch(node_module.Machine, 'release_or_erase')
        self.client.post(
            self.get_machine_uri(machine), {'op': 'release'})
        self.assertThat(
            machine_release,
            MockCalledOnceWith(self.logged_in_user, None))

    def test_POST_commission_commissions_machine(self):
        machine = factory.make_Node(
            status=NODE_STATUS.READY, owner=factory.make_User(),
            power_state=POWER_STATE.OFF)
        self.become_admin()
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'commission'})
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.COMMISSIONING, reload_object(machine).status)

    def test_POST_commission_commissions_machine_with_options(self):
        machine = factory.make_Node(
            status=NODE_STATUS.READY, owner=factory.make_User(),
            power_state=POWER_STATE.OFF)
        self.become_admin()
        response = self.client.post(self.get_machine_uri(machine), {
            'op': 'commission',
            'enable_ssh': "true",
            'skip_networking': 1,
            })
        self.assertEqual(http.client.OK, response.status_code)
        machine = reload_object(machine)
        self.assertTrue(machine.enable_ssh)
        self.assertTrue(machine.skip_networking)

    def test_PUT_updates_machine(self):
        self.become_admin()
        # The api allows the updating of a Machine.
        machine = factory.make_Node(
            hostname='diane', owner=self.logged_in_user,
            architecture=make_usable_architecture(self))
        response = self.client.put(
            self.get_machine_uri(machine), {'hostname': 'francis'})
        parsed_result = json_load_bytes(response.content)

        self.assertEqual(http.client.OK, response.status_code)
        NodeGroup.objects.ensure_master()
        domain_name = Domain.objects.get_default_domain().name
        self.assertEqual(
            'francis.%s' % domain_name, parsed_result['hostname'])
        self.assertEqual(0, Machine.objects.filter(hostname='diane').count())
        self.assertEqual(1, Machine.objects.filter(hostname='francis').count())

    def test_PUT_omitted_hostname(self):
        self.become_admin()
        hostname = factory.make_name('hostname')
        arch = make_usable_architecture(self)
        machine = factory.make_Node(
            hostname=hostname, owner=self.logged_in_user, architecture=arch)
        response = self.client.put(
            self.get_machine_uri(machine),
            {'architecture': arch})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertTrue(Machine.objects.filter(hostname=hostname).exists())

    def test_PUT_rejects_device(self):
        self.become_admin()
        machine = factory.make_Device(owner=self.logged_in_user)
        response = self.client.put(self.get_machine_uri(machine))
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)

    def test_PUT_ignores_unknown_fields(self):
        self.become_admin()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            architecture=make_usable_architecture(self))
        field = factory.make_string()
        response = self.client.put(
            self.get_machine_uri(machine),
            {field: factory.make_string()}
        )

        self.assertEqual(http.client.OK, response.status_code)

    def test_PUT_admin_can_change_power_type(self):
        self.become_admin()
        original_power_type = factory.pick_power_type()
        new_power_type = factory.pick_power_type(but_not=original_power_type)
        machine = factory.make_Node(
            owner=self.logged_in_user,
            power_type=original_power_type,
            architecture=make_usable_architecture(self))
        response = self.client.put(
            self.get_machine_uri(machine), {'power_type': new_power_type})

        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            new_power_type, reload_object(machine).power_type)

    def test_PUT_non_admin_cannot_change_power_type(self):
        original_power_type = factory.pick_power_type()
        new_power_type = factory.pick_power_type(but_not=original_power_type)
        machine = factory.make_Node(
            owner=self.logged_in_user, power_type=original_power_type)
        response = self.client.put(
            self.get_machine_uri(machine), {'power_type': new_power_type})

        self.assertEqual(http.client.FORBIDDEN, response.status_code)
        self.assertEqual(
            original_power_type, reload_object(machine).power_type)

    def test_resource_uri_points_back_at_machine(self):
        self.become_admin()
        # When a Machine is returned by the API, the field 'resource_uri'
        # provides the URI for this Machine.
        machine = factory.make_Node(
            hostname='diane', owner=self.logged_in_user,
            architecture=make_usable_architecture(self))
        response = self.client.put(
            self.get_machine_uri(machine), {'hostname': 'francis'})
        parsed_result = json_load_bytes(response.content)

        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            reverse('machine_handler', args=[parsed_result['system_id']]),
            parsed_result['resource_uri'])

    def test_PUT_rejects_invalid_data(self):
        # If the data provided to update a machine is invalid, a 'Bad request'
        # response is returned.
        self.become_admin()
        machine = factory.make_Node(
            hostname='diane', owner=self.logged_in_user,
            architecture=make_usable_architecture(self))
        response = self.client.put(
            self.get_machine_uri(machine), {'hostname': '.'})
        parsed_result = json_load_bytes(response.content)

        self.assertEqual(http.client.BAD_REQUEST, response.status_code)
        self.assertEqual(
            {'hostname':
                ["DNS name contains an empty label.", "Nonexistant domain."]},
            parsed_result)

    def test_PUT_refuses_to_update_nonexistent_machine(self):
        # When updating a Machine, the api returns a 'Not Found' (404) error
        # if no machine is found.
        self.become_admin()
        url = reverse('machine_handler', args=['invalid-uuid'])
        response = self.client.put(url)

        self.assertEqual(http.client.NOT_FOUND, response.status_code)

    def test_PUT_updates_power_parameters_field(self):
        # The api allows the updating of a Machine's power_parameters field.
        self.become_admin()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        # Create a power_parameter valid for the selected power_type.
        new_power_address = factory.make_mac_address()
        response = self.client.put(
            self.get_machine_uri(machine),
            {'power_parameters_mac_address': new_power_address})

        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            {'mac_address': new_power_address},
            reload_object(machine).power_parameters)

    def test_PUT_updates_cpu_memory(self):
        self.become_admin()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            power_type=factory.pick_power_type(),
            architecture=make_usable_architecture(self))
        response = self.client.put(
            self.get_machine_uri(machine),
            {'cpu_count': 1, 'memory': 1024})
        self.assertEqual(http.client.OK, response.status_code)
        machine = reload_object(machine)
        self.assertEqual(1, machine.cpu_count)
        self.assertEqual(1024, machine.memory)

    def test_PUT_updates_power_parameters_accepts_only_mac_for_wol(self):
        self.become_admin()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            power_type='ether_wake',
            architecture=make_usable_architecture(self))
        # Create an invalid power_parameter for WoL (not a valid
        # MAC address).
        new_power_address = factory.make_string()
        response = self.client.put(
            self.get_machine_uri(machine),
            {'power_parameters_mac_address': new_power_address})
        error_msg = MAC_ERROR_MSG % {'value': new_power_address}
        self.assertEqual(
            (
                http.client.BAD_REQUEST,
                {'power_parameters': ["MAC Address: %s" % error_msg]},
            ),
            (response.status_code, json_load_bytes(response.content)))

    def test_PUT_updates_power_parameters_rejects_unknown_param(self):
        self.become_admin()
        power_parameters = {factory.make_string(): factory.make_string()}
        machine = factory.make_Node(
            owner=self.logged_in_user,
            power_type='ether_wake',
            power_parameters=power_parameters,
            architecture=make_usable_architecture(self))
        response = self.client.put(
            self.get_machine_uri(machine),
            {'power_parameters_unknown_param': factory.make_string()})

        self.assertEqual(
            (
                http.client.BAD_REQUEST,
                {'power_parameters': ["Unknown parameter(s): unknown_param."]}
            ),
            (response.status_code, json_load_bytes(response.content)))
        self.assertEqual(
            power_parameters, reload_object(machine).power_parameters)

    def test_PUT_updates_power_type_default_resets_params(self):
        # If one sets power_type to empty, power_parameter gets
        # reset by default (if skip_check is not set).
        self.become_admin()
        power_parameters = {factory.make_string(): factory.make_string()}
        machine = factory.make_Node(
            owner=self.logged_in_user,
            power_type='ether_wake',
            power_parameters=power_parameters,
            architecture=make_usable_architecture(self))
        response = self.client.put(
            self.get_machine_uri(machine),
            {'power_type': ''})

        machine = reload_object(machine)
        self.assertEqual(
            (http.client.OK, machine.power_type, machine.power_parameters),
            (response.status_code, '', {}))

    def test_PUT_updates_power_type_empty_rejects_params(self):
        # If one sets power_type to empty, one cannot set power_parameters.
        self.become_admin()
        power_parameters = {factory.make_string(): factory.make_string()}
        machine = factory.make_Node(
            owner=self.logged_in_user,
            power_type='ether_wake',
            power_parameters=power_parameters,
            architecture=make_usable_architecture(self))
        new_param = factory.make_string()
        response = self.client.put(
            self.get_machine_uri(machine),
            {
                'power_type': '',
                'power_parameters_address': new_param,
            })

        machine = reload_object(machine)
        self.assertEqual(
            (
                http.client.BAD_REQUEST,
                {'power_parameters': ["Unknown parameter(s): address."]}
            ),
            (response.status_code, json_load_bytes(response.content)))
        self.assertEqual(
            power_parameters, reload_object(machine).power_parameters)

    def test_PUT_updates_power_type_empty_skip_check_to_force_params(self):
        # If one sets power_type to empty, it is possible to pass
        # power_parameter_skip_check='true' to force power_parameters.
        # XXX bigjools 2014-01-21 Why is this necessary?
        self.become_admin()
        power_parameters = {factory.make_string(): factory.make_string()}
        machine = factory.make_Node(
            owner=self.logged_in_user,
            power_type='ether_wake',
            power_parameters=power_parameters,
            architecture=make_usable_architecture(self))
        new_param = factory.make_string()
        response = self.client.put(
            self.get_machine_uri(machine),
            {
                'power_type': '',
                'power_parameters_param': new_param,
                'power_parameters_skip_check': 'true',
            })

        machine = reload_object(machine)
        self.assertEqual(
            (http.client.OK, machine.power_type, machine.power_parameters),
            (response.status_code, '', {'param': new_param}))

    def test_PUT_updates_power_parameters_skip_ckeck(self):
        # With power_parameters_skip_check, arbitrary data
        # can be put in a Machine's power_parameter field.
        self.become_admin()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            architecture=make_usable_architecture(self))
        new_param = factory.make_string()
        new_value = factory.make_string()
        response = self.client.put(
            self.get_machine_uri(machine),
            {
                'power_parameters_%s' % new_param: new_value,
                'power_parameters_skip_check': 'true',
            })

        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            {new_param: new_value}, reload_object(machine).power_parameters)

    def test_PUT_updates_power_parameters_empty_string(self):
        self.become_admin()
        power_parameters = {factory.make_string(): factory.make_string()}
        machine = factory.make_Node(
            owner=self.logged_in_user,
            power_type='ether_wake',
            power_parameters=power_parameters,
            architecture=make_usable_architecture(self))
        response = self.client.put(
            self.get_machine_uri(machine),
            {'power_parameters_mac_address': ''})

        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            {'mac_address': ''},
            reload_object(machine).power_parameters)

    def test_PUT_sets_zone(self):
        self.become_admin()
        new_zone = factory.make_Zone()
        machine = factory.make_Node(
            architecture=make_usable_architecture(self))

        response = self.client.put(
            self.get_machine_uri(machine), {'zone': new_zone.name})

        self.assertEqual(http.client.OK, response.status_code)
        machine = reload_object(machine)
        self.assertEqual(new_zone, machine.zone)

    def test_PUT_does_not_set_zone_if_not_present(self):
        self.become_admin()
        new_name = factory.make_name()
        machine = factory.make_Node(
            architecture=make_usable_architecture(self))
        old_zone = machine.zone

        response = self.client.put(
            self.get_machine_uri(machine), {'hostname': new_name})

        self.assertEqual(http.client.OK, response.status_code)
        machine = reload_object(machine)
        self.assertEqual(
            (old_zone, new_name), (machine.zone, machine.hostname))

    def test_PUT_clears_zone(self):
        self.skip(
            "XXX: JeroenVermeulen 2013-12-11 bug=1259872: Clearing the "
            "zone field does not work...")

        self.become_admin()
        machine = factory.make_Node(zone=factory.make_Zone())

        response = self.client.put(self.get_machine_uri(machine), {'zone': ''})

        self.assertEqual(http.client.OK, response.status_code)
        machine = reload_object(machine)
        self.assertEqual(None, machine.zone)

    def test_PUT_without_zone_leaves_zone_unchanged(self):
        self.become_admin()
        zone = factory.make_Zone()
        machine = factory.make_Node(
            zone=zone, architecture=make_usable_architecture(self))

        response = self.client.put(self.get_machine_uri(machine), {})

        self.assertEqual(http.client.OK, response.status_code)
        machine = reload_object(machine)
        self.assertEqual(zone, machine.zone)

    def test_PUT_requires_admin(self):
        machine = factory.make_Node(
            owner=self.logged_in_user,
            architecture=make_usable_architecture(self))
        # PUT the machine with no arguments - should get FORBIDDEN
        response = self.client.put(self.get_machine_uri(machine), {})
        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_PUT_zone_change_requires_admin(self):
        new_zone = factory.make_Zone()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            architecture=make_usable_architecture(self))
        old_zone = machine.zone

        response = self.client.put(
            self.get_machine_uri(machine),
            {'zone': new_zone.name})

        self.assertEqual(http.client.FORBIDDEN, response.status_code)
        # Confirm the machine's physical zone has not been updated.
        machine = reload_object(machine)
        self.assertEqual(old_zone, machine.zone)

    def test_PUT_sets_disable_ipv4(self):
        self.become_admin()
        original_setting = factory.pick_bool()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            architecture=make_usable_architecture(self),
            disable_ipv4=original_setting)
        new_setting = not original_setting

        response = self.client.put(
            self.get_machine_uri(machine), {'disable_ipv4': new_setting})
        self.assertEqual(http.client.OK, response.status_code)

        machine = reload_object(machine)
        self.assertEqual(new_setting, machine.disable_ipv4)

    def test_PUT_leaves_disable_ipv4_unchanged_by_default(self):
        self.become_admin()
        original_setting = factory.pick_bool()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            architecture=make_usable_architecture(self),
            disable_ipv4=original_setting)
        self.assertEqual(original_setting, machine.disable_ipv4)

        response = self.client.put(
            self.get_machine_uri(machine), {'zone': factory.make_Zone()})
        self.assertEqual(http.client.OK, response.status_code)

        machine = reload_object(machine)
        self.assertEqual(original_setting, machine.disable_ipv4)

    def test_PUT_updates_swap_size(self):
        self.become_admin()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            architecture=make_usable_architecture(self))
        response = self.client.put(
            reverse('machine_handler', args=[machine.system_id]),
            {'swap_size': 5 * 1000 ** 3})  # Making sure we overflow 32 bits
        parsed_result = json_load_bytes(response.content)
        machine = reload_object(machine)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(machine.swap_size, parsed_result['swap_size'])

    def test_PUT_updates_swap_size_suffixes(self):
        self.become_admin()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            architecture=make_usable_architecture(self))

        response = self.client.put(
            reverse('machine_handler', args=[machine.system_id]),
            {'swap_size': '5K'})
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(5000, parsed_result['swap_size'])

        response = self.client.put(
            reverse('machine_handler', args=[machine.system_id]),
            {'swap_size': '5M'})
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(5000000, parsed_result['swap_size'])

        response = self.client.put(
            reverse('machine_handler', args=[machine.system_id]),
            {'swap_size': '5G'})
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(5000000000, parsed_result['swap_size'])

        response = self.client.put(
            reverse('machine_handler', args=[machine.system_id]),
            {'swap_size': '5T'})
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(5000000000000, parsed_result['swap_size'])

    def test_PUT_updates_swap_size_invalid_suffix(self):
        self.become_admin()
        machine = factory.make_Node(
            owner=self.logged_in_user,
            architecture=make_usable_architecture(self))
        response = self.client.put(
            reverse('machine_handler', args=[machine.system_id]),
            {'swap_size': '5E'})  # We won't support exabytes yet
        parsed_result = json_load_bytes(response.content)
        self.assertEqual(http.client.BAD_REQUEST, response.status_code)
        self.assertEqual('Invalid size for swap: 5E',
                         parsed_result['swap_size'][0])

    def test_DELETE_deletes_machine(self):
        # The api allows to delete a Machine.
        self.become_admin()
        machine = factory.make_Node(owner=self.logged_in_user)
        system_id = machine.system_id
        response = self.client.delete(self.get_machine_uri(machine))

        self.assertEqual(204, response.status_code)
        self.assertItemsEqual([], Machine.objects.filter(system_id=system_id))

    def test_DELETE_rejects_device(self):
        device = factory.make_Device(owner=self.logged_in_user)
        response = self.client.delete(self.get_machine_uri(device))
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)

    def test_DELETE_deletes_machine_fails_if_not_admin(self):
        # Only superusers can delete machines.
        machine = factory.make_Node(owner=self.logged_in_user)
        response = self.client.delete(self.get_machine_uri(machine))

        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_DELETE_forbidden_without_edit_permission(self):
        # A user without the edit permission cannot delete a Machine.
        machine = factory.make_Node()
        response = self.client.delete(self.get_machine_uri(machine))

        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_DELETE_refuses_to_delete_invisible_machine(self):
        # The request to delete a single machine is denied if the machine isn't
        # visible by the user.
        other_machine = factory.make_Node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_User())

        response = self.client.delete(self.get_machine_uri(other_machine))

        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_DELETE_refuses_to_delete_nonexistent_machine(self):
        # When deleting a Machine, the api returns a 'Not Found' (404) error
        # if no machine is found.
        url = reverse('machine_handler', args=['invalid-uuid'])
        response = self.client.delete(url)

        self.assertEqual(http.client.NOT_FOUND, response.status_code)


class TestClaimStickyIpAddressAPI(APITestCase):
    """Tests for /api/1.0/machines/<machine>/?op=claim_sticky_ip_address"""

    def get_machine_uri(self, machine):
        """Get the API URI for `machine`."""
        return reverse('machine_handler', args=[machine.system_id])

    def test_claim_sticky_ip_address_disallows_when_allocated(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.ALLOCATED)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'claim_sticky_ip_address'})
        self.assertEqual(
            http.client.CONFLICT, response.status_code, response.content)
        self.assertEqual(
            "Sticky IP cannot be assigned to a node that is allocated",
            response.content.decode(settings.DEFAULT_CHARSET))

    def test_claim_sticky_ip_address_validates_ip_address(self):
        self.become_admin()
        machine = factory.make_Node()
        response = self.client.post(
            self.get_machine_uri(machine),
            {'op': 'claim_sticky_ip_address',
             'requested_address': '192.168.1000.1'})
        self.assertEqual(http.client.BAD_REQUEST, response.status_code)
        self.assertEqual(
            dict(requested_address=["Enter a valid IPv4 or IPv6 address."]),
            json_load_bytes(response.content))

    def test_claim_sticky_ip_address_returns_existing_if_already_exists(self):
        self.become_admin()
        machine = factory.make_Node_with_Interface_on_Subnet(
            disable_ipv4=False)
        # Silence 'update_host_maps'.
        self.patch_autospec(interface_module, "update_host_maps")
        [existing_ip] = machine.get_boot_interface().claim_static_ips()
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'claim_sticky_ip_address'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_machine = json_load_bytes(response.content)
        [returned_ip] = parsed_machine["ip_addresses"]
        self.assertEqual(
            (existing_ip.ip, IPADDRESS_TYPE.STICKY),
            (returned_ip, existing_ip.alloc_type)
        )

    def test_claim_sticky_ip_address_claims_sticky_ip_address_non_admin(self):
        machine = factory.make_Node_with_Interface_on_Subnet(
            owner=self.logged_in_user, disable_ipv4=False)
        # Silence 'update_host_maps'.
        self.patch(interface_module, "update_host_maps")
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'claim_sticky_ip_address'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_machine = json_load_bytes(response.content)
        [returned_ip] = parsed_machine["ip_addresses"]
        [given_ip] = StaticIPAddress.objects.filter(
            alloc_type=IPADDRESS_TYPE.STICKY, ip__isnull=False)
        self.assertEqual(
            (given_ip.ip, IPADDRESS_TYPE.STICKY),
            (returned_ip, given_ip.alloc_type),
        )

    def test_claim_sticky_ip_address_checks_edit_permission(self):
        other_user = factory.make_User()
        machine = factory.make_Node_with_Interface_on_Subnet(
            owner=other_user, disable_ipv4=False)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'claim_sticky_ip_address'})
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)

    def test_claim_sticky_ip_address_claims_sticky_ip_address(self):
        self.become_admin()
        machine = factory.make_Node_with_Interface_on_Subnet(
            disable_ipv4=False)
        # Silence 'update_host_maps'.
        self.patch(interface_module, "update_host_maps")
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'claim_sticky_ip_address'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_machine = json_load_bytes(response.content)
        [returned_ip] = parsed_machine["ip_addresses"]
        [given_ip] = StaticIPAddress.objects.filter(
            alloc_type=IPADDRESS_TYPE.STICKY, ip__isnull=False)
        self.assertEqual(
            (given_ip.ip, IPADDRESS_TYPE.STICKY),
            (returned_ip, given_ip.alloc_type),
        )

    def test_claim_sticky_ip_address_allows_macaddress_parameter(self):
        self.become_admin()
        machine = factory.make_Node_with_Interface_on_Subnet(
            disable_ipv4=False)
        boot_interface = machine.get_boot_interface()
        subnet = boot_interface.ip_addresses.first().subnet
        second_nic = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=machine)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, ip="",
            interface=second_nic, subnet=subnet)
        # Silence 'update_host_maps'.
        self.patch(interface_module, "update_host_maps")
        response = self.client.post(
            self.get_machine_uri(machine),
            {
                'op': 'claim_sticky_ip_address',
                'mac_address': second_nic.mac_address.get_raw(),
            })
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        [observed_static_ip] = second_nic.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.STICKY)
        self.assertEqual(IPADDRESS_TYPE.STICKY, observed_static_ip.alloc_type)

    def test_claim_sticky_ip_address_catches_bad_mac_address_parameter(self):
        self.become_admin()
        machine = factory.make_Node_with_Interface_on_Subnet(
            disable_ipv4=False)
        random_mac = factory.make_mac_address()

        response = self.client.post(
            self.get_machine_uri(machine),
            {
                'op': 'claim_sticky_ip_address',
                'mac_address': random_mac,
            })
        self.assertEqual(
            http.client.BAD_REQUEST, response.status_code, response.content)
        self.assertEqual(
            "mac_address %s not found on the node" % random_mac,
            response.content.decode(settings.DEFAULT_CHARSET))

    def test_claim_sticky_ip_allows_requested_ip(self):
        self.become_admin()
        machine = factory.make_Node_with_Interface_on_Subnet(
            disable_ipv4=False)
        boot_interface = machine.get_boot_interface()
        subnet = boot_interface.ip_addresses.first().subnet
        ngi = subnet.nodegroupinterface_set.first()
        requested_address = ngi.static_ip_range_low

        # Silence 'update_host_maps'.
        self.patch(interface_module, "update_host_maps")
        response = self.client.post(
            self.get_machine_uri(machine),
            {
                'op': 'claim_sticky_ip_address',
                'requested_address': requested_address,
            })
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertIsNotNone(
            StaticIPAddress.objects.filter(
                alloc_type=IPADDRESS_TYPE.STICKY, ip=requested_address,
                subnet=subnet).first())

    def test_claim_sticky_ip_address_detects_out_of_network_requested_ip(self):
        self.become_admin()
        machine = factory.make_Node_with_Interface_on_Subnet(
            disable_ipv4=False)
        boot_interface = machine.get_boot_interface()
        subnet = boot_interface.ip_addresses.first().subnet
        ngi = subnet.nodegroupinterface_set.first()
        other_network = factory.make_ipv4_network(but_not=ngi.network)
        requested_address = factory.pick_ip_in_network(other_network)

        response = self.client.post(
            self.get_machine_uri(machine),
            {
                'op': 'claim_sticky_ip_address',
                'requested_address': requested_address.format(),
            })
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)

    def test_claim_sticky_ip_address_detects_unavailable_requested_ip(self):
        self.become_admin()
        # Create 2 machines on the same nodegroup and interface.
        machine = factory.make_Node_with_Interface_on_Subnet(
            disable_ipv4=False)
        boot_interface = machine.get_boot_interface()
        subnet = boot_interface.ip_addresses.first().subnet
        ngi = subnet.nodegroupinterface_set.first()
        other_machine = factory.make_Node(
            interface=True, nodegroup=ngi.nodegroup, disable_ipv4=False)
        other_mac = other_machine.get_boot_interface()
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, ip="",
            interface=other_mac, subnet=subnet)

        # Allocate an IP to one of the machines.
        self.patch_autospec(interface_module, "update_host_maps")
        requested_address = IPAddress(ngi.static_ip_range_low) + 1
        requested_address = requested_address.format()
        other_machine.get_boot_interface().claim_static_ips(
            requested_address=requested_address)

        # Use the API to try to duplicate the same IP on the other machine.
        response = self.client.post(
            self.get_machine_uri(machine),
            {
                'op': 'claim_sticky_ip_address',
                'requested_address': requested_address,
            })
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)


class TestMachineAPITransactional(APITransactionTestCase):
    '''The following TestMachineAPI tests require APITransactionTestCase,
        and thus, have been separated from the TestMachineAPI above.
    '''

    def test_POST_start_returns_error_when_static_ips_exhausted(self):
        self.patch(node_module, 'power_driver_check')
        machine = factory.make_Node_with_Interface_on_Subnet(
            owner=self.logged_in_user, status=NODE_STATUS.ALLOCATED,
            architecture=make_usable_architecture(self))
        boot_interface = machine.get_boot_interface()
        subnet = boot_interface.ip_addresses.first().subnet
        ngi = subnet.nodegroupinterface_set.first()

        # Narrow the available IP range and pre-claim the only address.
        ngi.static_ip_range_high = ngi.static_ip_range_low
        ngi.save()
        with transaction.atomic():
            StaticIPAddress.objects.allocate_new(
                ngi.network, ngi.static_ip_range_low, ngi.static_ip_range_high,
                ngi.ip_range_low, ngi.ip_range_high)

        osystem = make_usable_osystem(self)
        distro_series = osystem['default_release']
        response = self.client.post(
            TestMachineAPI.get_machine_uri(machine),
            {
                'op': 'start',
                'distro_series': distro_series,
            })
        self.assertEqual(http.client.SERVICE_UNAVAILABLE, response.status_code)


class TestMachineReleaseStickyIpAddressAPI(APITestCase):
    """Tests for /api/1.0/machines/?op=release_sticky_ip_address."""

    @staticmethod
    def get_machine_uri(machine):
        """Get the API URI for `machine`."""
        return reverse('machine_handler', args=[machine.system_id])

    def test__releases_ip_address(self):
        self.become_admin()
        machine = factory.make_Node_with_Interface_on_Subnet(
            disable_ipv4=False)
        # Silence 'update_host_maps' and 'remove_host_maps'
        self.patch(interface_module, "update_host_maps")
        self.patch(interface_module, "remove_host_maps")
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'claim_sticky_ip_address'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_machine = json_load_bytes(response.content)
        self.expectThat(parsed_machine["ip_addresses"], Not(HasLength(0)))

        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'release_sticky_ip_address'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_machine = json_load_bytes(response.content)
        self.expectThat(parsed_machine["ip_addresses"], HasLength(0))

    def test__validates_ip_address(self):
        self.become_admin()
        machine = factory.make_Node_with_Interface_on_Subnet(
            disable_ipv4=False)
        # Silence 'update_host_maps' and 'remove_host_maps'
        response = self.client.post(
            self.get_machine_uri(machine),
            {'op': 'release_sticky_ip_address',
             'address': '192.168.1000.1'})
        self.assertEqual(http.client.BAD_REQUEST, response.status_code)
        self.assertEqual(
            dict(address=["Enter a valid IPv4 or IPv6 address."]),
            json_load_bytes(response.content))


class TestMachineReleaseStickyIpAddressAPITransactional(
        APITransactionTestCase):
    """The following TestMachineReleaseStickyIpAddressAPI tests require
        APITransactionTestCase, and thus, have been separated
        from the TestMachineReleaseStickyIpAddressAPI above.
    """

    def test__releases_all_ip_addresses(self):
        network = factory._make_random_network(slash=24)
        subnet = factory.make_Subnet(cidr=str(network.cidr))
        machine = factory.make_Node_with_Interface_on_Subnet(
            status=NODE_STATUS.ALLOCATED, node_type=NODE_TYPE.MACHINE,
            subnet=subnet, disable_ipv4=False, owner=self.logged_in_user)
        boot_interface = machine.get_boot_interface()
        # Silence 'update_host_maps' and 'remove_host_maps'
        self.patch(interface_module, "update_host_maps")
        self.patch(interface_module, "remove_host_maps")
        for interface in machine.interface_set.all():
            with transaction.atomic():
                allocated = boot_interface.claim_static_ips()
            self.expectThat(allocated, HasLength(1))
        response = self.client.post(
            TestMachineReleaseStickyIpAddressAPI.get_machine_uri(machine),
            {'op': 'release_sticky_ip_address'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_machine = json_load_bytes(response.content)
        self.expectThat(parsed_machine["ip_addresses"], HasLength(0))

    def test__releases_specific_address(self):
        network = factory._make_random_network(slash=24)
        subnet = factory.make_Subnet(cidr=str(network.cidr))
        machine = factory.make_Node_with_Interface_on_Subnet(
            status=NODE_STATUS.ALLOCATED, node_type=NODE_TYPE.MACHINE,
            subnet=subnet, disable_ipv4=False, owner=self.logged_in_user)
        boot_interface = machine.get_boot_interface()
        # Silence 'update_host_maps' and 'remove_host_maps'
        self.patch(interface_module, "update_host_maps")
        self.patch(interface_module, "remove_host_maps")
        ips = []
        for interface in machine.interface_set.all():
            with transaction.atomic():
                allocated = boot_interface.claim_static_ips()
            self.expectThat(allocated, HasLength(1))
            # Note: 'allocated' is a list of (ip,mac) tuples
            ips.append(allocated[0])
        response = self.client.post(
            TestMachineReleaseStickyIpAddressAPI.get_machine_uri(machine),
            {
                'op': 'release_sticky_ip_address',
                'address': ips[0].ip
            })
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_machine = json_load_bytes(response.content)
        self.expectThat(parsed_machine["ip_addresses"], HasLength(0))

    def test__rejected_if_not_permitted(self):
        machine = factory.make_Node_with_Interface_on_Subnet(
            status=NODE_STATUS.ALLOCATED, disable_ipv4=False,
            owner=factory.make_User())
        boot_interface = machine.get_boot_interface()
        # Silence 'update_host_maps' and 'remove_host_maps'
        self.patch(interface_module, "update_host_maps")
        self.patch(interface_module, "remove_host_maps")
        with transaction.atomic():
            boot_interface.claim_static_ips()
        response = self.client.post(
            TestMachineReleaseStickyIpAddressAPI.get_machine_uri(machine),
            {'op': 'release_sticky_ip_address'})
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)


class TestGetDetails(APITestCase):
    """Tests for /api/1.0/machines/<machine>/?op=details."""

    def make_lshw_result(self, machine, script_result=0):
        return factory.make_NodeResult_for_commissioning(
            node=machine, name=commissioningscript.LSHW_OUTPUT_NAME,
            script_result=script_result)

    def make_lldp_result(self, machine, script_result=0):
        return factory.make_NodeResult_for_commissioning(
            node=machine, name=commissioningscript.LLDP_OUTPUT_NAME,
            script_result=script_result)

    def get_details(self, machine):
        url = reverse('machine_handler', args=[machine.system_id])
        response = self.client.get(url, {'op': 'details'})
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual('application/bson', response['content-type'])
        return bson.BSON(response.content).decode()

    def test_GET_returns_empty_details_when_there_are_none(self):
        machine = factory.make_Node()
        self.assertDictEqual(
            {"lshw": None, "lldp": None},
            self.get_details(machine))

    def test_GET_returns_all_details(self):
        machine = factory.make_Node()
        lshw_result = self.make_lshw_result(machine)
        lldp_result = self.make_lldp_result(machine)
        self.assertDictEqual(
            {"lshw": lshw_result.data,
             "lldp": lldp_result.data},
            self.get_details(machine))

    def test_GET_returns_only_those_details_that_exist(self):
        machine = factory.make_Node()
        lshw_result = self.make_lshw_result(machine)
        self.assertDictEqual(
            {"lshw": lshw_result.data,
             "lldp": None},
            self.get_details(machine))

    def test_GET_returns_not_found_when_machine_does_not_exist(self):
        url = reverse('machine_handler', args=['does-not-exist'])
        response = self.client.get(url, {'op': 'details'})
        self.assertEqual(http.client.NOT_FOUND, response.status_code)


class TestMarkBroken(APITestCase):
    """Tests for /api/1.0/machines/<machine>/?op=mark_broken"""

    def get_machine_uri(self, machine):
        """Get the API URI for `machine`."""
        return reverse('machine_handler', args=[machine.system_id])

    def test_mark_broken_changes_status(self):
        machine = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, owner=self.logged_in_user)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'mark_broken'})
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.BROKEN, reload_object(machine).status)

    def test_mark_broken_updates_error_description(self):
        # 'error_description' parameter was renamed 'comment' for consistency
        # make sure this comment updates the machine's error_description
        machine = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, owner=self.logged_in_user)
        comment = factory.make_name('comment')
        response = self.client.post(
            self.get_machine_uri(machine),
            {'op': 'mark_broken', 'comment': comment})
        self.assertEqual(http.client.OK, response.status_code)
        machine = reload_object(machine)
        self.assertEqual(
            (NODE_STATUS.BROKEN, comment),
            (machine.status, machine.error_description)
        )

    def test_mark_broken_updates_error_description_compatibility(self):
        # test old 'error_description' parameter is honored for compatibility
        machine = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, owner=self.logged_in_user)
        error_description = factory.make_name('error_description')
        response = self.client.post(
            self.get_machine_uri(machine),
            {'op': 'mark_broken', 'error_description': error_description})
        self.assertEqual(http.client.OK, response.status_code)
        machine = reload_object(machine)
        self.assertEqual(
            (NODE_STATUS.BROKEN, error_description),
            (machine.status, machine.error_description)
        )

    def test_mark_broken_passes_comment(self):
        machine = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, owner=self.logged_in_user)
        machine_mark_broken = self.patch(node_module.Machine, 'mark_broken')
        comment = factory.make_name('comment')
        self.client.post(
            self.get_machine_uri(machine),
            {'op': 'mark_broken', 'comment': comment})
        self.assertThat(
            machine_mark_broken,
            MockCalledOnceWith(self.logged_in_user, comment))

    def test_mark_broken_handles_missing_comment(self):
        machine = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, owner=self.logged_in_user)
        machine_mark_broken = self.patch(node_module.Machine, 'mark_broken')
        self.client.post(
            self.get_machine_uri(machine), {'op': 'mark_broken'})
        self.assertThat(
            machine_mark_broken,
            MockCalledOnceWith(self.logged_in_user, None))

    def test_mark_broken_requires_ownership(self):
        machine = factory.make_Node(status=NODE_STATUS.COMMISSIONING)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'mark_broken'})
        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_mark_broken_allowed_from_any_other_state(self):
        for status, _ in NODE_STATUS_CHOICES:
            if status == NODE_STATUS.BROKEN:
                continue

            machine = factory.make_Node(
                status=status, owner=self.logged_in_user)
            response = self.client.post(
                self.get_machine_uri(machine), {'op': 'mark_broken'})
            self.expectThat(
                response.status_code, Equals(http.client.OK), response)
            machine = reload_object(machine)
            self.expectThat(machine.status, Equals(NODE_STATUS.BROKEN))


class TestMarkFixed(APITestCase):
    """Tests for /api/1.0/machines/<machine>/?op=mark_fixed"""

    def get_machine_uri(self, machine):
        """Get the API URI for `machine`."""
        return reverse('machine_handler', args=[machine.system_id])

    def test_mark_fixed_changes_status(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.BROKEN)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'mark_fixed'})
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.READY, reload_object(machine).status)

    def test_mark_fixed_requires_admin(self):
        machine = factory.make_Node(status=NODE_STATUS.BROKEN)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'mark_fixed'})
        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_mark_fixed_passes_comment(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.BROKEN)
        machine_mark_fixed = self.patch(node_module.Machine, 'mark_fixed')
        comment = factory.make_name('comment')
        self.client.post(
            self.get_machine_uri(machine),
            {'op': 'mark_fixed', 'comment': comment})
        self.assertThat(
            machine_mark_fixed,
            MockCalledOnceWith(self.logged_in_user, comment))

    def test_mark_fixed_handles_missing_comment(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.BROKEN)
        machine_mark_fixed = self.patch(node_module.Machine, 'mark_fixed')
        self.client.post(
            self.get_machine_uri(machine), {'op': 'mark_fixed'})
        self.assertThat(
            machine_mark_fixed,
            MockCalledOnceWith(self.logged_in_user, None))


class TestPowerParameters(APITestCase):
    def get_machine_uri(self, machine):
        """Get the API URI for `machine`."""
        return reverse('machine_handler', args=[machine.system_id])

    def test_get_power_parameters(self):
        self.become_admin()
        power_parameters = {factory.make_string(): factory.make_string()}
        machine = factory.make_Node(power_parameters=power_parameters)
        response = self.client.get(
            self.get_machine_uri(machine), {'op': 'power_parameters'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_params = json_load_bytes(response.content)
        self.assertEqual(machine.power_parameters, parsed_params)

    def test_get_power_parameters_empty(self):
        self.become_admin()
        machine = factory.make_Node()
        response = self.client.get(
            self.get_machine_uri(machine), {'op': 'power_parameters'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_params = json_load_bytes(response.content)
        self.assertEqual({}, parsed_params)

    def test_power_parameters_requires_admin(self):
        machine = factory.make_Node()
        response = self.client.get(
            self.get_machine_uri(machine), {'op': 'power_parameters'})
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)


class TestAbortOperation(APITransactionTestCase):
    """Tests for /api/1.0/machines/<machine>/?op=abort_operation"""

    def get_machine_uri(self, machine):
        """Get the API URI for `machine`."""
        return reverse('machine_handler', args=[machine.system_id])

    def test_abort_operation_changes_state(self):
        machine = factory.make_Node(
            status=NODE_STATUS.DISK_ERASING, owner=self.logged_in_user)
        machine_stop = self.patch(machine, "stop")
        machine_stop.side_effect = lambda user: post_commit()

        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'abort_operation'})

        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.FAILED_DISK_ERASING, reload_object(machine).status)

    def test_abort_operation_fails_for_unsupported_operation(self):
        machine = factory.make_Node(status=NODE_STATUS.COMMISSIONING)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'abort_operation'})
        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_abort_operation_passes_comment(self):
        self.become_admin()
        machine = factory.make_Node(
            status=NODE_STATUS.DISK_ERASING, owner=self.logged_in_user)
        machine_method = self.patch(node_module.Machine, 'abort_operation')
        comment = factory.make_name('comment')
        self.client.post(
            self.get_machine_uri(machine),
            {'op': 'abort_operation', 'comment': comment})
        self.assertThat(
            machine_method,
            MockCalledOnceWith(self.logged_in_user, comment))

    def test_abort_operation_handles_missing_comment(self):
        self.become_admin()
        machine = factory.make_Node(
            status=NODE_STATUS.DISK_ERASING, owner=self.logged_in_user)
        machine_method = self.patch(node_module.Machine, 'abort_operation')
        self.client.post(
            self.get_machine_uri(machine), {'op': 'abort_operation'})
        self.assertThat(
            machine_method,
            MockCalledOnceWith(self.logged_in_user, None))


class TestSetStorageLayout(APITestCase):

    def get_machine_uri(self, machine):
        """Get the API URI for `machine`."""
        return reverse('machine_handler', args=[machine.system_id])

    def test__403_when_not_admin(self):
        machine = factory.make_Node(status=NODE_STATUS.READY)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'set_storage_layout'})
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)

    def test__409_when_machine_not_ready(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.ALLOCATED)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'set_storage_layout'})
        self.assertEqual(
            http.client.CONFLICT, response.status_code, response.content)

    def test__400_when_storage_layout_missing(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.READY)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'set_storage_layout'})
        self.assertEqual(
            http.client.BAD_REQUEST, response.status_code, response.content)
        self.assertEqual({
            "storage_layout": [
                "This field is required."],
            }, json_load_bytes(response.content))

    def test__400_when_invalid_optional_param(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.READY)
        factory.make_PhysicalBlockDevice(node=machine)
        response = self.client.post(
            self.get_machine_uri(machine), {
                'op': 'set_storage_layout',
                'storage_layout': 'flat',
                'boot_size': MIN_BOOT_PARTITION_SIZE - 1,
                })
        self.assertEqual(
            http.client.BAD_REQUEST, response.status_code, response.content)
        self.assertEqual({
            "boot_size": [
                "Size is too small. Minimum size is %s." % (
                    MIN_BOOT_PARTITION_SIZE)],
            }, json_load_bytes(response.content))

    def test__400_when_no_boot_disk(self):
        self.become_admin()
        machine = factory.make_Node(
            status=NODE_STATUS.READY, with_boot_disk=False)
        response = self.client.post(
            self.get_machine_uri(machine), {
                'op': 'set_storage_layout',
                'storage_layout': 'flat',
                })
        self.assertEqual(
            http.client.BAD_REQUEST, response.status_code, response.content)
        self.assertEqual(
            "Machine is missing a boot disk; no storage layout can be "
            "applied.", response.content.decode(settings.DEFAULT_CHARSET))

    def test__400_when_layout_error(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.READY)
        mock_set_storage_layout = self.patch(Machine, "set_storage_layout")
        error_msg = factory.make_name("error")
        mock_set_storage_layout.side_effect = StorageLayoutError(error_msg)

        response = self.client.post(
            self.get_machine_uri(machine), {
                'op': 'set_storage_layout',
                'storage_layout': 'flat',
                })
        self.assertEqual(
            http.client.BAD_REQUEST, response.status_code, response.content)
        self.assertEqual(
            "Failed to configure storage layout 'flat': %s" % error_msg,
            response.content.decode(settings.DEFAULT_CHARSET))

    def test__400_when_layout_not_supported(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.READY)
        factory.make_PhysicalBlockDevice(node=machine)
        response = self.client.post(
            self.get_machine_uri(machine), {
                'op': 'set_storage_layout',
                'storage_layout': 'bcache',
                })
        self.assertEqual(
            http.client.BAD_REQUEST, response.status_code, response.content)
        self.assertEqual(
            "Failed to configure storage layout 'bcache': Node doesn't "
            "have an available cache device to setup bcache.",
            response.content.decode(settings.DEFAULT_CHARSET))

    def test__calls_set_storage_layout_on_machine(self):
        self.become_admin()
        machine = factory.make_Node(status=NODE_STATUS.READY)
        mock_set_storage_layout = self.patch(Machine, "set_storage_layout")
        response = self.client.post(
            self.get_machine_uri(machine), {
                'op': 'set_storage_layout',
                'storage_layout': 'flat',
                })
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertThat(
            mock_set_storage_layout,
            MockCalledOnceWith('flat', params=ANY, allow_fallback=False))


class TestClearDefaultGateways(APITestCase):

    def get_machine_uri(self, machine):
        """Get the API URI for `machine`."""
        return reverse('machine_handler', args=[machine.system_id])

    def test__403_when_not_admin(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, status=NODE_STATUS.ALLOCATED)
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'clear_default_gateways'})
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)

    def test__clears_default_gateways(self):
        self.become_admin()
        machine = factory.make_Node(
            owner=self.logged_in_user, status=NODE_STATUS.ALLOCATED)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=machine)
        network_v4 = factory.make_ipv4_network()
        subnet_v4 = factory.make_Subnet(
            cidr=str(network_v4.cidr), vlan=interface.vlan)
        link_v4 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, ip="",
            subnet=subnet_v4, interface=interface)
        machine.gateway_link_ipv4 = link_v4
        network_v6 = factory.make_ipv6_network()
        subnet_v6 = factory.make_Subnet(
            cidr=str(network_v6.cidr), vlan=interface.vlan)
        link_v6 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, ip="",
            subnet=subnet_v6, interface=interface)
        machine.gateway_link_ipv6 = link_v6
        machine.save()
        response = self.client.post(
            self.get_machine_uri(machine), {'op': 'clear_default_gateways'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        machine = reload_object(machine)
        self.assertIsNone(machine.gateway_link_ipv4)
        self.assertIsNone(machine.gateway_link_ipv6)


class TestGetCurtinConfig(APITestCase):

    def get_machine_uri(self, machine):
        """Get the API URI for `machine`."""
        return reverse('machine_handler', args=[machine.system_id])

    def test__500_when_machine_not_in_deployment_state(self):
        machine = factory.make_Node(
            owner=self.logged_in_user,
            status=factory.pick_enum(
                NODE_STATUS, but_not=[
                    NODE_STATUS.DEPLOYING,
                    NODE_STATUS.DEPLOYED,
                    NODE_STATUS.FAILED_DEPLOYMENT,
                ]))
        response = self.client.get(
            self.get_machine_uri(machine), {'op': 'get_curtin_config'})
        self.assertEqual(
            http.client.BAD_REQUEST, response.status_code, response.content)

    def test__returns_curtin_config_in_yaml(self):
        machine = factory.make_Node(
            owner=self.logged_in_user, status=NODE_STATUS.DEPLOYING)
        fake_config = {
            "config": factory.make_name("config")
        }
        mock_get_curtin_merged_config = self.patch(
            machines_module, "get_curtin_merged_config")
        mock_get_curtin_merged_config.return_value = fake_config
        response = self.client.get(
            self.get_machine_uri(machine), {'op': 'get_curtin_config'})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertEqual(
            yaml.safe_dump(fake_config, default_flow_style=False),
            response.content.decode(settings.DEFAULT_CHARSET))
        self.assertThat(
            mock_get_curtin_merged_config, MockCalledOnceWith(machine))