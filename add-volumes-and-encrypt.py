# Copyright 2017 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#!/usr/bin/env python
import binascii
import guestfs
import os

# remove old generated drive
try:
    os.unlink("/tmp/overcloud-full-partitioned.qcow2")
except:
    pass

g = guestfs.GuestFS(python_return_dict=True)

# import old and new images
print("Creating new repartitioned image")
g.add_drive_opts("/tmp/overcloud-full.qcow2", format="qcow2", readonly=1)
g.disk_create("/tmp/overcloud-full-partitioned.qcow2", "qcow2", 10 * 1024 * 1024 * 1024) #10G
g.add_drive_opts("/tmp/overcloud-full-partitioned.qcow2", format="qcow2", readonly=0)
g.launch()

# create the partitions for new image
print("Creating the initial partitions")
g.part_init("/dev/sdb", "mbr")
g.part_add("/dev/sdb", "primary", 2048, 616448)
g.part_add("/dev/sdb", "primary", 616449, -1)

g.pvcreate("/dev/sdb2")
g.vgcreate("vg", ['/dev/sdb2', ])
g.lvcreate("var", "vg", 4400)
g.lvcreate("tmp", "vg", 500)
g.lvcreate("swap", "vg", 250)
g.lvcreate("home", "vg", 100)
g.lvcreate("root", "vg", 4000)
g.part_set_bootable("/dev/sdb", 1, True)

# encrypt home partition and write keys
print("Encrypting volume")
random_content = binascii.b2a_hex(os.urandom(1024))
g.luks_format('/dev/vg/home', random_content, 0)

# open the encrypted volume
volumes = ['/dev/vg/var', '/dev/vg/tmp', '/dev/vg/swap', '/dev/mapper/cryptedhome', '/dev/vg/root']

g.luks_open('/dev/vg/home', random_content, 'cryptedhome')
g.vgscan()
g.vg_activate_all(True)

# add filesystems to volumes
print("Adding filesystems")
ids = {}
keys = [ 'var', 'tmp', 'swap', 'home', 'root' ]
swap_volume = volumes[2]

count = 0
for volume in volumes:
    if count!=2:
        g.mkfs('ext4', volume)
        if keys[count] == 'home':
            ids['home'] = g.vfs_uuid('/dev/vg/home')
        else:
            ids[keys[count]] = g.vfs_uuid(volume)
    count +=1

# create filesystem on boot and swap
g.mkfs('ext4', '/dev/sdb1')
g.mkswap_opts(volumes[2])
ids['swap'] = g.vfs_uuid(volumes[2])

# mount drives and copy content
print("Start copying content")
g.mkmountpoint('/old')
g.mkmountpoint('/root')
g.mkmountpoint('/boot')
g.mkmountpoint('/home')
g.mkmountpoint('/var')
g.mount('/dev/sda', '/old')

g.mount('/dev/sdb1', '/boot')
g.mount(volumes[4], '/root')
g.mount(volumes[3], '/home')
g.mount(volumes[0], '/var')

# copy content to root
results = g.ls('/old/')
for result in results:
    if result not in ('boot', 'home', 'tmp', 'var'):
        print("Copying %s to root" % result)
        g.cp_a('/old/%s' % result, '/root/')

# copy extra content
folders_to_copy = ['boot', 'home', 'var']
for folder in folders_to_copy:
    results = g.ls('/old/%s/' % folder)
    for result in results:
        print("Copying %s to %s" % (result, folder))
        g.cp_a('/old/%s/%s' % (folder, result),
               '/%s/' % folder)

# write keyfile for encrypted volume
g.write('/root/root/home_keyfile', random_content)
g.chmod(0400, '/root/root/home_keyfile')

# generate mapper for encrypted home
mapper = """
home UUID={home_id} /root/home_keyfile
""".format(home_id=ids['home'])
g.write('/root/etc/crypttab', mapper)

# create /etc/fstab file
print("Generating fstab content")
fstab_content = """
UUID={boot_id} /boot ext4 defaults 1 2
UUID={root_id} / ext4 defaults 1 1
UUID={swap_id} none swap sw 0 0
UUID={tmp_id} /tmp ext4 defaults 1 2
UUID={var_id} /var ext4 defaults 1 2
/dev/mapper/home /home ext4 defaults 1 2
""".format(
    boot_id=g.vfs_uuid('/dev/sdb1'),
    root_id=ids['root'],
    swap_id=ids['swap'],
    tmp_id=ids['tmp'],
    home_id=ids['home'],
    var_id=ids['var'])

g.write('/root/etc/fstab', fstab_content)

# umount filesystems
g.umount('/root')
g.umount('/boot')
g.umount('/old')
g.umount('/var')
g.umount('/home')

# close encrypted volume
g.luks_close('/dev/mapper/cryptedhome')

# mount in the right directories to install bootloader
print("Installing bootloader")
g.mount(volumes[4], '/')
g.mkdir('/boot')
g.mkdir('/var')
g.mount('/dev/sdb1', '/boot')
g.mount(volumes[0], '/var')

# add rd.auto=1 on grub parameters
g.sh('sed  -i "s/.*GRUB_CMDLINE_LINUX.*/GRUB_CMDLINE_LINUX=\\"console=tty0 crashkernel=auto rd.auto=1\\"/" /etc/default/grub')

g.sh('grub2-install --target=i386-pc /dev/sdb')
g.sh('grub2-mkconfig -o /boot/grub2/grub.cfg')

# create dracut.conf file
dracut_content = """
add_dracutmodules+="lvm crypt"
"""
g.write('/etc/dracut.conf', dracut_content)

# update initramfs to include lvm and crypt
kernels = g.ls('/lib/modules')
for kernel in kernels:
    print("Updating dracut to include modules in kernel %s" % kernel)
    g.sh('dracut -f /boot/initramfs-%s.img %s --force' % (kernel, kernel))

# do a selinux relabel
g.selinux_relabel('/etc/selinux/targeted/contexts/files/file_contexts', '/', force=True)
g.selinux_relabel('/etc/selinux/targeted/contexts/files/file_contexts', '/var', force=True)


g.umount('/boot')
g.umount('/var')
g.umount('/')

# close images
print("Finishing image")
g.shutdown()
g.close()
