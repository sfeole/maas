# Copyright 2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for :py:module:`~maasserver.rpc.events`."""

__all__ = []


import datetime
import logging

from maasserver.models.event import Event
from maasserver.models.eventtype import EventType
from maasserver.rpc import events
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from provisioningserver.rpc.exceptions import NoSuchEventType


class TestRegisterEventType(MAASServerTestCase):

    def test__registers_type(self):
        name = factory.make_name('name')
        description = factory.make_name('description')
        level = logging.DEBUG
        events.register_event_type(name, description, level)
        # Doesn't raise a DoesNotExist error.
        EventType.objects.get(
            name=name, description=description, level=level)


class TestSendEvent(MAASServerTestCase):

    def test__errors_when_no_event_type(self):
        name = factory.make_name('name')
        description = factory.make_name('description')
        node = factory.make_Node()
        self.assertRaises(
            NoSuchEventType, events.send_event,
            node.system_id, name, description, datetime.datetime.utcnow())

    def test__silent_when_no_node(self):
        event_type = factory.make_EventType()
        description = factory.make_name('description')
        # Exception should not be raised.
        events.send_event(
            factory.make_name('system_id'), event_type.name,
            description, datetime.datetime.utcnow())

    def test__creates_event_for_node(self):
        event_type = factory.make_EventType()
        node = factory.make_Node()
        description = factory.make_name('description')
        timestamp = datetime.datetime.utcnow()
        events.send_event(
            node.system_id, event_type.name,
            description, timestamp)
        # Doesn't raise a DoesNotExist error.
        Event.objects.get(
            node=node, type=event_type, description=description,
            created=timestamp)


class TestSendEventMACAddress(MAASServerTestCase):

    def test__errors_when_no_event_type(self):
        name = factory.make_name('name')
        description = factory.make_name('description')
        node = factory.make_Node()
        self.assertRaises(
            NoSuchEventType, events.send_event_mac_address,
            node.system_id, name, description, datetime.datetime.utcnow())

    def test__silent_when_no_node(self):
        event_type = factory.make_EventType()
        description = factory.make_name('description')
        # Exception should not be raised.
        events.send_event_mac_address(
            factory.make_mac_address(), event_type.name,
            description, datetime.datetime.utcnow())

    def test__creates_event_for_node(self):
        event_type = factory.make_EventType()
        node = factory.make_Node(interface=True)
        description = factory.make_name('description')
        timestamp = datetime.datetime.utcnow()
        mac_address = node.interface_set.first().mac_address
        events.send_event_mac_address(
            mac_address, event_type.name,
            description, timestamp)
        # Doesn't raise a DoesNotExist error.
        Event.objects.get(
            node=node, type=event_type, description=description,
            created=timestamp)
