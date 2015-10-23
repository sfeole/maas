# Copyright 2014-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the ``maasregiond`` TAP."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = []

from operator import setitem
import random

import crochet
from django.db import connections
from django.db.backends import BaseDatabaseWrapper
from maasserver import eventloop
from maasserver.plugin import (
    Options,
    RegionServiceMaker,
)
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import (
    disable_all_database_connections,
    DisabledDatabaseConnection,
    enable_all_database_connections,
)
from maastesting.factory import factory
from maastesting.matchers import (
    MockCalledOnceWith,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase
from provisioningserver import logger
from provisioningserver.utils.twisted import (
    asynchronous,
    ThreadPool,
)
from south import migration
from testtools import monkey
from testtools.matchers import IsInstance
from twisted.application.service import MultiService
from twisted.internet import reactor


def import_websocket_handlers():
    # Import the websocket handlers for their side-effects: merely defining
    # DeviceHandler, e.g., causes a database access, which will crash if it
    # happens inside the reactor thread where database access is forbidden and
    # prevented. The most sensible solution to this might be to disallow
    # database access at import time.
    import maasserver.websockets.handlers  # noqa


class TestOptions(MAASTestCase):
    """Tests for `maasserver.plugin.Options`."""

    def test_defaults(self):
        options = Options()
        self.assertEqual({"introspect": None}, options.defaults)

    def test_parse_minimal_options(self):
        options = Options()
        # The minimal set of options that must be provided.
        arguments = []
        options.parseOptions(arguments)  # No error.


class TestRegionServiceMaker(MAASTestCase):
    """Tests for `maasserver.plugin.RegionServiceMaker`."""

    def setUp(self):
        super(TestRegionServiceMaker, self).setUp()
        self.patch(eventloop.loop, "services", MultiService())
        self.patch_autospec(crochet, "no_setup")
        self.patch_autospec(logger, "basicConfig")
        # Enable database access in the reactor just for these tests.
        asynchronous(enable_all_database_connections, 5)()
        # _checkDatabase() is tested separately; see later.
        self.patch_autospec(RegionServiceMaker, "_checkDatabase")
        import_websocket_handlers()

    def tearDown(self):
        super(TestRegionServiceMaker, self).tearDown()
        # Disable database access in the reactor again.
        asynchronous(disable_all_database_connections, 5)()

    def test_init(self):
        service_maker = RegionServiceMaker("Harry", "Hill")
        self.assertEqual("Harry", service_maker.tapname)
        self.assertEqual("Hill", service_maker.description)

    @asynchronous(timeout=5)
    def test_makeService(self):
        options = Options()
        service_maker = RegionServiceMaker("Harry", "Hill")
        # Disable _configureThreads() as it's too invasive right now.
        self.patch_autospec(service_maker, "_configureThreads")
        service = service_maker.makeService(options)
        self.assertIsInstance(service, MultiService)
        expected_services = [
            "database-tasks",
            "import-resources",
            "import-resources-progress",
            "nonce-cleanup",
            "rpc",
            "rpc-advertise",
            "web",
        ]
        self.assertItemsEqual(expected_services, service.namedServices)
        self.assertEqual(
            len(service.namedServices), len(service.services),
            "Not all services are named.")
        self.assertThat(logger.basicConfig, MockCalledOnceWith())
        self.assertThat(crochet.no_setup, MockCalledOnceWith())
        self.assertThat(
            RegionServiceMaker._checkDatabase,
            MockCalledOnceWith(service_maker))

    @asynchronous(timeout=5)
    def test_configures_thread_pool(self):
        # Patch and restore where it's visible because patching a running
        # reactor is potentially fairly harmful.
        patcher = monkey.MonkeyPatcher()
        patcher.add_patch(reactor, "threadpool", None)
        patcher.add_patch(reactor, "threadpoolForDatabase", None)
        patcher.patch()
        try:
            service_maker = RegionServiceMaker("Harry", "Hill")
            service_maker.makeService(Options())
            threadpool = reactor.getThreadPool()
            self.assertThat(threadpool, IsInstance(ThreadPool))
        finally:
            patcher.restore()

    def assertConnectionsEnabled(self):
        for alias in connections:
            self.assertThat(
                connections[alias],
                IsInstance(BaseDatabaseWrapper))

    def assertConnectionsDisabled(self):
        for alias in connections:
            self.assertEqual(
                DisabledDatabaseConnection,
                type(connections[alias]))

    @asynchronous(timeout=5)
    def test_disables_database_connections_in_reactor(self):
        self.assertConnectionsEnabled()
        service_maker = RegionServiceMaker("Harry", "Hill")
        # Disable _configureThreads() as it's too invasive right now.
        self.patch_autospec(service_maker, "_configureThreads")
        service_maker.makeService(Options())
        self.assertConnectionsDisabled()


class TestRegionServiceMakerDatabaseChecks(MAASServerTestCase):
    """Tests for `maasserver.plugin.RegionServiceMaker._checkDatabase`."""

    def setUp(self):
        super(TestRegionServiceMakerDatabaseChecks, self).setUp()
        import_websocket_handlers()

    @asynchronous(timeout=5)
    def test__checks_database_connectivity_early(self):
        exception_type = factory.make_exception_type()
        service_maker = RegionServiceMaker("Harry", "Hill")
        _checkDatabase = self.patch_autospec(service_maker, "_checkDatabase")
        _checkDatabase.side_effect = exception_type
        # Disable _configureThreads() as it's too invasive right now.
        self.patch_autospec(service_maker, "_configureThreads")
        self.patch_autospec(eventloop.loop, "populate")
        self.assertRaises(exception_type, service_maker.makeService, Options())
        self.assertThat(_checkDatabase, MockCalledOnceWith())
        self.assertThat(eventloop.loop.populate, MockNotCalled())

    def test__completes_quietly_if_database_can_be_connected_to(self):
        service_maker = RegionServiceMaker("Harry", "Hill")
        try:
            service_maker._checkDatabase()
        except SystemExit as error:
            # Django/South sometimes declares that all migrations have been
            # applied, sometimes it declares that none have been applied, and
            # it appears to depend on what other tests have run or are due to
            # run. This is highly irritating. This workaround is ugly and
            # diminishes the value of this test, but it also avoids a long and
            # expensive diving expedition into Django's convoluted innards.
            self.assertDocTestMatches(
                "The MAAS database schema is not yet fully installed: "
                "... migration(s) are missing.", unicode(error))
        else:
            # This is what was meant to happen.
            pass

    def test__complains_if_database_cannot_be_connected_to(self):
        # Disable all database connections in this thread.
        for alias in connections:
            self.addCleanup(setitem, connections, alias, connections[alias])
            connections[alias] = DisabledDatabaseConnection()

        service_maker = RegionServiceMaker("Harry", "Hill")
        error = self.assertRaises(SystemExit, service_maker._checkDatabase)
        self.assertDocTestMatches(
            "The MAAS database cannot be used. Please investigate: ...",
            unicode(error))

    def test__complains_if_not_all_migrations_have_been_applied(self):

        def random_unapplied(migrations, _):
            # Always declare that one migration has not been applied.
            return [random.choice(migrations)]

        self.patch(migration, "get_unapplied_migrations", random_unapplied)

        service_maker = RegionServiceMaker("Harry", "Hill")
        error = self.assertRaises(SystemExit, service_maker._checkDatabase)
        self.assertEqual(
            "The MAAS database schema is not yet fully installed: "
            "1 migration(s) are missing.", unicode(error))