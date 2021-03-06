# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""API handlers: `Bcache`."""

from maasserver.api.support import OperationsHandler
from maasserver.enum import (
    NODE_PERMISSION,
    NODE_STATUS,
)
from maasserver.exceptions import (
    MAASAPIValidationError,
    NodeStateViolation,
)
from maasserver.forms import (
    CreateBcacheForm,
    UpdateBcacheForm,
)
from maasserver.models import (
    Bcache,
    Machine,
)
from maasserver.utils.converters import human_readable_bytes
from piston3.utils import rc


DISPLAYED_BCACHE_FIELDS = (
    'system_id',
    'id',
    'uuid',
    'name',
    'cache_set',
    'backing_device',
    'size',
    'human_size',
    'virtual_device',
    'cache_mode',
)


class BcachesHandler(OperationsHandler):
    """Manage bcache devices on a machine."""
    api_doc_section_name = "Bcache Devices"
    update = delete = None
    fields = DISPLAYED_BCACHE_FIELDS

    @classmethod
    def resource_uri(cls, *args, **kwargs):
        # See the comment in NodeHandler.resource_uri.
        return ('bcache_devices_handler', ["system_id"])

    def read(self, request, system_id):
        """List all bcache devices belonging to a machine.

        Returns 404 if the machine is not found.
        """
        machine = Machine.objects.get_node_or_404(
            system_id, request.user, NODE_PERMISSION.VIEW)
        return Bcache.objects.filter_by_node(machine)

    def create(self, request, system_id):
        """Creates a Bcache.

        :param name: Name of the Bcache.
        :param uuid: UUID of the Bcache.
        :param cache_set: Cache set.
        :param backing_device: Backing block device.
        :param backing_partition: Backing partition.
        :param cache_mode: Cache mode (WRITEBACK, WRITETHROUGH, WRITEAROUND).

        Specifying both a device and a partition for a given role (cache or
        backing) is not allowed.

        Returns 404 if the machine is not found.
        Returns 409 if the machine is not Ready.
        """
        machine = Machine.objects.get_node_or_404(
            system_id, request.user, NODE_PERMISSION.ADMIN)
        if machine.status != NODE_STATUS.READY:
            raise NodeStateViolation(
                "Cannot create Bcache because the machine is not Ready.")
        form = CreateBcacheForm(machine, data=request.data)
        if form.is_valid():
            return form.save()
        else:
            raise MAASAPIValidationError(form.errors)


class BcacheHandler(OperationsHandler):
    """Manage bcache device on a machine."""
    api_doc_section_name = "Bcache Device"
    create = None
    model = Bcache
    fields = DISPLAYED_BCACHE_FIELDS

    @classmethod
    def resource_uri(cls, bcache=None):
        # See the comment in NodeHandler.resource_uri.
        system_id = "system_id"
        bcache_id = "id"
        if bcache is not None:
            bcache_id = bcache.id
            node = bcache.get_node()
            if node is not None:
                system_id = node.system_id
        return ('bcache_device_handler', (system_id, bcache_id))

    @classmethod
    def system_id(cls, bcache):
        node = bcache.get_node()
        return None if node is None else node.system_id

    @classmethod
    def size(cls, bcache):
        return bcache.get_size()

    @classmethod
    def human_size(cls, bcache):
        return human_readable_bytes(bcache.get_size())

    @classmethod
    def virtual_device(cls, bcache):
        """Return the `VirtualBlockDevice` of bcache device."""
        return bcache.virtual_device

    @classmethod
    def backing_device(cls, bcache):
        """Return the backing device for this bcache."""
        return bcache.get_bcache_backing_filesystem().get_parent()

    def read(self, request, system_id, id):
        """Read bcache device on a machine.

        Returns 404 if the machine or bcache is not found.
        """
        return Bcache.objects.get_object_or_404(
            system_id, id, request.user, NODE_PERMISSION.VIEW)

    def delete(self, request, system_id, id):
        """Delete bcache on a machine.

        Returns 404 if the machine or bcache is not found.
        Returns 409 if the machine is not Ready.
        """
        bcache = Bcache.objects.get_object_or_404(
            system_id, id, request.user, NODE_PERMISSION.ADMIN)
        node = bcache.get_node()
        if node.status != NODE_STATUS.READY:
            raise NodeStateViolation(
                "Cannot delete Bcache because the machine is not Ready.")
        bcache.delete()
        return rc.DELETED

    def update(self, request, system_id, id):
        """Delete bcache on a machine.

        :param name: Name of the Bcache.
        :param uuid: UUID of the Bcache.
        :param cache_set: Cache set to replace current one.
        :param backing_device: Backing block device to replace current one.
        :param backing_partition: Backing partition to replace current one.
        :param cache_mode: Cache mode (writeback, writethrough, writearound).

        Specifying both a device and a partition for a given role (cache or
        backing) is not allowed.

        Returns 404 if the machine or the bcache is not found.
        Returns 409 if the machine is not Ready.
        """
        bcache = Bcache.objects.get_object_or_404(
            system_id, id, request.user, NODE_PERMISSION.ADMIN)
        node = bcache.get_node()
        if node.status != NODE_STATUS.READY:
            raise NodeStateViolation(
                "Cannot update Bcache because the machine is not Ready.")
        form = UpdateBcacheForm(bcache, data=request.data)
        if form.is_valid():
            return form.save()
        else:
            raise MAASAPIValidationError(form.errors)
