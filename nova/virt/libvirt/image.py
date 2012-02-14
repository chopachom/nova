# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011, Grid Dynamics
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc
import logging
import os
from xml.etree import ElementTree
from eventlet.green import time
from nova import utils, exception
from nova.flags import FLAGS
from nova.virt import disk, images

LOG = logging.getLogger('nova.virt.libvirt.image')


def select_driver():
    """selects image driver by current local_images_type flag value
    :rtype: :class:`nova.virt.libvirt.ImageDriver`
    :return: image driver"""

    if FLAGS.local_images_type == 'raw':
        driver = RawImageDriver
    elif FLAGS.local_images_type == 'qcow':
        driver = QcowImageDriver
    elif FLAGS.local_images_type == 'lvm':
        driver = LvmImageDriver
    elif FLAGS.local_images_type == 'legacy':
        if FLAGS.use_cow_images:
            driver = QcowImageDriver
        else:
            driver = RawImageDriver
    else:
        raise RuntimeError("No driver found for: %s" % FLAGS.local_images_type)
    return driver


class ImageDriver(object):

    __metaclass__ = abc.ABCMeta

    @classmethod
    @abc.abstractmethod
    def create_image(cls, instance_name, image_name=None, suffix=None):
        """Create image for VM.
        :type instance_name: string
        :param instance_name: name of the instance, that will own this image
        :type image_name: string
        :param image_name: name of the image
        :type suffix: string
        :param suffix: suffix
        :rtype: :class:`nova.virt.libvirt.image.Image`
        :return: Image object"""
        raise NotImplementedError('This method should '
                                  'be implemented '
                                  'in subclasses')

    @classmethod
    @abc.abstractmethod
    def list_images(cls, virt_domain):
        """List images for domain
        :type virt_domain: :class:`libvirt.virDomain`
        :param virt_domain: virDomain object
        :rtype: list
        """
        raise NotImplementedError('This method should '
                                  'be implemented '
                                  'in subclasses')

    @classmethod
    @abc.abstractmethod
    def image_info(cls, instance_name, image_name=None, suffix=None):
        """Libvirt image info
        :type instance_name: string
        :param instance_name: name of instance
        :type image_name: string
        :param image_name: name of image
        :type suffix: string
        :param suffix: suffix
        :rtype: dict"""
        raise NotImplementedError('This method should '
                                  'be implemented '
                                  'in subclasses')


class LvmImageDriver(ImageDriver):

    @classmethod
    def create_image(cls, instance_name, image_name=None, suffix=None):
        lv_name = cls._lv_name(instance_name, image_name, suffix)
        return LvmImage(FLAGS.lvm_volume_group, lv_name)

    @classmethod
    def _lv_name(cls, instance_name, image_name=None, suffix=None):
        lv_name = instance_name
        if image_name:
            lv_name += '-' + image_name
        if suffix:
            lv_name += '-' + suffix
        return lv_name

    @classmethod
    def _image_path(cls, vg, lv):
        return os.path.join('/dev', vg, lv)

    @classmethod
    def _list_disks(cls, virt_domain):
        xml_description = virt_domain.XMLDesc(0)
        domain = ElementTree.fromstring(xml_description)
        elements = domain.findall('devices/disk/source')
        return [element.get('dev') for element in elements]

    @classmethod
    def list_images(cls, virt_domain):
        images = []
        paths = cls._list_disks(virt_domain)
        LOG.info('Disks used by domain: %s' % paths)
        for lv_path in cls._list_disks(virt_domain):
            if lv_path is not None:
                image_name = os.path.basename(lv_path)
                image = cls.create_image(image_name)
                images.append(image)
        return images

    @classmethod
    def image_info(cls, instance_name, image_name=None, suffix=None):
        lv_name = cls._lv_name(instance_name, image_name, suffix)
        return {
            'device_type': 'block',
            'source_type': 'dev',
            'driver_type': 'raw',
            'disk': cls._image_path(FLAGS.lvm_volume_group, lv_name)
        }


class _FileImageDriver(ImageDriver):
    __metaclass__ = abc.ABCMeta

    @classmethod
    def _image_path(cls, instance_name, image_name, suffix):
        image_path = os.path.join(FLAGS.instances_path, instance_name, image_name)
        if suffix:
            image_path += suffix
        return image_path

    @classmethod
    def _list_disks(cls, virt_domain):
        xml_description = virt_domain.XMLDesc(0)
        domain = ElementTree.fromstring(xml_description)
        elements = domain.findall('devices/disk/source')
        return [element.get('file') for element in elements]


class RawImageDriver(_FileImageDriver):
    @classmethod
    def create_image(cls, instance_name, image_name, suffix=None):
        image_path = cls._image_path(instance_name, image_name, suffix)
        return RawImage(image_path)

    @classmethod
    def list_images(cls, virt_domain):
        return [RawImage(path) for path in cls._list_disks(virt_domain)]

    @classmethod
    def image_info(cls, instance_name, image_name=None, suffix=None):
        return {
            'device_type': 'file',
            'source_type': 'file',
            'driver_type': 'raw',
            'disk': cls._image_path(instance_name, image_name, suffix)
        }


class QcowImageDriver(_FileImageDriver):
    @classmethod
    def create_image(cls, instance_name, image_name, suffix=None):
        image_path = cls._image_path(instance_name, image_name, suffix)
        return QcowImage(image_path)

    @classmethod
    def list_images(cls, virt_domain):
        return [QcowImage(path) for path in cls._list_disks(virt_domain)]

    @classmethod
    def image_info(cls, instance_name, image_name=None, suffix=None):
        return {
            'device_type': 'file',
            'source_type': 'file',
            'driver_type': 'qcow2',
            'disk': cls._image_path(instance_name, image_name, suffix)
        }


class Image(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self):
        self._mounted = None

    def exists(self):
        """Check that image is created in filesystem"""
        return os.path.exists(path=self.path())

    def _assert_image_not_larger(self, base, size):
        """
        Asserts that given base image isn't larger than resulting image
        :param base: base image's path
        :param size: target size in bytes, can be None
        """
        # do not perform assertion if size wasn't given
        if not size:
            return
        base_image_size = images.virtual_size(base)
        LOG.debug(_("image_size_bytes=%(base_image_size)d, "
                    "allowed_size_bytes=%(size)d") % locals())
        if base_image_size > size:
            LOG.info(_("Image size %(base_image_size)d exceeded instance_type "
                       "allowed size %(size)d") % locals())
            raise exception.ImageTooLarge()

    @abc.abstractmethod
    def make_snapshot(self, virt_domain, snapshot_name, force_live_snapshot):
        """Create snapshot of a image
        :type virt_domain: :class:`libvirt.virDomain`
        :param virt_domain: libvirt domain (instance) which owns this image
        :type snapshot_name: string
        :param snapshot_name: snapshot name
        :type force_live_snapshot: bool
        :param force_live_snapshot: always try to perform snapshot on running VM
        :return: snapshot object
        """
        raise NotImplementedError('This method should '
                                  'be implemented '
                                  'in subclasses')

    @abc.abstractmethod
    def create_from_raw(self, base, size=None):
        """Create image from base image.
        :type base: string
        :param base: base image path
        :type size: int
        :param size: the size of resulting image in bytes
        """
        raise NotImplementedError('This method should '
                                  'be implemented '
                                  'in subclasses')

    @abc.abstractmethod
    def path(self):
        """Get image path
        :rtype: string
        :return: path where image will be in filesystem"""
        raise NotImplementedError('This method should '
                                  'be implemented '
                                  'in subclasses')

    @abc.abstractmethod
    def delete(self):
        """Delete image"""
        raise NotImplementedError('This method should '
                                  'be implemented '
                                  'in subclasses')


class _FileImage(Image):

    __metaclass__ = abc.ABCMeta

    def delete(self):
        try:
            os.remove(self.path())
        except OSError, e:
            LOG.error("Error during image delete: %s" % e)


class RawImage(_FileImage):

    def __init__(self, image_path):
        super(RawImage, self).__init__()
        self.image_path = image_path

    def create_from_raw(self, base, size=None):
        self._assert_image_not_larger(base, size)
        utils.execute('cp', base, self.image_path)
        if size:
            disk.extend(self.image_path, size)

    def path(self):
        return self.image_path

    def make_snapshot(self, virt_domain, snapshot_name, force_live_snapshot):
        return RawSnapshot(virt_domain, snapshot_name, self.path())
        

class QcowImage(_FileImage):

    def __init__(self, image_path):
        super(QcowImage, self).__init__()
        self.image_path = image_path

    def create_from_raw(self, base, size=None):
        self._assert_image_not_larger(base, size)
        utils.execute('qemu-img', 'create', '-f', 'qcow2', '-o',
            'cluster_size=2M,backing_file=%s' % base,
            self.path())
        if size:
            disk.extend(self.image_path, size)

    def path(self):
        return self.image_path

    def make_snapshot(self, virt_domain, snapshot_name, force_live_snapshot):
        return QcowSnapshot(virt_domain, snapshot_name, self.path())

class LvmImage(Image):

    def __init__(self, vg, lv):
        super(LvmImage, self).__init__()
        self.vg = vg
        self.lv = lv
        self._path = os.path.join('/dev', vg, lv)

    def create_from_raw(self, base, size=None):
        """
            Creating volume from raw image.
        """
        self._assert_image_not_larger(base, size)
        if not size:
            size = images.virtual_size(base)
        target = self.path()

        LOG.info(_("lvm volume %s with size %db: creating"),
            (self.lv, size))
        self.__try_execute('lvcreate', '-L', '%db' % size, '-n',
            self.lv, self.vg, run_as_root=True)

        LOG.info(_("disk %s converting to lvm volume %s"), (base, self.lv))
        utils.execute('qemu-img', 'convert', base, '-O',
                      'raw', target, run_as_root=True)
        utils.execute('e2fsck', '-fp', target,
                      run_as_root=True, check_exit_code=False)
        utils.execute('resize2fs', target,
                      run_as_root=True, check_exit_code=False)

    def make_snapshot(self, virt_domain, snapshot_name, force_live_snapshot):
        size = images.virtual_size(self._path)
        return LvmSnapshot(virt_domain, self.vg,
                           snapshot_name, size,
                           self.path(), force_live_snapshot)

    def delete(self):
        """Deletes a logical volume."""
        volume = self.path()
        LOG.info(_("lvm volume %s: deleting"), volume)
        if self.__volume_not_present(volume):
            # If the volume isn't present, then don't attempt to delete
            return True

        out, err = utils.execute('lvdisplay', '--noheading',
            '-C', '-o', 'Attr',
            volume,
            run_as_root=True)
        if out:
            out = out.strip()
            if ('o' in out) or ('O' in out):
                utils.execute('dmsetup','remove','-c',volume)
        self.__delete_image(volume)

    def path(self):
        return self._path

    @classmethod
    def __try_execute(cls, *command, **kwargs):
        tries = 0
        while True:
            try:
                utils.execute(*command, **kwargs)
                return True
            except exception.ProcessExecutionError:
                tries += 1
                if tries >= 3:
                    raise
                time.sleep(tries ** 2)

    @classmethod
    def __delete_image(cls, volume):
        """Deletes a logical volume."""
        cls.__try_execute('lvremove', '-f', volume, run_as_root=True)

    @classmethod
    def __volume_not_present(cls, volume):
        try:
            utils.execute('lvdisplay', volume, run_as_root=True)
        except Exception:
            # If the volume isn't present
            return True
        return False


class Snapshot(object):

    __metaclass__ = abc.ABCMeta

    def __init__(self, virt_domain, snapshot_name):
        """Initialize snapshot object by virDomain and name
        :type virt_domain: :class:`libvirt.virDomain`
        :param virt_domain: virDomain object of instance that owns this snapshot
        """
        self._virt_domain = virt_domain
        self._snapshot_name = snapshot_name

    def __enter__(self):
        return self.create()

    def __exit__(self, exc_type, exc_value, traceback):
        self.delete()

    @abc.abstractmethod
    def convert_to_raw(self, destination):
        """Convert snapshot to raw format
        :type destination: string
        :param destination: path where raw snapshot will be stored
        """
        raise NotImplementedError('This method should '
                                  'be implemented '
                                  'in subclasses')

    @abc.abstractmethod
    def create(self):
        """Create snapshot"""
        raise NotImplementedError('This method should '
                                  'be implemented '
                                  'in subclasses')

    @abc.abstractmethod
    def delete(self):
        """Delete snapshot"""
        raise NotImplementedError('This method should '
                                  'be implemented '
                                  'in subclasses')


class _LibvirtSnapshot(Snapshot):

    __metaclass__ = abc.ABCMeta
    
    def __init__(self, virt_domain, snapshot_name, source_path):
        super(_LibvirtSnapshot, self).__init__(virt_domain, snapshot_name)
        self._source_path = source_path

    def create(self):
        snapshot_description = """
        <domainsnapshot>
            <name>%s</name>
        </domainsnapshot>
        """ % self._snapshot_name
        self._snapshot_ptr = self._virt_domain.snapshotCreateXML(snapshot_description, 0)
        return self

    def _convert_to_raw(self, format, destination):
        qemu_img_cmd = ('qemu-img',
                        'convert',
                        '-f',
                        format,
                        '-O',
                        'raw',
                        '-s',
                        os.path.basename(destination),
                        self._source_path,
                        destination)
        utils.execute(*qemu_img_cmd, run_as_root=True)

    def delete(self):
        self._snapshot_ptr.delete(0)


class QcowSnapshot(_LibvirtSnapshot):
    def __init__(self, virt_domain, snapshot_name, source_path):
        super(QcowSnapshot, self).__init__(virt_domain, snapshot_name, source_path)

    def convert_to_raw(self, destination):
        self._convert_to_raw('qcow2', destination)


class RawSnapshot(_LibvirtSnapshot):
    def __init__(self, virt_domain, snapshot_name, source_path):
        super(RawSnapshot, self).__init__(virt_domain, snapshot_name, source_path)

    def convert_to_raw(self, destination):
        self._convert_to_raw('raw', destination)
    

class LvmSnapshot(Snapshot):

    def __init__(self, virt_domain, volume_group,
                 snapshot_name, snapshot_size,
                 source_path, force_live_snapshot):
        super(LvmSnapshot, self).__init__(virt_domain, snapshot_name)
        self._snapshot_path = os.path.join('/dev', volume_group, snapshot_name)
        self._snapshot_size = snapshot_size
        self._source_path = source_path
        self._force_live_snapshot = force_live_snapshot

    def create(self):
        if not self._force_live_snapshot and self._virt_domain.isActive():
            raise RuntimeError("VM must be suspended before doing LVM snapshot")
        #TODO(bfilippov): Try to do that with libvirt storage pool
        utils.execute('lvcreate','-L%db'%self._snapshot_size, '-s', '-n',
                      self._snapshot_name, self._source_path,
                      run_as_root=True)
        return self

    def convert_to_raw(self, destination):
        utils.execute('dd', 'if=%s' % self._snapshot_path,
                      'of=%s' % destination, 'bs=1M', run_as_root=True)

    def delete(self):
        utils.execute('lvremove', '-f', self._snapshot_path, run_as_root=True)
