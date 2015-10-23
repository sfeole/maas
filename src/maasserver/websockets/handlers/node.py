# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""The node handler for the WebSocket connection."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
)

str = None

__metaclass__ = type
__all__ = [
    "NodeHandler",
]

import logging
from operator import itemgetter

from django.core.exceptions import ValidationError
from lxml import etree
from maasserver.enum import (
    FILESYSTEM_FORMAT_TYPE_CHOICES,
    FILESYSTEM_FORMAT_TYPE_CHOICES_DICT,
    INTERFACE_LINK_TYPE,
    IPADDRESS_TYPE,
    NODE_PERMISSION,
    NODE_STATUS,
)
from maasserver.exceptions import NodeActionError
from maasserver.forms import (
    AddPartitionForm,
    AdminNodeWithMACAddressesForm,
    CreateBcacheForm,
    CreateCacheSetForm,
    CreateRaidForm,
    FormatBlockDeviceForm,
    FormatPartitionForm,
    MountBlockDeviceForm,
    MountPartitionForm,
)
from maasserver.forms_interface import (
    BondInterfaceForm,
    InterfaceForm,
    VLANInterfaceForm,
)
from maasserver.forms_interface_link import InterfaceLinkForm
from maasserver.models.blockdevice import BlockDevice
from maasserver.models.cacheset import CacheSet
from maasserver.models.config import Config
from maasserver.models.event import Event
from maasserver.models.filesystemgroup import VolumeGroup
from maasserver.models.interface import Interface
from maasserver.models.node import Node
from maasserver.models.nodegroup import NodeGroup
from maasserver.models.nodeprobeddetails import get_single_probed_details
from maasserver.models.partition import Partition
from maasserver.models.physicalblockdevice import PhysicalBlockDevice
from maasserver.models.subnet import Subnet
from maasserver.models.tag import Tag
from maasserver.models.virtualblockdevice import VirtualBlockDevice
from maasserver.node_action import compile_node_actions
from maasserver.rpc import getClientFor
from maasserver.third_party_drivers import get_third_party_driver
from maasserver.utils.converters import (
    human_readable_bytes,
    XMLToYAML,
)
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
from maasserver.websockets.base import (
    HandlerDoesNotExistError,
    HandlerError,
    HandlerPermissionError,
)
from maasserver.websockets.handlers.event import dehydrate_event_type_level
from maasserver.websockets.handlers.timestampedmodel import (
    dehydrate_datetime,
    TimestampedModelHandler,
)
from metadataserver.enum import RESULT_TYPE
from metadataserver.models import NodeResult
from provisioningserver.drivers.power import POWER_QUERY_TIMEOUT
from provisioningserver.logger import get_maas_logger
from provisioningserver.power.poweraction import (
    PowerActionFail,
    UnknownPowerType,
)
from provisioningserver.rpc.cluster import PowerQuery
from provisioningserver.rpc.exceptions import NoConnectionsAvailable
from provisioningserver.tags import merge_details_cleanly
from provisioningserver.utils.twisted import (
    asynchronous,
    deferWithTimeout,
)
from twisted.internet.defer import (
    CancelledError,
    inlineCallbacks,
    returnValue,
)


maaslog = get_maas_logger("websockets.node")


class NodeHandler(TimestampedModelHandler):

    class Meta:
        queryset = (
            Node.nodes.filter(installable=True)
                .select_related('nodegroup', 'pxe_mac', 'owner')
                .prefetch_related('interface_set__ip_addresses__subnet__vlan')
                .prefetch_related('nodegroup__nodegroupinterface_set__subnet')
                .prefetch_related('zone')
                .prefetch_related('tags')
                .prefetch_related('blockdevice_set__physicalblockdevice')
                .prefetch_related('blockdevice_set__virtualblockdevice'))
        pk = 'system_id'
        pk_type = unicode
        allowed_methods = [
            'list',
            'get',
            'create',
            'update',
            'action',
            'set_active',
            'check_power',
            'create_vlan',
            'create_bond',
            'update_interface',
            'delete_interface',
            'link_subnet',
            'unlink_subnet',
            'update_filesystem',
            'update_disk_tags',
            'delete_disk',
            'delete_partition',
            'delete_volume_group',
            'delete_cache_set',
            'create_partition',
            'create_cache_set',
            'create_bcache',
            'create_raid',
        ]
        form = AdminNodeWithMACAddressesForm
        exclude = [
            "installable",
            "parent",
            "boot_interface",
            "boot_cluster_ip",
            "token",
            "netboot",
            "agent_name",
            "power_state_updated",
            "gateway_link_ipv4",
            "gateway_link_ipv6",
            "block_poweroff",
            "enable_ssh",
            "skip_networking",
            "skip_storage",
        ]
        list_fields = [
            "system_id",
            "hostname",
            "owner",
            "cpu_count",
            "memory",
            "power_state",
            "zone",
        ]
        listen_channels = [
            "node",
        ]

    def get_queryset(self):
        """Return `QuerySet` for nodes only vewable by `user`."""
        nodes = super(NodeHandler, self).get_queryset()
        return Node.nodes.get_nodes(
            self.user, NODE_PERMISSION.VIEW, from_nodes=nodes)

    def dehydrate_owner(self, user):
        """Return owners username."""
        if user is None:
            return ""
        else:
            return user.username

    def dehydrate_zone(self, zone):
        """Return zone name."""
        return {
            "id": zone.id,
            "name": zone.name,
        }

    def dehydrate_nodegroup(self, nodegroup):
        """Return the nodegroup name."""
        if nodegroup is None:
            return None
        else:
            return {
                "id": nodegroup.id,
                "uuid": nodegroup.uuid,
                "name": nodegroup.name,
                "cluster_name": nodegroup.cluster_name,
            }

    def dehydrate_routers(self, routers):
        """Return list of routers."""
        if routers is None:
            return []
        return [
            "%s" % router
            for router in routers
        ]

    def dehydrate_power_parameters(self, power_parameters):
        """Return power_parameters None if empty."""
        if power_parameters == '':
            return None
        else:
            return power_parameters

    def dehydrate(self, obj, data, for_list=False):
        """Add extra fields to `data`."""
        data["fqdn"] = obj.fqdn
        data["status"] = obj.display_status()
        data["actions"] = compile_node_actions(obj, self.user).keys()
        data["memory"] = obj.display_memory()

        data["extra_macs"] = [
            "%s" % mac_address
            for mac_address in obj.get_extra_macs()
        ]
        boot_interface = obj.get_boot_interface()
        if boot_interface is not None:
            data["pxe_mac"] = "%s" % boot_interface.mac_address
            data["pxe_mac_vendor"] = obj.get_pxe_mac_vendor()
        else:
            data["pxe_mac"] = data["pxe_mac_vendor"] = ""

        blockdevices = self.get_blockdevices_for(obj)
        physical_blockdevices = [
            blockdevice for blockdevice in blockdevices
            if isinstance(blockdevice, PhysicalBlockDevice)
            ]
        data["physical_disk_count"] = len(physical_blockdevices)
        data["storage"] = "%3.1f" % (
            sum([
                blockdevice.size
                for blockdevice in physical_blockdevices
                ]) / (1000 ** 3))
        data["storage_tags"] = self.get_all_storage_tags(blockdevices)

        data["tags"] = [
            tag.name
            for tag in obj.tags.all()
        ]
        if not for_list:
            data["show_os_info"] = self.dehydrate_show_os_info(obj)
            data["osystem"] = obj.get_osystem()
            data["distro_series"] = obj.get_distro_series()
            data["hwe_kernel"] = obj.hwe_kernel

            # Network
            data["interfaces"] = [
                self.dehydrate_interface(interface, obj)
                for interface in obj.interface_set.all().order_by('name')
            ]

            # Devices
            devices = [
                self.dehydrate_device(device)
                for device in obj.children.all()
            ]
            data["devices"] = sorted(
                devices, key=itemgetter("fqdn"))

            # Storage
            data["disks"] = [
                self.dehydrate_blockdevice(blockdevice)
                for blockdevice in blockdevices
            ]
            data["disks"] = data["disks"] + [
                self.dehydrate_volume_group(volume_group)
                for volume_group in VolumeGroup.objects.filter_by_node(obj)
            ] + [
                self.dehydrate_cache_set(cache_set)
                for cache_set in CacheSet.objects.get_cache_sets_for_node(obj)
            ]
            data["disks"] = sorted(data["disks"], key=itemgetter("name"))
            data["supported_filesystems"] = [
                {'key': key, 'ui': ui}
                for key, ui in FILESYSTEM_FORMAT_TYPE_CHOICES
            ]

            # Events
            data["events"] = self.dehydrate_events(obj)

            # Machine output
            data = self.dehydrate_summary_output(obj, data)
            data["commissioning_results"] = self.dehydrate_node_results(
                obj, RESULT_TYPE.COMMISSIONING)
            data["installation_results"] = self.dehydrate_node_results(
                obj, RESULT_TYPE.INSTALLATION)

            # Third party drivers
            if Config.objects.get_config('enable_third_party_drivers'):
                driver = get_third_party_driver(obj)
                if "module" in driver and "comment" in driver:
                    data["third_party_driver"] = {
                        "module": driver["module"],
                        "comment": driver["comment"],
                    }

        return data

    def dehydrate_show_os_info(self, obj):
        """Return True if OS information should show in the UI."""
        return (
            obj.status == NODE_STATUS.DEPLOYING or
            obj.status == NODE_STATUS.FAILED_DEPLOYMENT or
            obj.status == NODE_STATUS.DEPLOYED or
            obj.status == NODE_STATUS.RELEASING or
            obj.status == NODE_STATUS.FAILED_RELEASING or
            obj.status == NODE_STATUS.DISK_ERASING or
            obj.status == NODE_STATUS.FAILED_DISK_ERASING)

    def dehydrate_device(self, device):
        """Return the `Device` formatted for JSON encoding."""
        return {
            "fqdn": device.fqdn,
            "interfaces": [
                self.dehydrate_interface(interface, device)
                for interface in device.interface_set.all().order_by('id')
            ],
        }

    def dehydrate_blockdevice(self, blockdevice):
        """Return `BlockDevice` formatted for JSON encoding."""
        # model and serial are currently only avalible on physical block
        # devices
        if isinstance(blockdevice, PhysicalBlockDevice):
            model = blockdevice.model
            serial = blockdevice.serial
        else:
            serial = model = ""
        partition_table = blockdevice.get_partitiontable()
        if partition_table is not None:
            partition_table_type = partition_table.table_type
        else:
            partition_table_type = ""
        data = {
            "id": blockdevice.id,
            "name": blockdevice.get_name(),
            "tags": blockdevice.tags,
            "type": blockdevice.type,
            "path": blockdevice.path,
            "size": blockdevice.size,
            "size_human": human_readable_bytes(blockdevice.size),
            "used_size": blockdevice.used_size,
            "used_size_human": human_readable_bytes(
                blockdevice.used_size),
            "available_size": blockdevice.available_size,
            "available_size_human": human_readable_bytes(
                blockdevice.available_size),
            "block_size": blockdevice.block_size,
            "model": model,
            "serial": serial,
            "partition_table_type": partition_table_type,
            "used_for": blockdevice.used_for,
            "filesystem": self.dehydrate_filesystem(
                blockdevice.get_effective_filesystem()),
            "partitions": self.dehydrate_partitions(
                blockdevice.get_partitiontable()),
        }
        if isinstance(blockdevice, VirtualBlockDevice):
            data["parent"] = {
                "id": blockdevice.filesystem_group.id,
                "uuid": blockdevice.filesystem_group.uuid,
                "type": blockdevice.filesystem_group.group_type,
            }
        return data

    def dehydrate_volume_group(self, volume_group):
        """Return `VolumeGroup` formatted for JSON encoding."""
        size = volume_group.get_size()
        available_size = volume_group.get_lvm_free_space()
        used_size = volume_group.get_lvm_allocated_size()
        return {
            "id": volume_group.id,
            "name": volume_group.name,
            "tags": [],
            "type": volume_group.group_type,
            "path": "",
            "size": size,
            "size_human": human_readable_bytes(size),
            "used_size": used_size,
            "used_size_human": human_readable_bytes(used_size),
            "available_size": available_size,
            "available_size_human": human_readable_bytes(available_size),
            "block_size": volume_group.get_virtual_block_device_block_size(),
            "model": "",
            "serial": "",
            "partition_table_type": "",
            "used_for": "volume group",
            "filesystem": None,
            "partitions": None,
        }

    def dehydrate_cache_set(self, cache_set):
        """Return `CacheSet` formatted for JSON encoding."""
        device = cache_set.get_device()
        used_size = device.get_used_size()
        available_size = device.get_available_size()
        bcache_devices = sorted([
            bcache.name
            for bcache in cache_set.filesystemgroup_set.all()
        ])
        return {
            "id": cache_set.id,
            "name": cache_set.name,
            "tags": [],
            "type": "cache-set",
            "path": "",
            "size": device.size,
            "size_human": human_readable_bytes(device.size),
            "used_size": used_size,
            "used_size_human": human_readable_bytes(used_size),
            "available_size": available_size,
            "available_size_human": human_readable_bytes(available_size),
            "block_size": device.get_block_size(),
            "model": "",
            "serial": "",
            "partition_table_type": "",
            "used_for": ", ".join(bcache_devices),
            "filesystem": None,
            "partitions": None,
        }

    def dehydrate_partitions(self, partition_table):
        """Return `PartitionTable` formatted for JSON encoding."""
        if partition_table is None:
            return None
        partitions = []
        for partition in partition_table.partitions.all():
            partitions.append({
                "filesystem": self.dehydrate_filesystem(
                    partition.get_effective_filesystem()),
                "name": partition.get_name(),
                "path": partition.path,
                "type": partition.type,
                "id": partition.id,
                "size": partition.size,
                "size_human": human_readable_bytes(partition.size),
                "used_for": partition.used_for,
            })
        return partitions

    def dehydrate_filesystem(self, filesystem):
        """Return `Filesystem` formatted for JSON encoding."""
        if filesystem is None:
            return None
        return {
            "label": filesystem.label,
            "mount_point": filesystem.mount_point,
            "fstype": filesystem.fstype,
            "is_format_fstype": (
                filesystem.fstype in FILESYSTEM_FORMAT_TYPE_CHOICES_DICT),
            }

    def dehydrate_interface(self, interface, obj):
        """Dehydrate a `interface` into a interface definition."""
        # Sort the links by ID that way they show up in the same order in
        # the UI.
        links = sorted(interface.get_links(), key=itemgetter("id"))
        for link in links:
            # Replace the subnet object with the subnet_id. The client will
            # use this information to pull the subnet information from the
            # websocket.
            subnet = link.pop("subnet", None)
            if subnet is not None:
                link["subnet_id"] = subnet.id
        return {
            "id": interface.id,
            "type": interface.type,
            "name": interface.get_name(),
            "enabled": interface.is_enabled(),
            "is_boot": interface == obj.boot_interface,
            "mac_address": "%s" % interface.mac_address,
            "vlan_id": interface.vlan_id,
            "parents": [
                nic.id
                for nic in interface.parents.all()
            ],
            "children": [
                nic.child.id
                for nic in interface.children_relationships.all()
            ],
            "links": links,
        }

    def dehydrate_summary_output(self, obj, data):
        """Dehydrate the machine summary output."""
        # Produce a "clean" composite details document.
        probed_details = merge_details_cleanly(
            get_single_probed_details(obj.system_id))

        # We check here if there's something to show instead of after
        # the call to get_single_probed_details() because here the
        # details will be guaranteed well-formed.
        if len(probed_details.xpath('/*/*')) == 0:
            data['summary_xml'] = None
            data['summary_yaml'] = None
        else:
            data['summary_xml'] = etree.tostring(
                probed_details, encoding=unicode, pretty_print=True)
            data['summary_yaml'] = XMLToYAML(
                etree.tostring(
                    probed_details, encoding=unicode,
                    pretty_print=True)).convert()
        return data

    def dehydrate_node_results(self, obj, result_type):
        """Dehydrate node results with the given `result_type`."""
        return [
            {
                "id": result.id,
                "result": result.script_result,
                "name": result.name,
                "data": result.data,
                "line_count": len(result.data.splitlines()),
                "created": dehydrate_datetime(result.created),
            }
            for result in NodeResult.objects.filter(
                node=obj, result_type=result_type)
        ]

    def dehydrate_events(self, obj):
        """Dehydrate the node events.

        The latests 50 not including DEBUG events will be dehydrated. The
        `EventsHandler` needs to be used if more are required.
        """
        events = (
            Event.objects.filter(node=obj)
            .exclude(type__level=logging.DEBUG)
            .select_related("type")
            .order_by('-id')[:50])
        return [
            {
                "id": event.id,
                "type": {
                    "id": event.type.id,
                    "name": event.type.name,
                    "description": event.type.description,
                    "level": dehydrate_event_type_level(event.type.level),
                },
                "description": event.description,
                "created": dehydrate_datetime(event.created),
            }
            for event in events
        ]

    def get_all_storage_tags(self, blockdevices):
        """Return list of all storage tags in `blockdevices`."""
        tags = set()
        for blockdevice in blockdevices:
            tags = tags.union(blockdevice.tags)
        return list(tags)

    def get_blockdevices_for(self, obj):
        """Return only `BlockDevice`s using the prefetched query."""
        return [
            blockdevice.actual_instance
            for blockdevice in obj.blockdevice_set.all()
        ]

    def get_object(self, params):
        """Get object by using the `pk` in `params`."""
        obj = super(NodeHandler, self).get_object(params)
        if self.user.is_superuser:
            return obj
        if obj.owner is None or obj.owner == self.user:
            return obj
        raise HandlerDoesNotExistError(params[self._meta.pk])

    def get_mac_addresses(self, data):
        """Convert the given `data` into a list of mac addresses.

        This is used by the create method and the hydrate method. The `pxe_mac`
        will always be the first entry in the list.
        """
        macs = data.get("extra_macs", [])
        if "pxe_mac" in data:
            macs.insert(0, data["pxe_mac"])
        return macs

    def get_form_class(self, action):
        """Return the form class used for `action`."""
        if action in ("create", "update"):
            return AdminNodeWithMACAddressesForm
        else:
            raise HandlerError("Unknown action: %s" % action)

    def preprocess_form(self, action, params):
        """Process the `params` to before passing the data to the form."""
        new_params = {}

        # Only copy the allowed fields into `new_params` to be passed into
        # the form.
        new_params["mac_addresses"] = self.get_mac_addresses(params)
        new_params["hostname"] = params.get("hostname")
        new_params["architecture"] = params.get("architecture")
        new_params["power_type"] = params.get("power_type")
        if "zone" in params:
            new_params["zone"] = params["zone"]["name"]
        if "nodegroup" in params:
            new_params["nodegroup"] = params["nodegroup"]["uuid"]
        if "min_hwe_kernel" in params:
            new_params["min_hwe_kernel"] = params["min_hwe_kernel"]

        # Cleanup any fields that have a None value.
        new_params = {
            key: value
            for key, value in new_params.viewitems()
            if value is not None
        }

        return super(NodeHandler, self).preprocess_form(action, new_params)

    def create(self, params):
        """Create the object from params."""
        # Only admin users can perform create.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        # Create the object, then save the power parameters because the
        # form will not save this information.
        data = super(NodeHandler, self).create(params)
        node_obj = Node.objects.get(system_id=data['system_id'])
        node_obj.power_parameters = params.get("power_parameters", {})
        node_obj.save()

        # Start the commissioning process right away, which has the
        # desired side effect of initializing the node's power state.
        node_obj.start_commissioning(self.user)

        return self.full_dehydrate(node_obj)

    def update(self, params):
        """Update the object from params."""
        # Only admin users can perform update.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        # Update the node with the form. The form will not update the
        # nodegroup or power_parameters, so we perform that logic here.
        data = super(NodeHandler, self).update(params)
        node_obj = Node.objects.get(system_id=data['system_id'])
        node_obj.nodegroup = NodeGroup.objects.get(
            uuid=params['nodegroup']['uuid'])
        node_obj.power_parameters = params.get("power_parameters")
        if node_obj.power_parameters is None:
            node_obj.power_parameters = {}

        # Update the tags for the node and disks.
        self.update_tags(node_obj, params['tags'])
        node_obj.save()
        return self.full_dehydrate(node_obj)

    def update_filesystem(self, params):
        node = self.get_object(params)
        block_id = params.get('block_id')
        partition_id = params.get('partition_id')
        fstype = params.get('fstype')
        mount_point = params.get('mount_point')

        if partition_id:
            self.update_partition_filesystem(
                node, block_id, partition_id, fstype, mount_point)
        else:
            self.update_blockdevice_filesystem(
                node, block_id, fstype, mount_point)

    def update_partition_filesystem(
            self, node, block_id, partition_id, fstype, mount_point):
        partition = Partition.objects.get(
            id=partition_id,
            partition_table__block_device__node=node)
        fs = partition.get_effective_filesystem()
        if not fstype:
            if fs:
                fs.delete()
                return
        if fs is None or fstype != fs.fstype:
            form = FormatPartitionForm(partition, {'fstype': fstype})
            if not form.is_valid():
                raise HandlerError(form.errors)
            form.save()
            fs = partition.get_effective_filesystem()
        if mount_point != fs.mount_point:
            if not mount_point:
                fs.mount_point = None
                fs.save()
            else:
                form = MountPartitionForm(
                    partition, {'mount_point': mount_point})
                if not form.is_valid():
                    raise HandlerError(form.errors)
                else:
                    form.save()

    def update_blockdevice_filesystem(
            self, node, block_id, fstype, mount_point):
        blockdevice = BlockDevice.objects.get(id=block_id, node=node)
        fs = blockdevice.get_effective_filesystem()
        if not fstype:
            if fs:
                fs.delete()
                return
        if fs is None or fstype != fs.fstype:
            form = FormatBlockDeviceForm(blockdevice, {'fstype': fstype})
            if not form.is_valid():
                raise HandlerError(form.errors)
            form.save()
            fs = blockdevice.get_effective_filesystem()
        if mount_point != fs.mount_point:
            if not mount_point:
                fs.mount_point = None
                fs.save()
            else:
                form = MountBlockDeviceForm(
                    blockdevice, {'mount_point': mount_point})
                if not form.is_valid():
                    raise HandlerError(form.errors)
                else:
                    form.save()

    def update_tags(self, node_obj, tags):
        # Loop through the nodes current tags. If the tag exists in `tags` then
        # nothing needs to be done so its removed from `tags`. If it does not
        # exists then the tag was removed from the node and should be removed
        # from the nodes set of tags.
        for tag in node_obj.tags.all():
            if tag.name not in tags:
                node_obj.tags.remove(tag)
            else:
                tags.remove(tag.name)

        # All the tags remaining in `tags` are tags that are not linked to
        # node. Get or create that tag and add the node to the tags set.
        for tag_name in tags:
            tag_obj, _ = Tag.objects.get_or_create(name=tag_name)
            if tag_obj.is_defined:
                raise HandlerError(
                    "Cannot add tag %s to node because it has a "
                    "definition." % tag_name)
            tag_obj.node_set.add(node_obj)
            tag_obj.save()

    def update_disk_tags(self, params):
        """Update all the tags on all disks."""
        node = self.get_object(params)
        disk_obj = BlockDevice.objects.get(id=params['block_id'], node=node)
        disk_obj.tags = params['tags']
        disk_obj.save(update_fields=['tags'])

    def delete_disk(self, params):
        # Only admin users can perform delete.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        node = self.get_object(params)
        block_id = params.get('block_id')
        if block_id is not None:
            block_device = BlockDevice.objects.get(id=block_id, node=node)
            block_device.delete()

    def delete_partition(self, params):
        # Only admin users can perform delete.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        node = self.get_object(params)
        partition_id = params.get('partition_id')
        if partition_id is not None:
            partition = Partition.objects.get(
                id=partition_id, partition_table__block_device__node=node)
            partition.delete()

    def delete_volume_group(self, params):
        # Only admin users can perform delete.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        node = self.get_object(params)
        volume_group_id = params.get('volume_group_id')
        if volume_group_id is not None:
            volume_group = VolumeGroup.objects.get(id=volume_group_id)
            if volume_group.get_node() != node:
                raise VolumeGroup.DoesNotExist()
            volume_group.delete()

    def delete_cache_set(self, params):
        # Only admin users can perform delete.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        node = self.get_object(params)
        cache_set_id = params.get('cache_set_id')
        if cache_set_id is not None:
            cache_set = CacheSet.objects.get(id=cache_set_id)
            if cache_set.get_node() != node:
                raise CacheSet.DoesNotExist()
            cache_set.delete()

    def create_partition(self, params):
        """Create a partition."""
        node = self.get_object(params)
        disk_obj = BlockDevice.objects.get(id=params['block_id'], node=node)
        form = AddPartitionForm(
            disk_obj, {
                'size': params['partition_size'],
            })
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            form.save()

    def create_cache_set(self, params):
        """Create a cache set."""
        node = self.get_object(params)
        block_id = params.get('block_id')
        partition_id = params.get('partition_id')

        data = {}
        if partition_id is not None:
            data["cache_partition"] = partition_id
        elif block_id is not None:
            data["cache_device"] = block_id
        else:
            raise HandlerError(
                "Either block_id or partition_id is required.")

        form = CreateCacheSetForm(node=node, data=data)
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            form.save()

    def create_bcache(self, params):
        """Create a bcache."""
        node = self.get_object(params)
        block_id = params.get('block_id')
        partition_id = params.get('partition_id')

        data = {
            "name": params["name"],
            "cache_set": params["cache_set"],
            "cache_mode": params["cache_mode"],
        }

        if partition_id is not None:
            data["backing_partition"] = partition_id
        elif block_id is not None:
            data["backing_device"] = block_id
        else:
            raise HandlerError(
                "Either block_id or partition_id is required.")

        form = CreateBcacheForm(node=node, data=data)
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            bcache = form.save()

        if 'fstype' in params:
            self.update_blockdevice_filesystem(
                node, bcache.virtual_device.id,
                params.get("fstype"), params.get("mount_point"))

    def create_raid(self, params):
        """Create a RAID."""
        node = self.get_object(params)
        form = CreateRaidForm(node=node, data=params)
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            raid = form.save()

        if 'fstype' in params:
            self.update_blockdevice_filesystem(
                node, raid.virtual_device.id,
                params.get("fstype"), params.get("mount_point"))

    def action(self, params):
        """Perform the action on the object."""
        obj = self.get_object(params)
        action_name = params.get("action")
        actions = compile_node_actions(obj, self.user)
        action = actions.get(action_name)
        if action is None:
            raise NodeActionError(
                "%s action is not available for this node." % action_name)
        extra_params = params.get("extra", {})
        return action.execute(**extra_params)

    def _create_link_on_interface(self, interface, params):
        """Create a link on a new interface."""
        mode = params.get("mode", None)
        subnet_id = params.get("subnet", None)
        if mode is not None:
            if mode != INTERFACE_LINK_TYPE.LINK_UP:
                link_form = InterfaceLinkForm(instance=interface, data=params)
                if link_form.is_valid():
                    link_form.save()
                else:
                    raise ValidationError(link_form.errors)
            elif subnet_id is not None:
                link_ip = interface.ip_addresses.get(
                    alloc_type=IPADDRESS_TYPE.STICKY, ip__isnull=True)
                link_ip.subnet = Subnet.objects.get(id=subnet_id)
                link_ip.save()

    def create_vlan(self, params):
        """Create VLAN interface."""
        # Only admin users can perform create.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        node = self.get_object(params)
        params['parents'] = [params.pop('parent')]
        form = VLANInterfaceForm(node=node, data=params)
        if form.is_valid():
            interface = form.save()
            self._create_link_on_interface(interface, params)
        else:
            raise ValidationError(form.errors)

    def create_bond(self, params):
        """Create bond interface."""
        # Only admin users can perform create.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        node = self.get_object(params)
        form = BondInterfaceForm(node=node, data=params)
        if form.is_valid():
            interface = form.save()
            self._create_link_on_interface(interface, params)
        else:
            raise ValidationError(form.errors)

    def update_interface(self, params):
        """Update the interface."""
        # Only admin users can perform update.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        node = self.get_object(params)
        interface = Interface.objects.get(node=node, id=params["interface_id"])
        interface_form = InterfaceForm.get_interface_form(interface.type)
        form = interface_form(instance=interface, data=params)
        if form.is_valid():
            form.save()
        else:
            raise ValidationError(form.errors)

    def delete_interface(self, params):
        """Delete the interface."""
        # Only admin users can perform delete.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        node = self.get_object(params)
        interface = Interface.objects.get(node=node, id=params["interface_id"])
        interface.delete()

    def link_subnet(self, params):
        """Create or update the link."""
        # Only admin users can perform update.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        node = self.get_object(params)
        interface = Interface.objects.get(node=node, id=params["interface_id"])
        subnet = None
        if "subnet" in params:
            subnet = Subnet.objects.get(id=params["subnet"])
        if "link_id" in params:
            # We are updating an already existing link.
            interface.update_link_by_id(
                params["link_id"], params["mode"], subnet,
                ip_address=params.get("ip_address", None))
        else:
            # We are creating a new link.
            interface.link_subnet(
                params["mode"], subnet,
                ip_address=params.get("ip_address", None))

    def unlink_subnet(self, params):
        """Delete the link."""
        # Only admin users can perform unlink.
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        node = self.get_object(params)
        interface = Interface.objects.get(node=node, id=params["interface_id"])
        interface.unlink_subnet_by_id(params["link_id"])

    @asynchronous
    @inlineCallbacks
    def check_power(self, params):
        """Check the power state of the node."""

        # XXX: This is largely the same function as
        # update_power_state_of_node.

        @transactional
        def get_node_cluster_and_power_info():
            obj = self.get_object(params)
            if obj.installable:
                node_info = obj.system_id, obj.hostname
                nodegroup_info = obj.nodegroup.cluster_name, obj.nodegroup.uuid
                try:
                    power_info = obj.get_effective_power_info()
                except UnknownPowerType:
                    return node_info, nodegroup_info, None
                else:
                    return node_info, nodegroup_info, power_info
            else:
                raise HandlerError(
                    "%s: Unable to query power state; not an "
                    "installable node" % obj.hostname)

        @transactional
        def update_power_state(state):
            obj = self.get_object(params)
            obj.update_power_state(state)

        # Grab info about the node, its cluster, and its power parameters from
        # the database. If it can't be queried we can return early, but first
        # update the node's power state with what we know we don't know.
        node_info, cluster_info, power_info = (
            yield deferToDatabase(get_node_cluster_and_power_info))
        if power_info is None or not power_info.can_be_queried:
            yield deferToDatabase(update_power_state, "unknown")
            returnValue("unknown")

        # Get a client to talk to the node's cluster. If we're not connected
        # we can return early, albeit with an exception.
        cluster_name, cluster_uuid = cluster_info
        try:
            client = yield getClientFor(cluster_uuid)
        except NoConnectionsAvailable:
            maaslog.error(
                "Unable to get RPC connection for cluster '%s' (%s)",
                cluster_name, cluster_uuid)
            raise HandlerError("Unable to connect to cluster controller")

        # Query the power state via the node's cluster.
        node_id, node_hostname = node_info
        try:
            response = yield deferWithTimeout(
                POWER_QUERY_TIMEOUT, client, PowerQuery, system_id=node_id,
                hostname=node_hostname, power_type=power_info.power_type,
                context=power_info.power_parameters)
        except CancelledError:
            # We got fed up waiting. The query may later discover the node's
            # power state but by then we won't be paying attention.
            maaslog.error("%s: Timed-out querying power.", node_hostname)
            state = "error"
        except PowerActionFail:
            # We discard the reason. That will have been logged elsewhere.
            # Here we're signalling something very simple back to the user.
            state = "error"
        except NotImplementedError:
            # The power driver has declared that it doesn't after all know how
            # to query the power for this node, so "unknown" seems appropriate.
            state = "unknown"
        else:
            state = response["state"]

        yield deferToDatabase(update_power_state, state)
        returnValue(state)