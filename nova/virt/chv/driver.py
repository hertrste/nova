"""
A module allowing to control Cloud Hypervisor virtual machines.
"""

import os
import psutil
import subprocess

import nova.compute.power_state as power_state
import os_resource_classes as orc

from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import fileutils
from oslo_utils import units
from oslo_utils import versionutils

from nova import conf
from nova import exception
from nova.compute import utils as compute_utils
from nova.objects import fields as obj_fields
from nova.virt import driver
from nova.virt.hardware import InstanceInfo

from nova.virt.libvirt import vif as libvirt_vif
from nova.virt.libvirt import utils as libvirt_utils

LOG = logging.getLogger(__name__)
CONF = conf.CONF

CHV_NAME = "cloud-hypervisor"
CH_REMOTE_NAME = "ch-remote"


class CHVNetworkInterface(object):
    """Represents a network interface that can be attached to a CHV VM."""

    def __init__(self, vif_driver, instance, network_info):
        """Constructor takes care of "plugging" in the virtual interface if
        required. For some interfaces, the interface still has to be connected
        to the bridge so networking works successfully.
        """
        self.vif_driver = vif_driver
        self.instance = instance
        self.network_info = network_info
        self.vif_driver.plug(instance, network_info)

    def name(self):
        """Returns the devices name, e.g. tap12345."""
        return self.network_info["devname"]

    def address(self):
        """Returns the devices MAC address."""
        return self.network_info["address"]

    def __del__(self):
        self.vif_driver.unplug(self.instance, self.network_info)


class CHVDisk:
    """Represents a disk that can be connected to a CHV instance."""

    def __init__(self, context, image_info, dst_path):
        """The constructor receives the metadata about the disk image and takes
        care of fetching the image data and placing it at some given path.
        """
        self.dst_path = dst_path

        libvirt_utils.fetch_image(
            context,
            dst_path,
            image_info["image_id"],
        )

    def path(self):
        return self.dst_path

    def __del__(self):
        if os.path.exists(self.dst_path):
            os.remove(self.dst_path)


class CHVInstance:
    """
    Represents an instance of a Cloud Hypervisor virtual machine.

    The CHVInstance uses RAII principles to allocate and cleanup all required
    resources to run the VM. Temporary files and sockets are created in an
    instance specific directory.
    """

    def __init__(self, instance):
        self.inst_path = libvirt_utils.get_instance_path(instance)
        fileutils.ensure_tree(self.inst_path)
        self.name = instance.name
        self.uuid = instance.uuid
        self.state = power_state.SHUTDOWN
        self.vcpus = instance.vcpus
        self.memory_mb = instance.memory_mb
        self.proc = None
        self.disks: list[CHVDisk] = []
        self.network_interfaces: list[CHVNetworkInterface] = []

    def start(self):
        if len(self.disks) > 1:
            raise exception.Invalid(
                f"Found {len(self.disks)} disks to attach, but currently max 1 are supported."
            )

        if len(self.network_interfaces) > 1:
            raise exception.Invalid(
                f"Found {len(self.network_interfaces)} network interfaces to attach, but currently max 1 are supported."
            )

        command = [
            "cloud-hypervisor",
            "--kernel",
            f"{CONF.state_path}/hypervisor-fw",
            "--cpus",
            f"boot={self.vcpus}",
            "--memory",
            f"size={self.memory_mb}M",
            "--serial",
            f"file={self.inst_path}/chv-serial",
            f"--api-socket={self.inst_path}/chv-socket",
        ]

        for iface in self.network_interfaces:
            command.append("--net")
            command.append(f"tap={iface.name()},mac={iface.address()}")

        for disk in self.disks:
            command.append("--disk")
            command.append(f"path={disk.path()}")

        LOG.info(f"Start Cloud Hypervisor: {command}")

        self.proc = subprocess.Popen(command, text=True)
        self.state = power_state.RUNNING

    def attach_disk(self, disk: CHVDisk):
        """Attach a disk to the CHVInstance.

        The disk can only be attached before the CHVInstance is initially
        started. Dynamic attaching disks at runtime is not yet supported.
        """
        if self.state != power_state.SHUTDOWN:
            LOG.error(
                "Trying to attach a disk to a running CHVInstance. "
                "Currently, disk attachments are only supported at instance creation."
            )
            return
        self.disks.append(disk)

    def get_console_log(self):
        """Returns the serial console log of the instance."""
        bytes = b""
        with open(f"{self.inst_path}/chv-serial", "rb") as f:
            bytes = f.read()

        output = bytes.decode(encoding="utf-8", errors="ignore")
        return output

    def _ch_remote(self, cmd: str):
        """
        Issues a command to the VM via the ch-remote executable.
        """
        api_socket_uri = self.inst_path + "/chv-socket"
        command = [CH_REMOTE_NAME, f"--api-socket={api_socket_uri}", cmd]
        subprocess.run(command, check=True)

    def power_on(self):
        self._ch_remote("boot")
        self.state = power_state.RUNNING

    def power_off(self):
        self._ch_remote("shutdown")
        self.state = power_state.SHUTDOWN

    def reboot(self):
        self._ch_remote("reboot")
        self.state = power_state.RUNNING

    def attach_network_interface(self, network_interface: CHVNetworkInterface):
        """Attach a network interface to the CHVInstance.

        The network interface can only be attached before the CHVInstance is
        initially started. Dynamic attaching interfaces at runtime is not yet
        supported.
        """
        if self.state != power_state.SHUTDOWN:
            LOG.error(
                "Trying to attach a network interface to a running CHVInstance. "
                "Currently, network attachments are only supported at instance creation."
            )
            return
        self.network_interfaces.append(network_interface)

    def __del__(self):
        """Destructor takes care of cleaning up the running Cloud Hypervisor
        process and any created sockets.
        """
        if self.proc:
            self.proc.kill()

        for f in [
            f"{self.inst_path}/chv-serial",
            f"{self.inst_path}/chv-socket",
        ]:
            if os.path.exists(f):
                os.remove(f)

    def __getitem__(self, key):
        return getattr(self, key)


class CHVDriver(driver.ComputeDriver):
    """Cloud Hypervisor implementation of ComputeDriver."""

    capabilities = {
        "supports_pcpus": False,
        "supports_remote_managed_ports": False,
        "supports_address_space_passthrough": False,
        "supports_address_space_emulated": False,
        "supports_stateless_firmware": False,
        # Image type support flags
        "supports_image_type_aki": False,
        "supports_image_type_ami": False,
        "supports_image_type_ari": False,
        "supports_image_type_iso": False,
        "supports_image_type_qcow2": True,
        "supports_image_type_raw": True,
        "supports_image_type_vdi": False,
        "supports_image_type_vhd": False,
        "supports_image_type_vhdx": False,
        "supports_image_type_vmdk": False,
        "supports_image_type_ploop": False,
    }

    def __get_chv_version(self):
        """Return a integer representation of the Cloud Hypervisor version."""
        res = subprocess.run([CHV_NAME, "--version"], capture_output=True, text=True)
        ver = res.stdout.split()[1][1:]
        return versionutils.convert_version_to_int(ver)

    def __init__(self, virtapi):
        super(CHVDriver, self).__init__(virtapi)

        # Dictionary of UUIDs to CHVInstance objects
        self.instances: dict[str, CHVInstance] = {}

        self.host_status_base = {
            "hypervisor_type": obj_fields.HVType.CHV,
            "hypervisor_version": self.__get_chv_version(),
            "hypervisor_hostname": self.get_available_nodes()[0],
            # The VMWare driver reports None for cpu_info as it has control over
            # multiple nodes potentially with different CPU architectures. We
            # might need to present something meaningful here.
            "cpu_info": None,
            "disk_available_least": 0,
            "supported_instances": [
                (
                    obj_fields.Architecture.X86_64,
                    obj_fields.HVType.CHV,
                    obj_fields.VMMode.HVM,
                )
            ],
            "numa_topology": None,
        }

        # Re-use the libvirt vif driver for plugging in virtual interfaces
        self.vif_driver = libvirt_vif.LibvirtGenericVIFDriver()

        LOG.info("The Cloud Hypervisor compute driver has been initialized.")

    def get_console_output(self, context, instance):
        """Return the serial console output of a given instance."""
        key = instance.uuid
        if key not in self.instances:
            raise exception.InstanceNotFound(
                f"Instance {instance.name}:{instance.uuid} not available"
            )

        return self.instances[key].get_console_log()

    def init_host(self, host):
        LOG.debug(f"init_host host={host}")
        pass

    def get_available_nodes(self, refresh=False):
        """Return list of managed nodes.

        The Cloud Hypervisor driver only supports a single node (the current
        host we are running on). Thus, only the host name is returned.

        This is in contrast to other drivers that may have control over multiple
        nodes themselves.
        """
        import socket

        return [socket.gethostname()]

    def list_instances(self):
        """Returns a list of currently managed CHVInstances."""
        return [self.instances[uuid].name for uuid in self.instances.keys()]

    def list_instance_uuids(self):
        """Returns a list of UUIDs of currently managed CHVInstances."""
        return list(self.instances.keys())

    @staticmethod
    def _to_gb(x):
        return int(x / units.Gi)

    def get_available_resource(self, nodename):
        """Returns a representation of the current nodes resources, e.g. RAM,
        vCPUs, and disk space.
        """
        if nodename not in self.get_available_nodes():
            return {}

        used_cpus = sum([instance.vcpus for instance in self.instances.values()])
        used_memory_mb = sum(
            [instance.memory_mb for instance in self.instances.values()]
        )

        disk_info = libvirt_utils.get_fs_info(CONF.instances_path)

        host_status = self.host_status_base.copy()
        host_status["host_hostname"] = nodename
        host_status["host_name_label"] = nodename
        host_status["hypervisor_hostname"] = nodename
        host_status["disk_total"] = max(
            CHVDriver._to_gb(disk_info["total"]), 1
        )  # Zero is not allowed as a value
        host_status["local_gb"] = CHVDriver._to_gb(disk_info["free"])
        host_status["local_gb_used"] = CHVDriver._to_gb(disk_info["used"])
        # Retrieve the currently available virtual memory for a lack of a better
        # value to present here.
        host_status["memory_mb"] = int(
            psutil.virtual_memory().available / (1024 * 1024)
        )
        host_status["memory_mb_used"] = used_memory_mb
        host_status["vcpus"] = os.cpu_count()
        host_status["vcpus_used"] = used_cpus

        return host_status

    def update_provider_tree(self, provider_tree, nodename, allocations=None):
        """Update a ProviderTree object with current resource provider,
        inventory information and CPU traits.
        """
        resources = self.get_available_resource(nodename)

        inventory = provider_tree.data(nodename).inventory
        allocation_ratios = self._get_allocation_ratios(inventory)

        inventory = {
            orc.VCPU: {
                "total": resources["vcpus"],
                "min_unit": 1,
                "max_unit": resources["vcpus"],
                "step_size": 1,
                "allocation_ratio": allocation_ratios[orc.VCPU],
                "reserved": CONF.reserved_host_cpus,
            },
            orc.MEMORY_MB: {
                "total": resources["memory_mb"],
                "min_unit": 1,
                "max_unit": resources["memory_mb"],
                "step_size": 1,
                "allocation_ratio": allocation_ratios[orc.MEMORY_MB],
                "reserved": CONF.reserved_host_memory_mb,
            },
            orc.DISK_GB: {
                "total": resources["disk_total"],
                "min_unit": 1,
                "max_unit": resources["disk_total"],
                "step_size": 1,
                "allocation_ratio": allocation_ratios[orc.DISK_GB],
                "reserved": compute_utils.convert_mb_to_ceil_gb(
                    CONF.reserved_host_disk_mb
                ),
            },
        }

        provider_tree.update_inventory(nodename, inventory)

    def get_host_uptime(self):
        out, err = processutils.execute("env", "LANG=C", "uptime")
        return out

    def spawn(
        self,
        context,
        instance,
        image_meta,
        injected_files,
        admin_password,
        allocations,
        network_info=None,
        block_device_info=None,
        power_on=True,
        accel_info=None,
    ):
        """Spawn and start a CHVInstance.

        Handles the creation and attachment of network interfaces and disks.
        """
        uuid = instance.uuid
        chvInstance = CHVInstance(instance)

        disks = [
            CHVDisk(context, image_info, chvInstance.inst_path + f"/disk{idx}")
            for idx, image_info in enumerate(block_device_info["image"])
        ]

        network_ifaces = [
            CHVNetworkInterface(self.vif_driver, instance, info)
            for info in network_info
        ]

        for disk in disks:
            chvInstance.attach_disk(disk)

        for iface in network_ifaces:
            chvInstance.attach_network_interface(iface)

        if power_on:
            chvInstance.start()

        self.instances[uuid] = chvInstance

        LOG.info(f"Successfully spawned instance {instance.name}:{instance.uuid}")

    def get_info(self, instance, use_cache=True):
        """Returns the power state of the given instance."""
        LOG.debug(f"get_info: {instance}")
        return InstanceInfo(state=power_state.RUNNING)

    def reboot(
        self,
        context,
        instance,
        network_info,
        reboot_type,
        block_device_info=None,
        bad_volumes_callback=None,
        accel_info=None,
    ):
        """Reboot a virtual machine, given an instance reference."""
        key = instance.uuid
        if key not in self.instances:
            raise exception.InstanceNotFound(
                f"Instance {instance.name}:{instance.uuid} not available"
            )

        LOG.info(f"Rebooting instance {instance.name}")
        self.instances[key].reboot()

    def power_off(self, instance, timeout=0, retry_interval=0):
        key = instance.uuid
        if key not in self.instances:
            raise exception.InstanceNotFound(
                f"Instance {instance.name}:{instance.uuid} not available"
            )

        LOG.info(f"Power off instance {instance.name}")
        self.instances[key].power_off()

    def power_on(
        self, context, instance, network_info, block_device_info=None, accel_info=None
    ):
        key = instance.uuid
        if key not in self.instances:
            raise exception.InstanceNotFound(
                f"Instance {instance.name}:{instance.uuid} not available"
            )

        LOG.info(f"Power on instance {instance.name}")
        self.instances[key].power_on()

    def destroy(
        self,
        context,
        instance,
        network_info,
        block_device_info=None,
        destroy_disks=True,
        destroy_secrets=True,
    ):
        """Destroy the given instance.

        Cleans up all sockets and file created in the instances directory.
        """
        LOG.info(f"Destroy instance {instance.name}:{instance.uuid}")
        key = instance.uuid
        if key in self.instances:
            del self.instances[key]
        else:
            LOG.warning(f"Key {key} not in instances {self.instances}")
