# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for process helpers."""

__all__ = []

import os
import random
from textwrap import dedent

from maastesting.factory import factory
from maastesting.testcase import MAASTestCase
from provisioningserver.utils.fs import atomic_write
from provisioningserver.utils.ps import (
    get_running_pids_with_command,
    is_pid_in_container,
    running_in_container,
)


NOT_IN_CONTAINER = dedent("""\
    11:freezer:/
    10:perf_event:/
    9:cpuset:/
    8:net_cls,net_prio:/init.scope
    7:devices:/init.scope
    6:blkio:/init.scope
    5:memory:/init.scope
    4:cpu,cpuacct:/init.scope
    3:pids:/init.scope
    2:hugetlb:/
    1:name=systemd:/init.scope
    """)

IN_DOCKER_CONTAINER = dedent("""\
    11:freezer:/system.slice/docker-8467.scope
    10:perf_event:/
    9:cpuset:/system.slice/docker-8467.scope
    8:net_cls,net_prio:/init.scope
    7:devices:/init.scope/system.slice/docker-8467.scope
    6:blkio:/system.slice/docker-8467.scope
    5:memory:/system.slice/docker-8467.scope
    4:cpu,cpuacct:/system.slice/docker-8467.scope
    3:pids:/system.slice/docker-8467.scopeatomic_write
    2:hugetlb:/
    1:name=systemd:/system.slice/docker-8467.scope
    """)

IN_LXC_CONTAINER = dedent("""\
    11:freezer:/lxc/maas
    10:perf_event:/lxc/maas
    9:cpuset:/lxc/maas
    8:net_cls,net_prio:/lxc/maas
    7:devices:/lxc/maas/init.scope
    6:blkio:/lxc/maas
    5:memory:/lxc/maas
    4:cpu,cpuacct:/lxc/maas
    3:pids:/lxc/maas/init.scope
    2:hugetlb:/lxc/maas
    1:name=systemd:/lxc/maas/init.scope
    """)


class TestIsPIDInContainer(MAASTestCase):

    scenarios = (
        ("not_in_container", {
            "result": False,
            "cgroup": NOT_IN_CONTAINER,
        }),
        ("in_docker_container", {
            "result": True,
            "cgroup": IN_DOCKER_CONTAINER,
        }),
        ("in_lxc_container", {
            "result": True,
            "cgroup": IN_LXC_CONTAINER,
        }),
    )

    def test__result(self):
        proc_path = self.make_dir()
        pid = random.randint(1, 1000)
        pid_path = os.path.join(proc_path, str(pid))
        os.mkdir(pid_path)
        atomic_write(
            self.cgroup.encode("ascii"),
            os.path.join(pid_path, "cgroup"))
        self.assertEqual(
            self.result, is_pid_in_container(pid, proc_path=proc_path))


class TestRunningInContainer(MAASTestCase):

    scenarios = (
        ("not_in_container", {
            "result": False,
            "cgroup": NOT_IN_CONTAINER,
        }),
        ("in_docker_container", {
            "result": True,
            "cgroup": IN_DOCKER_CONTAINER,
        }),
        ("in_lxc_container", {
            "result": True,
            "cgroup": IN_LXC_CONTAINER,
        }),
    )

    def test__result(self):
        proc_path = self.make_dir()
        pid_path = os.path.join(proc_path, "1")
        os.mkdir(pid_path)
        atomic_write(
            self.cgroup.encode("ascii"),
            os.path.join(pid_path, "cgroup"))
        self.assertEqual(
            self.result, running_in_container(proc_path=proc_path))


class TestGetRunningPIDsWithCommand(MAASTestCase):

    def make_process(self, proc_path, pid, in_container=False, command=None):
        cgroup = NOT_IN_CONTAINER
        if in_container:
            cgroup = random.choice([IN_DOCKER_CONTAINER, IN_LXC_CONTAINER])
        pid_path = os.path.join(proc_path, str(pid))
        os.mkdir(pid_path)
        atomic_write(
            cgroup.encode("ascii"),
            os.path.join(pid_path, "cgroup"))
        if command is not None:
            atomic_write(
                command.encode("ascii"),
                os.path.join(pid_path, "comm"))

    def make_init_process(self, proc_path, in_container=False):
        self.make_process(proc_path, 1, in_container=in_container)

    def test_returns_processes_running_on_host_not_container(self):
        proc_path = self.make_dir()
        self.make_init_process(proc_path)
        command = factory.make_name("command")
        pids_running_command = set(
            random.randint(2, 999)
            for _ in range(3)
        )
        for pid in pids_running_command:
            self.make_process(proc_path, pid, command=command)
        pids_not_running_command = set(
            random.randint(1000, 1999)
            for _ in range(3)
        )
        for pid in pids_not_running_command:
            self.make_process(
                proc_path, pid, command=factory.make_name("command"))
        pids_running_command_in_container = set(
            random.randint(2000, 2999)
            for _ in range(3)
        )
        for pid in pids_running_command_in_container:
            self.make_process(
                proc_path, pid,
                in_container=True, command=command)
        self.assertItemsEqual(
            pids_running_command,
            get_running_pids_with_command(command, proc_path=proc_path))

    def test_returns_processes_when_running_in_container(self):
        proc_path = self.make_dir()
        self.make_init_process(proc_path, in_container=True)
        command = factory.make_name("command")
        pids_running_command = set(
            random.randint(2, 999)
            for _ in range(3)
        )
        for pid in pids_running_command:
            self.make_process(
                proc_path, pid, in_container=True, command=command)
        pids_not_running_command = set(
            random.randint(1000, 1999)
            for _ in range(3)
        )
        for pid in pids_not_running_command:
            self.make_process(
                proc_path, pid,
                in_container=True, command=factory.make_name("command"))
        self.assertItemsEqual(
            pids_running_command,
            get_running_pids_with_command(command, proc_path=proc_path))
