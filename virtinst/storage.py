#
# Copyright 2008, 2013, 2015 Red Hat, Inc.
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

import os
import threading

import libvirt

from . import generatename
from . import progress
from .logger import log
from .xmlbuilder import XMLBuilder, XMLChildProperty, XMLProperty


_DEFAULT_DEV_TARGET = "/dev"
_DEFAULT_LVM_TARGET_BASE = "/dev/"
_DEFAULT_SCSI_TARGET = "/dev/disk/by-path"
_DEFAULT_MPATH_TARGET = "/dev/mapper"


class _StoragePermissions(XMLBuilder):
    XML_NAME = "permissions"
    _XML_PROP_ORDER = ["mode", "owner", "group", "label"]

    mode = XMLProperty("./mode")
    owner = XMLProperty("./owner")
    group = XMLProperty("./group")
    label = XMLProperty("./label")


class _StorageObject(XMLBuilder):
    """
    Base class for building any libvirt storage object.

    Meaningless to directly instantiate.
    """

    ##############
    # Properties #
    ##############

    name = XMLProperty("./name")
    permissions = XMLChildProperty(_StoragePermissions,
                                   relative_xpath="./target",
                                   is_single=True)


def _preferred_default_pool_path(conn):
    path = "/var/lib/libvirt/images"
    if conn.is_session_uri():
        path = os.path.expanduser("~/.local/share/libvirt/images")
    return path


def _lookup_poolxml_by_path(conn, path):
    for poolxml in conn.fetch_all_pools():
        xml_path = poolxml.target_path
        if xml_path is not None and os.path.abspath(xml_path) == path:
            return poolxml
    return None


def _lookup_default_pool(conn):
    """
    Helper to lookup the default pool. It will return one of
    * The pool named 'default'
    * If that doesn't exist, the pool pointing to the default path
    * Otherwise None
    """
    name = "default"
    path = _preferred_default_pool_path(conn)

    poolxml = None
    for trypool in conn.fetch_all_pools():
        if trypool.name == name:
            poolxml = trypool
            break
    else:
        poolxml = _lookup_poolxml_by_path(conn, path)

    if poolxml:
        log.debug("Found default pool name=%s target=%s",
                poolxml.name, poolxml.target_path)
    return poolxml


class _EnumerateSource(XMLBuilder):
    XML_NAME = "source"


class _EnumerateSources(XMLBuilder):
    XML_NAME = "sources"
    sources = XMLChildProperty(_EnumerateSource)


class _Host(XMLBuilder):
    _XML_PROP_ORDER = ["name", "port"]
    XML_NAME = "host"

    name = XMLProperty("./@name")
    port = XMLProperty("./@port", is_int=True)


class StoragePool(_StorageObject):
    """
    Base class for building and installing libvirt storage pool xml
    """
    TYPE_DIR     = "dir"
    TYPE_FS      = "fs"
    TYPE_NETFS   = "netfs"
    TYPE_LOGICAL = "logical"
    TYPE_DISK    = "disk"
    TYPE_ISCSI   = "iscsi"
    TYPE_SCSI    = "scsi"
    TYPE_MPATH   = "mpath"
    TYPE_GLUSTER = "gluster"
    TYPE_RBD     = "rbd"
    TYPE_SHEEPDOG = "sheepdog"
    TYPE_ZFS     = "zfs"

    @staticmethod
    def pool_list_from_sources(conn, pool_type, host=None):
        """
        Return a list of StoragePool instances built from libvirt's pool
        source enumeration (if supported).

        :param conn: Libvirt connection
        :param name: Name for the new pool
        :param pool_type: Pool type string from I{Types}
        :param host: Option host string to poll for sources
        """
        if host:
            source_xml = "<source><host name='%s'/></source>" % host
        else:
            source_xml = "<source/>"

        try:
            xml = conn.findStoragePoolSources(pool_type, source_xml, 0)
        except Exception as e:
            if conn.support.is_error_nosupport(e):
                return []
            raise

        ret = []
        sources = _EnumerateSources(conn, xml)
        for source in sources.sources:
            source_xml = source.get_xml()

            pool_xml = "<pool>\n%s\n</pool>" % source_xml
            parseobj = StoragePool(conn, parsexml=pool_xml)
            parseobj.type = pool_type

            obj = StoragePool(conn)
            obj.type = pool_type
            obj.source_path = parseobj.source_path
            for h in parseobj.hosts:
                parseobj.remove_child(h)
                obj.add_child(h)
            obj.source_name = parseobj.source_name
            obj.format = parseobj.format

            ret.append(obj)
        return ret

    @staticmethod
    def build_default_pool(conn, build=True):
        """
        Attempt to lookup the 'default' pool, but if it doesn't exist,
        create it
        """
        poolxml = _lookup_default_pool(conn)
        if poolxml:
            return poolxml
        if not build:
            return None

        try:
            name = "default"
            path = _preferred_default_pool_path(conn)
            log.debug("Attempting to build default pool with target '%s'",
                          path)
            defpool = StoragePool(conn)
            defpool.type = defpool.TYPE_DIR
            defpool.name = name
            defpool.target_path = path
            defpool.install(build=True, create=True, autostart=True)
            return defpool
        except Exception as e:
            raise RuntimeError(
                _("Couldn't create default storage pool '%s': %s") %
                (path, str(e)))

    @staticmethod
    def lookup_pool_by_path(conn, path):
        """
        Return the first pool with matching matching target path.
        return the first we find, active or inactive. This iterates over
        all pools and dumps their xml, so it is NOT quick.

        :returns: virStoragePool object if found, None otherwise
        """
        poolxml = _lookup_poolxml_by_path(conn, path)
        if not poolxml:
            return None
        return conn.storagePoolLookupByName(poolxml.name)

    @staticmethod
    def find_free_name(conn, basename, **kwargs):
        """
        Finds a name similar (or equal) to passed 'basename' that is not
        in use by another pool. Extra params are passed to generate_name
        """
        def cb(name):
            for pool in conn.fetch_all_pools():
                if pool.name == name:
                    return True
            return False
        return generatename.generate_name(basename, cb, **kwargs)

    @staticmethod
    def ensure_pool_is_running(pool_object, refresh=False):
        """
        If the passed vmmStoragePool isn't running, start it.

        :param pool_object: vmmStoragePool to check/start
        :param refresh: If True, run refresh() as well
        """
        if pool_object.info()[0] != libvirt.VIR_STORAGE_POOL_RUNNING:
            log.debug("starting pool=%s", pool_object.name())
            pool_object.create(0)
        if refresh:
            log.debug("refreshing pool=%s", pool_object.name())
            pool_object.refresh(0)


    ######################
    # Validation helpers #
    ######################

    @staticmethod
    def validate_name(conn, name):
        XMLBuilder.validate_generic_name(_("Storage object"), name)

        try:
            conn.storagePoolLookupByName(name)
        except libvirt.libvirtError:
            return
        raise ValueError(_("Name '%s' already in use by another pool." %
                            name))

    def default_target_path(self):
        if not self.supports_property("target_path"):
            return None
        if (self.type == self.TYPE_DIR or
            self.type == self.TYPE_NETFS or
            self.type == self.TYPE_FS):
            return os.path.join(
                    _preferred_default_pool_path(self.conn), self.name)
        if self.type == self.TYPE_LOGICAL:
            name = self.name
            if self.source_name:
                name = self.source_name
            return _DEFAULT_LVM_TARGET_BASE + name
        if self.type == self.TYPE_DISK:
            return _DEFAULT_DEV_TARGET
        if self.type == self.TYPE_ISCSI or self.type == self.TYPE_SCSI:
            return _DEFAULT_SCSI_TARGET
        if self.type == self.TYPE_MPATH:
            return _DEFAULT_MPATH_TARGET
        raise RuntimeError("No default target_path for type=%s" % self.type)

    def _type_to_source_prop(self):
        if (self.type == self.TYPE_NETFS or
            self.type == self.TYPE_GLUSTER):
            return "_source_dir"
        elif self.type == self.TYPE_SCSI:
            return "_source_adapter"
        else:
            return "_source_device"

    def _get_source(self):
        return getattr(self, self._type_to_source_prop())
    def _set_source(self, val):
        return setattr(self, self._type_to_source_prop(), val)
    source_path = property(_get_source, _set_source)

    def default_source_name(self):
        srcname = None

        if not self.supports_property("source_name"):
            srcname = None
        elif self.type == StoragePool.TYPE_NETFS:
            srcname = self.name
        elif self.type == StoragePool.TYPE_RBD:
            srcname = "rbd"
        elif self.type == StoragePool.TYPE_GLUSTER:
            srcname = "gv0"
        elif ("target_path" in self._propstore and
                self.target_path and
                self.target_path.startswith(_DEFAULT_LVM_TARGET_BASE)):
            # If there is a target path, parse it for an expected VG
            # location, and pull the name from there
            vg = self.target_path[len(_DEFAULT_LVM_TARGET_BASE):]
            srcname = vg.split("/", 1)[0]

        return srcname


    ##############
    # Properties #
    ##############

    XML_NAME = "pool"
    _XML_PROP_ORDER = ["name", "type", "uuid",
                       "capacity", "allocation", "available",
                       "format", "hosts",
                       "_source_dir", "_source_adapter", "_source_device",
                       "source_name", "target_path",
                       "permissions",
                       "auth_type", "auth_username", "auth_secret_uuid"]


    _source_dir = XMLProperty("./source/dir/@path")
    _source_adapter = XMLProperty("./source/adapter/@name")
    _source_device = XMLProperty("./source/device/@path")

    type = XMLProperty("./@type")
    uuid = XMLProperty("./uuid")

    capacity = XMLProperty("./capacity", is_int=True)
    allocation = XMLProperty("./allocation", is_int=True)
    available = XMLProperty("./available", is_int=True)

    format = XMLProperty("./source/format/@type")
    iqn = XMLProperty("./source/initiator/iqn/@name")
    source_name = XMLProperty("./source/name")

    auth_type = XMLProperty("./source/auth/@type")
    auth_username = XMLProperty("./source/auth/@username")
    auth_secret_uuid = XMLProperty("./source/auth/secret/@uuid")

    target_path = XMLProperty("./target/path")

    hosts = XMLChildProperty(_Host, relative_xpath="./source")


    ######################
    # Public API helpers #
    ######################

    def supports_property(self, propname):
        users = {
            "source_path": [self.TYPE_FS, self.TYPE_NETFS, self.TYPE_LOGICAL,
                            self.TYPE_DISK, self.TYPE_ISCSI, self.TYPE_SCSI,
                            self.TYPE_GLUSTER],
            "source_name": [self.TYPE_LOGICAL, self.TYPE_GLUSTER,
                            self.TYPE_RBD, self.TYPE_SHEEPDOG, self.TYPE_ZFS],
            "hosts": [self.TYPE_NETFS, self.TYPE_ISCSI, self.TYPE_GLUSTER,
                     self.TYPE_RBD, self.TYPE_SHEEPDOG],
            "format": [self.TYPE_FS, self.TYPE_NETFS, self.TYPE_DISK],
            "iqn": [self.TYPE_ISCSI],
            "target_path": [self.TYPE_DIR, self.TYPE_FS, self.TYPE_NETFS,
                             self.TYPE_LOGICAL, self.TYPE_DISK, self.TYPE_ISCSI,
                             self.TYPE_SCSI, self.TYPE_MPATH]
        }

        if users.get(propname):
            return self.type in users[propname]
        return hasattr(self, propname)

    def get_disk_type(self):
        if (self.type == StoragePool.TYPE_DISK or
            self.type == StoragePool.TYPE_LOGICAL or
            self.type == StoragePool.TYPE_SCSI or
            self.type == StoragePool.TYPE_MPATH or
            self.type == StoragePool.TYPE_ZFS):
            return StorageVolume.TYPE_BLOCK
        if (self.type == StoragePool.TYPE_GLUSTER or
            self.type == StoragePool.TYPE_RBD or
            self.type == StoragePool.TYPE_ISCSI or
            self.type == StoragePool.TYPE_SHEEPDOG):
            return StorageVolume.TYPE_NETWORK
        return StorageVolume.TYPE_FILE


    ##################
    # Build routines #
    ##################

    def validate(self):
        self.validate_name(self.conn, self.name)

        if not self.target_path:
            self.target_path = self.default_target_path()
        if not self.source_name:
            self.source_name = self.default_source_name()
        if not self.format and self.supports_property("format"):
            self.format = "auto"

        if self.supports_property("hosts") and not self.hosts:
            raise RuntimeError(_("Hostname is required"))
        if (self.supports_property("source_path") and
            self.type != self.TYPE_LOGICAL and
            not self.source_path):
            raise RuntimeError(_("Source path is required"))

        if (self.type == self.TYPE_DISK and self.format == "auto"):
            # There is no explicit "auto" type for disk pools, but leaving out
            # the format type seems to do the job for existing formatted disks
            self.format = None

    def install(self, meter=None, create=False, build=False, autostart=False):
        """
        Install storage pool xml.
        """
        if (self.type == self.TYPE_LOGICAL and
            build and not self.source_path):
            raise ValueError(_("Must explicitly specify source path if "
                               "building pool"))
        if (self.type == self.TYPE_DISK and
            build and self.format == "auto"):
            raise ValueError(_("Must explicitly specify disk format if "
                               "formatting disk device."))

        xml = self.get_xml()
        log.debug("Creating storage pool '%s' with xml:\n%s",
                      self.name, xml)

        meter = progress.ensure_meter(meter)

        try:
            pool = self.conn.storagePoolDefineXML(xml, 0)
        except Exception as e:
            raise RuntimeError(_("Could not define storage pool: %s") % str(e))

        errmsg = None
        if build:
            try:
                pool.build(libvirt.VIR_STORAGE_POOL_BUILD_NEW)
            except Exception as e:
                errmsg = _("Could not build storage pool: %s") % str(e)

        if create and not errmsg:
            try:
                pool.create(0)
            except Exception as e:
                errmsg = _("Could not start storage pool: %s") % str(e)

        if autostart and not errmsg:
            try:
                pool.setAutostart(True)
            except Exception as e:
                errmsg = _("Could not set pool autostart flag: %s") % str(e)

        if errmsg:
            # Try and clean up the leftover pool
            try:
                pool.undefine()
            except Exception as e:
                log.debug("Error cleaning up pool after failure: %s",
                              str(e))
            raise RuntimeError(errmsg)

        self.conn.cache_new_pool(pool)

        return pool



class StorageVolume(_StorageObject):
    """
    Base class for building and installing libvirt storage volume xml
    """
    @staticmethod
    def get_file_extension_for_format(fmt):
        if not fmt:
            return ""
        if fmt == "raw":
            return ".img"
        return "." + fmt

    @staticmethod
    def find_free_name(conn, pool_object, basename, collideguest=None, **kwargs):
        """
        Finds a name similar (or equal) to passed 'basename' that is not
        in use by another volume. Extra params are passed to generate_name

        :param collideguest: Guest object. If specified, also check to
        ensure we don't collide with any disk paths there
        """
        collidelist = []
        if collideguest:
            pooltarget = None
            poolname = pool_object.name()
            for poolxml in conn.fetch_all_pools():
                if poolxml.name == poolname:
                    pooltarget = poolxml.target_path
                    break

            for disk in collideguest.devices.disk:
                if (pooltarget and disk.path and
                    os.path.dirname(disk.path) == pooltarget):
                    collidelist.append(os.path.basename(disk.path))

        def cb(tryname):
            if tryname in collidelist:
                return True
            return generatename.check_libvirt_collision(
                pool_object.storageVolLookupByName, tryname)

        StoragePool.ensure_pool_is_running(pool_object, refresh=True)
        return generatename.generate_name(basename, cb, **kwargs)

    TYPE_FILE = getattr(libvirt, "VIR_STORAGE_VOL_FILE", 0)
    TYPE_BLOCK = getattr(libvirt, "VIR_STORAGE_VOL_BLOCK", 1)
    TYPE_DIR = getattr(libvirt, "VIR_STORAGE_VOL_DIR", 2)
    TYPE_NETWORK = getattr(libvirt, "VIR_STORAGE_VOL_NETWORK", 3)
    TYPE_NETDIR = getattr(libvirt, "VIR_STORAGE_VOL_NETDIR", 4)


    def __init__(self, *args, **kwargs):
        _StorageObject.__init__(self, *args, **kwargs)

        self._input_vol = None
        self._pool = None
        self._pool_xml = None
        self._reflink = False

        self._install_finished = threading.Event()


    ######################
    # Non XML properties #
    ######################

    def _get_pool(self):
        return self._pool
    def _set_pool(self, newpool):
        StoragePool.ensure_pool_is_running(newpool)
        self._pool = newpool
        self._pool_xml = StoragePool(self.conn,
            parsexml=self._pool.XMLDesc(0))
    pool = property(_get_pool, _set_pool)

    def _get_input_vol(self):
        return self._input_vol
    def _set_input_vol(self, vol):
        if vol is None:
            self._input_vol = None
            return

        if not isinstance(vol, libvirt.virStorageVol):
            raise ValueError(_("input_vol must be a virStorageVol"))

        self._input_vol = vol
    input_vol = property(_get_input_vol, _set_input_vol)

    def _get_reflink(self):
        return self._reflink
    def _set_reflink(self, reflink):
        self._reflink = reflink
    reflink = property(_get_reflink, _set_reflink)

    def sync_input_vol(self, only_format=False):
        # Pull parameters from input vol into this class
        parsevol = StorageVolume(self.conn,
                                 parsexml=self._input_vol.XMLDesc(0))

        self.format = parsevol.format
        self.capacity = parsevol.capacity
        self.allocation = parsevol.allocation
        if only_format:
            return
        self.pool = self._input_vol.storagePoolLookupByVolume()


    ##########################
    # XML validation helpers #
    ##########################

    @staticmethod
    def validate_name(pool, name):
        XMLBuilder.validate_generic_name(_("Storage object"), name)

        try:
            pool.storageVolLookupByName(name)
        except libvirt.libvirtError:
            return
        raise ValueError(_("Name '%s' already in use by another volume." %
                            name))

    def _get_vol_type(self):
        if self.type:
            if self.type == "file":
                return self.TYPE_FILE
            elif self.type == "block":
                return self.TYPE_BLOCK
            elif self.type == "dir":
                return self.TYPE_DIR
            elif self.type == "network":
                return self.TYPE_NETWORK
        return self._pool_xml.get_disk_type()
    file_type = property(_get_vol_type)


    ##################
    # XML properties #
    ##################

    XML_NAME = "volume"
    _XML_PROP_ORDER = ["name", "key", "capacity", "allocation", "format",
                       "target_path", "permissions"]

    type = XMLProperty("./@type")
    key = XMLProperty("./key")
    capacity = XMLProperty("./capacity", is_int=True)
    allocation = XMLProperty("./allocation", is_int=True)
    format = XMLProperty("./target/format/@type")
    target_path = XMLProperty("./target/path")
    backing_store = XMLProperty("./backingStore/path")
    backing_format = XMLProperty("./backingStore/format/@type")
    lazy_refcounts = XMLProperty(
            "./target/features/lazy_refcounts", is_bool=True)


    def _detect_backing_store_format(self):
        log.debug("Attempting to detect format for backing_store=%s",
                self.backing_store)
        from . import diskbackend
        vol, pool = diskbackend.manage_path(self.conn, self.backing_store)

        if not vol:
            log.debug("Didn't find any volume for backing_store")
            return None

        # Only set backing format for volumes that support
        # the 'format' parameter as we know it, like qcow2 etc.
        volxml = StorageVolume(self.conn, vol.XMLDesc(0))
        volxml.pool = pool
        log.debug("Found backing store volume XML:\n%s",
                volxml.get_xml())

        if volxml.supports_property("format"):
            log.debug("Returning format=%s", volxml.format)
            return volxml.format

        log.debug("backing_store volume doesn't appear to have "
            "a file format we can specify, returning None")
        return None


    ######################
    # Public API helpers #
    ######################

    def _supports_format(self):
        if self.file_type == self.TYPE_FILE:
            return True
        if self._pool_xml.type == StoragePool.TYPE_GLUSTER:
            return True
        return False

    def supports_property(self, propname):
        if propname == "format":
            return self._supports_format()
        return hasattr(self, propname)


    ##################
    # Build routines #
    ##################

    def validate(self):
        self.validate_name(self.pool, self.name)

        if not self.format and self.file_type == self.TYPE_FILE:
            self.format = "raw"
        if self._prop_is_unset("lazy_refcounts") and self.format == "qcow2":
            self.lazy_refcounts = self.conn.support.conn_qcow2_lazy_refcounts()

        if self._pool_xml.type == StoragePool.TYPE_LOGICAL:
            if self.allocation != self.capacity:
                log.warning(_("Sparse logical volumes are not supported, "
                               "setting allocation equal to capacity"))
                self.allocation = self.capacity

        isfatal, errmsg = self.is_size_conflict()
        if isfatal:
            raise ValueError(errmsg)
        if errmsg:
            log.warning(errmsg)

    def install(self, meter=None):
        """
        Build and install storage volume from xml
        """
        if self.backing_store and not self.backing_format:
            self.backing_format = self._detect_backing_store_format()

        xml = self.get_xml()
        log.debug("Creating storage volume '%s' with xml:\n%s",
                      self.name, xml)

        t = threading.Thread(target=self._progress_thread,
                             name="Checking storage allocation",
                             args=(meter,))
        t.setDaemon(True)

        meter = progress.ensure_meter(meter)

        cloneflags = 0
        createflags = 0
        if (self.format == "qcow2" and
            not self.backing_store and
            not self.conn.is_really_test() and
            self.conn.support.pool_metadata_prealloc(self.pool)):
            createflags |= libvirt.VIR_STORAGE_VOL_CREATE_PREALLOC_METADATA
            if self.capacity == self.allocation:
                # For cloning, this flag will make libvirt+qemu-img preallocate
                # the new disk image
                cloneflags |= libvirt.VIR_STORAGE_VOL_CREATE_PREALLOC_METADATA

        if self.reflink:
            cloneflags |= getattr(libvirt,
                "VIR_STORAGE_VOL_CREATE_REFLINK", 1)

        try:
            self._install_finished.clear()
            t.start()
            meter.start(size=self.capacity,
                        text=_("Allocating '%s'") % self.name)

            if self.input_vol:
                vol = self.pool.createXMLFrom(xml, self.input_vol, cloneflags)
            else:
                log.debug("Using vol create flags=%s", createflags)
                vol = self.pool.createXML(xml, createflags)

            self._install_finished.set()
            t.join()
            meter.end(self.capacity)
            log.debug("Storage volume '%s' install complete.",
                          self.name)
            return vol
        except Exception as e:
            log.debug("Error creating storage volume", exc_info=True)
            raise RuntimeError("Couldn't create storage volume "
                               "'%s': '%s'" % (self.name, str(e)))

    def _progress_thread(self, meter):
        vol = None
        if not meter:
            return

        while True:
            try:
                if not vol:
                    vol = self.pool.storageVolLookupByName(self.name)
                vol.info()
                break
            except Exception:
                if self._install_finished.wait(.2):
                    break

        if vol is None:
            log.debug("Couldn't lookup storage volume in prog thread.")
            return

        while True:
            ignore, ignore, alloc = vol.info()
            meter.update(alloc)
            if self._install_finished.wait(1):
                break


    def is_size_conflict(self):
        """
        Report if requested size exceeds its pool's available amount

        :returns: 2 element tuple:
            1. True if collision is fatal, false otherwise
            2. String message if some collision was encountered.
        """
        if not self.pool:
            return (False, "")

        # pool info is [pool state, capacity, allocation, available]
        avail = self.pool.info()[3]
        if self.allocation > avail:
            return (True, _("There is not enough free space on the storage "
                            "pool to create the volume. "
                            "(%d M requested allocation > %d M available)") %
                            ((self.allocation // (1024 * 1024)),
                             (avail // (1024 * 1024))))
        elif self.capacity > avail:
            return (False, _("The requested volume capacity will exceed the "
                             "available pool space when the volume is fully "
                             "allocated. "
                             "(%d M requested capacity > %d M available)") %
                             ((self.capacity // (1024 * 1024)),
                              (avail // (1024 * 1024))))
        return (False, "")
