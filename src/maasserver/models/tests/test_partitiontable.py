# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `PartitionTable`."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = []

from django.core.exceptions import ValidationError
from maasserver.enum import PARTITION_TABLE_TYPE
from maasserver.models.blockdevice import MIN_BLOCK_DEVICE_SIZE
from maasserver.models.partition import MAX_PARTITION_SIZE_FOR_MBR
from maasserver.models.partitiontable import PARTITION_TABLE_EXTRA_SPACE
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.converters import round_size_to_nearest_block


class TestPartitionTable(MAASServerTestCase):
    """Tests for the `PartitionTable` model."""

    def test_get_node_returns_block_device_node(self):
        partition_table = factory.make_PartitionTable()
        self.assertEquals(
            partition_table.block_device.node, partition_table.get_node())

    def test_get_size_returns_block_device_size_minus_initial_offset(self):
        partition_table = factory.make_PartitionTable()
        self.assertEquals(
            partition_table.block_device.size - PARTITION_TABLE_EXTRA_SPACE,
            partition_table.get_size())

    def test_get_block_size_returns_block_device_block_size(self):
        partition_table = factory.make_PartitionTable()
        self.assertEquals(
            partition_table.block_device.block_size,
            partition_table.get_block_size())

    def test_add_misaligned_partition(self):
        """Tests whether a partition size are adjusted according to
        device block size."""
        block_size = 4096
        device = factory.make_BlockDevice(
            size=MIN_BLOCK_DEVICE_SIZE * 2 + PARTITION_TABLE_EXTRA_SPACE,
            block_size=block_size)
        partition_table = factory.make_PartitionTable(block_device=device)
        partition = partition_table.add_partition(
            size=MIN_BLOCK_DEVICE_SIZE + 54)
        self.assertEqual(
            round_size_to_nearest_block(
                MIN_BLOCK_DEVICE_SIZE + 54, block_size),
            partition.size)

    def test_add_partition_no_size(self):
        """Tests whether a partition with no specified size stretches to the
        end of the device"""
        block_size = 4096
        device = factory.make_BlockDevice(
            size=MIN_BLOCK_DEVICE_SIZE * 2 + PARTITION_TABLE_EXTRA_SPACE,
            block_size=block_size)
        partition_table = factory.make_PartitionTable(block_device=device)
        partition = partition_table.add_partition()
        self.assertEqual(
            partition.size, MIN_BLOCK_DEVICE_SIZE * 2)

    def test_add_partition_no_size_sets_mbr_max(self):
        block_size = 4096
        device = factory.make_BlockDevice(
            size=3 * (1024 ** 4),
            block_size=block_size)
        partition_table = factory.make_PartitionTable(
            table_type=PARTITION_TABLE_TYPE.MBR, block_device=device)
        partition = partition_table.add_partition()
        number_of_blocks = MAX_PARTITION_SIZE_FOR_MBR / block_size
        self.assertEqual(
            partition.size, block_size * (number_of_blocks - 1))

    def test_add_second_partition_no_size(self):
        """Tests whether a second partition with no specified size starts from
        the end of the previous partition and stretches to the end of the
        device."""
        block_size = 4096
        device = factory.make_BlockDevice(
            size=MIN_BLOCK_DEVICE_SIZE * 3 + PARTITION_TABLE_EXTRA_SPACE,
            block_size=block_size)
        partition_table = factory.make_PartitionTable(block_device=device)
        partition_table.add_partition(size=MIN_BLOCK_DEVICE_SIZE)
        partition = partition_table.add_partition()
        self.assertEqual(MIN_BLOCK_DEVICE_SIZE * 2, partition.size)

    def test_add_partition_to_full_device(self):
        """Tests whether we fail to add a partition to an already full device.
        """
        block_size = 4096
        device = factory.make_BlockDevice(
            size=MIN_BLOCK_DEVICE_SIZE * 3 + PARTITION_TABLE_EXTRA_SPACE,
            block_size=block_size)
        partition_table = factory.make_PartitionTable(block_device=device)
        partition_table.add_partition()
        self.assertRaises(
            ValidationError, partition_table.add_partition)

    def test_get_available_size(self):
        block_size = 4096
        device = factory.make_BlockDevice(
            size=MIN_BLOCK_DEVICE_SIZE * 3 + PARTITION_TABLE_EXTRA_SPACE,
            block_size=block_size)
        partition_table = factory.make_PartitionTable(block_device=device)
        partition_table.add_partition(size=MIN_BLOCK_DEVICE_SIZE)
        self.assertEquals(
            MIN_BLOCK_DEVICE_SIZE * 2, partition_table.get_available_size())

    def test_get_available_size_skips_partitions(self):
        block_size = 4096
        device = factory.make_BlockDevice(
            size=MIN_BLOCK_DEVICE_SIZE * 3 + PARTITION_TABLE_EXTRA_SPACE,
            block_size=block_size)
        partition_table = factory.make_PartitionTable(block_device=device)
        ignore_partitions = [
            partition_table.add_partition(size=MIN_BLOCK_DEVICE_SIZE)
            for _ in range(2)
            ]
        partition_table.add_partition(size=MIN_BLOCK_DEVICE_SIZE)
        self.assertEquals(
            MIN_BLOCK_DEVICE_SIZE * 2,
            partition_table.get_available_size(
                ignore_partitions=ignore_partitions))

    def test_save_sets_table_type_to_mbr_for_pxe_boot(self):
        node = factory.make_Node(bios_boot_method="pxe")
        boot_disk = factory.make_PhysicalBlockDevice(node=node)
        partition_table = factory.make_PartitionTable(block_device=boot_disk)
        self.assertEquals(PARTITION_TABLE_TYPE.MBR, partition_table.table_type)

    def test_save_sets_table_type_to_gpt_for_uefi_boot(self):
        node = factory.make_Node(bios_boot_method="uefi")
        boot_disk = factory.make_PhysicalBlockDevice(node=node)
        partition_table = factory.make_PartitionTable(block_device=boot_disk)
        self.assertEquals(PARTITION_TABLE_TYPE.GPT, partition_table.table_type)

    def test_save_sets_table_type_to_gpt_for_none_boot_disk(self):
        node = factory.make_Node(bios_boot_method="pxe")
        factory.make_PhysicalBlockDevice(node=node)
        other_disk = factory.make_PhysicalBlockDevice(node=node)
        partition_table = factory.make_PartitionTable(block_device=other_disk)
        self.assertEquals(PARTITION_TABLE_TYPE.GPT, partition_table.table_type)

    def test_save_force_mbr_on_boot_disk_pxe(self):
        node = factory.make_Node(bios_boot_method="pxe")
        boot_disk = factory.make_PhysicalBlockDevice(node=node)
        error = self.assertRaises(
            ValidationError,
            factory.make_PartitionTable,
            table_type=PARTITION_TABLE_TYPE.GPT, block_device=boot_disk)
        self.assertEquals({
            "table_type": [
                "Partition table on this node's boot disk must "
                "be using 'MBR'."],
            }, error.error_dict)

    def test_save_force_mbr_on_boot_disk_pxe_force_gpt_on_boot_disk_uefi(self):
        node = factory.make_Node(bios_boot_method="uefi")
        boot_disk = factory.make_PhysicalBlockDevice(node=node)
        error = self.assertRaises(
            ValidationError,
            factory.make_PartitionTable,
            table_type=PARTITION_TABLE_TYPE.MBR, block_device=boot_disk)
        self.assertEquals({
            "table_type": [
                "Partition table on this node's boot disk must "
                "be using 'GPT'."],
            }, error.error_dict)

    def test_save_no_force_on_none_boot_disk(self):
        node = factory.make_Node(bios_boot_method="uefi")
        factory.make_PhysicalBlockDevice(node=node)
        other_disk = factory.make_PhysicalBlockDevice(node=node)
        # No error should be raised.
        factory.make_PartitionTable(
            table_type=PARTITION_TABLE_TYPE.MBR, block_device=other_disk)

    def test_clean_no_partition_table_on_logical_volume(self):
        node = factory.make_Node()
        virtual_device = factory.make_VirtualBlockDevice(node=node)
        error = self.assertRaises(
            ValidationError,
            factory.make_PartitionTable, block_device=virtual_device)
        self.assertEquals({
            "block_device": [
                "Cannot create a partition table on a logical volume."],
            }, error.error_dict)