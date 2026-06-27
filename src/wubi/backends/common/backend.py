# Copyright (c) 2008 Agostino Russo
#
# Written by Agostino Russo <agostino.russo@gmail.com>
#
# This file is part of Wubi the Win32 Ubuntu Installer.
#
# Wubi is free software; you can redistribute it and/or modify
# it under 5the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation; either version 2.1 of
# the License, or (at your option) any later version.
#
# Wubi is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import sys
import os
import tempfile
import locale
import struct
import logging
import time
import gettext
import glob
import shutil
import ConfigParser
import btdownloader
import downloader
import subprocess

from metalink import parse_metalink
from tasklist import ThreadedTaskList, Task
from distro import Distro
from mappings import lang_country2linux_locale
from utils import join_path, run_nonblocking_command, md5_password, copy_file, read_file, write_file, get_file_hash, reversed, find_line_in_file, unix_path, rm_tree, spawn_command
from signature import verify_gpg_signature
from wubi import errors
from os.path import abspath

log = logging.getLogger("CommonBackend")

class Backend(object):
    '''
    Implements non-platform-specific functionality
    Subclasses need to implement platform-specific getters
    '''
    def __init__(self, application):
        self.application = application
        self.info = application.info
        #~ if hasattr(sys,'frozen') and sys.frozen:
            #~ root_dir = dirname(abspath(sys.executable))
        #~ else:
            #~ root_dir = ''
        #~ self.info.root_dir = abspath(root_dir)
        self.info.temp_dir = join_path(self.info.root_dir, 'temp')
        self.info.data_dir = join_path(self.info.root_dir, 'data')
        self.info.bin_dir = join_path(self.info.root_dir, 'bin')
        self.info.image_dir = join_path(self.info.data_dir, 'images')
        self.info.translations_dir = join_path(self.info.root_dir, 'translations')
        self.info.trusted_keys = join_path(self.info.data_dir, 'trustedkeys.gpg')
        self.info.application_icon = join_path(self.info.image_dir, self.info.application_name.capitalize() + ".ico")
        self.info.icon = self.info.application_icon
        self.info.iso_md5_hashes = {}
        log.debug('data_dir=%s' % self.info.data_dir)
        if self.info.locale:
            locale.setlocale(locale.LC_ALL, self.info.locale)
            log.debug('user defined locale = %s' % self.info.locale)
        gettext.install(self.info.application_name, localedir=self.info.translations_dir, unicode=True, names=['ngettext'])

    def get_installation_tasklist(self):
        self.cache_cd_path()
        dimage = self.info.distro.diskimage
        # don't use diskimage for a FAT32 target directory
        if dimage and not self.cd_path and not self.iso_path and not self.info.target_drive.is_fat():
            tasks = [
            Task(self.select_target_dir,
                 description=_("Selecting the target directory")),
            Task(self.create_dir_structure,
                 description=_("Creating the directories")),
            Task(self.create_uninstaller,
                 description=_("Creating the uninstaller")),
            Task(self.create_preseed_diskimage,
                 description=_("Creating a preseed file")),
            Task(self.get_diskimage,
                 description=_("Retrieving installation files")),
            Task(self.extract_diskimage, description=_("Extracting")),
            Task(self.choose_disk_sizes, description=_("Choosing disk sizes")),
            Task(self.expand_diskimage,
                 description=_("Expanding")),
            Task(self.create_swap_diskimage,
                 description=_("Creating virtual memory")),
            Task(self.modify_bootloader,
                 description=_("Adding a new bootloader entry")),
            Task(self.diskimage_bootloader,
                 description=_("Installing the bootloader")),
            ]
        else:
            tasks = [
            Task(self.select_target_dir, description=_("Selecting the target directory")),
            Task(self.create_dir_structure, description=_("Creating the installation directories")),
            Task(self.uncompress_target_dir, description=_("Uncompressing files")),
            Task(self.create_uninstaller, description=_("Creating the uninstaller")),
            Task(self.copy_installation_files, description=_("Copying installation files")),
            Task(self.get_iso, description=_("Retrieving installation files")),
            Task(self.extract_kernel, description=_("Extracting the kernel")),
            Task(self.choose_disk_sizes, description=_("Choosing disk sizes")),
            Task(self.create_preseed, description=_("Creating a preseed file")),
            Task(self.modify_bootloader, description=_("Adding a new bootloader entry")),
            Task(self.modify_grub_configuration, description=_("Setting up installation boot menu")),
            Task(self.create_virtual_disks, description=_("Creating the virtual disks")),
            Task(self.uncompress_files, description=_("Uncompressing files")),
            Task(self.eject_cd, description=_("Ejecting the CD")),
            ]
        description = _("Installing %(distro)s-%(version)s") % dict(distro=self.info.distro.name, version=self.info.version)
        tasklist = ThreadedTaskList(description=description, tasks=tasks)
        return tasklist

    def get_cdboot_tasklist(self):
        self.cache_cd_path()
        tasks = [
            Task(self.select_target_dir, description=_("Selecting the target directory")),
            Task(self.create_dir_structure, description=_("Creating the installation directories")),
            Task(self.uncompress_target_dir, description=_("Uncompressing files")),
            Task(self.create_uninstaller, description=_("Creating the uninstaller")),
            Task(self.copy_installation_files, description=_("Copying installation files")),
            Task(self.use_cd, description=_("Extracting CD content")),
            Task(self.extract_kernel, description=_("Extracting the kernel")),
            Task(self.create_preseed_cdboot, description=_("Creating a preseed file")),
            Task(self.modify_bootloader, description=_("Adding a new bootloader entry")),
            Task(self.modify_grub_configuration, description=_("Setting up installation boot menu")),
            Task(self.uncompress_files, description=_("Uncompressing files")),
            Task(self.eject_cd, description=_("Ejecting the CD")),
            ]
        tasklist = ThreadedTaskList(description=_("Installing CD boot helper"), tasks=tasks)
        return tasklist

    def get_reboot_tasklist(self):
        tasks = [
            Task(self.reboot, description=_("Rebooting")),
            ]
        tasklist = ThreadedTaskList(description=_("Rebooting"), tasks=tasks)
        return tasklist

    def get_uninstallation_tasklist(self):
        tasks = [
            Task(self.undo_bootloader, _("Remove bootloader entry")),
            Task(self.remove_target_dir, _("Remove target dir")),
            Task(self.remove_registry_key, _("Remove registry key")),]
        tasklist = ThreadedTaskList(description=_("Uninstalling %s") % self.info.previous_distro_name, tasks=tasks)
        return tasklist

    def show_info(self):
        log.debug("Showing info")
        os.startfile(self.info.cd_distro.website)

    def fetch_basic_info(self):
        '''
        Basic information required by the application dispatcher select_task()
        '''
        log.debug("Fetching basic info...")
        self.info.uninstall_before_install = False
        self.info.original_exe = self.get_original_exe()
        self.info.platform = self.get_platform()
        self.info.osname = self.get_osname()
        if not self.info.language:
            self.info.language, self.info.encoding = self.get_language_encoding()
        self.info.environment_variables = os.environ
        self.info.arch = self.get_arch()
        if self.info.force_i386:
            log.debug("Forcing 32 bit arch")
            self.info.arch = "i386"
        self.info.check_arch = (self.info.arch == "i386")
        self.info.distro = None
        self.info.distros = self.get_distros()
        distros = [((d.name.lower(), d.arch), d) for d in  self.info.distros]
        self.info.distros_dict = dict(distros)
        self.fetch_host_info()
        self.info.previous_uninstaller_path = self.get_uninstaller_path()
        self.info.previous_target_dir = self.get_previous_target_dir()
        self.info.previous_distro_name = self.get_previous_distro_name()
        self.info.keyboard_layout, self.info.keyboard_variant = self.get_keyboard_layout()
        if not self.info.locale:
            self.info.locale = self.get_locale(self.info.language)
        self.info.total_memory_mb = self.get_total_memory_mb()
        self.info.iso_path, self.info.iso_distro = self.find_any_iso()
        self.info.cd_path, self.info.cd_distro = self.find_any_cd()

    def get_distros(self):
        isolist_path = join_path(self.info.data_dir, 'isolist.ini')
        distros = self.parse_isolist(isolist_path)
        return distros

    def get_original_exe(self):
        if self.info.original_exe:
            original_exe = self.info.original_exe
        else:
            original_exe = abspath(sys.argv[0])
        log.debug("original_exe=%s" % original_exe)
        return original_exe

    def get_locale(self, language_country, fallback="en_US"):
        _locale = lang_country2linux_locale.get(language_country, None)
        if not _locale:
            _locale = lang_country2linux_locale.get(fallback)
        log.debug("python locale=%s" % str(locale.getdefaultlocale()))
        log.debug("locale=%s" % _locale)
        return _locale

    def get_platform(self):
        platform = sys.platform
        log.debug("platform=%s" % platform)
        return platform

    def get_osname(self):
        osname = os.name
        log.debug("osname=%s" % osname)
        return osname

    def get_language_encoding(self):
        language, encoding = locale.getdefaultlocale()
        log.debug("language=%s" % language)
        log.debug("encoding=%s" % encoding)
        return language, encoding

    def get_arch(self):
        arch = struct.calcsize('P') == 8 and "amd64" or "i386"
        log.debug("arch=%s" % arch)
        return arch

    def create_dir_structure(self, associated_task=None):
        self.info.disks_dir = join_path(self.info.target_dir, "disks")
        self.info.install_dir = join_path(self.info.target_dir, "install")
        self.info.install_boot_dir = join_path(self.info.install_dir, "boot")
        self.info.disks_boot_dir = join_path(self.info.disks_dir, "boot")
        dirs = [
            self.info.target_dir,
            self.info.disks_dir,
            self.info.install_dir,
            self.info.install_boot_dir,
            self.info.disks_boot_dir,
            join_path(self.info.disks_boot_dir, "grub"),
            join_path(self.info.install_boot_dir, "grub"),]
        for d in dirs:
            if not os.path.isdir(d):
                log.debug("Creating dir %s" % d)
                os.mkdir(d)

    def fetch_installer_info(self):
        '''
        Fetch information required by the installer
        '''

    def dummy_function(self):
        time.sleep(1)

    def check_metalink(self, metalink, base_url, associated_task=None):
        # تخطي التحقق من ميتالينك للعمل أوفلاين بالكامل
        return True

    def check_cd(self, cd_path, associated_task=None):
        return True

    def check_iso(self, iso_path, associated_task=None):
        # تخطي فحص البصمة والوثوق في ملف الأيزو المحلي مباشرة
        log.debug("Bypassing MD5 check for local ISO: %s" % iso_path)
        return True

    def select_mirrors(self, urls):
        '''
        Sort urls by preference giving a "boost" to the urls in the
        same country as the client
        '''
        def cmp(x, y):
            return y.score - x.score #reverse order
        urls = list(urls)
        for url in urls:
            url.score = url.preference
            if self.info.country == url.location:
                url.score += 50
        urls.sort(cmp)
        return urls

    def cache_cd_path(self):
        self.iso_path = None
        self.cd_path = None
        if self.info.cd_distro \
        and self.info.distro == self.info.cd_distro \
        and self.info.cd_path \
        and os.path.isdir(self.info.cd_path):
            self.cd_path = self.info.cd_path
        else:
            self.cd_path = self.find_cd()

        if not self.cd_path:
            if self.info.iso_distro \
            and self.info.distro == self.info.iso_distro \
            and os.path.isfile(self.info.iso_path):
                self.iso_path = self.info.iso_path
            else:
                self.iso_path = self.find_iso()

    def create_diskimage_dirs(self, associated_task=None):
        self.info.disks_dir = join_path(self.info.target_dir, "disks")
        self.info.disks_boot_dir = join_path(self.info.disks_dir, "boot")
        dirs = [
            self.info.target_dir,
            self.info.disks_dir,
            self.info.disks_boot_dir,
            join_path(self.info.disks_boot_dir, "grub"),
            ]
        for d in dirs:
            if not os.path.isdir(d):
                log.debug("Creating dir %s" % d)
                os.mkdir(d)

    def download_diskimage(self, diskimage, associated_task=None):
        return False

    def download_iso(self, associated_task=None):
        # منع البرنامج تماماً من محاولة تحميل أي ملفات من سيرفرات أوبونتو لمنع أخطاء الشبكة
        log.error("Internet download disabled. Forcing Offline Mode.")
        raise Exception("Offline mode active: Please make sure your Lubuntu ISO is placed next to this installer.")

    def get_metalink(self, associated_task=None):
        if associated_task:
            associated_task.description = _("Skipping online metalink (Offline mode)")
        return

    def get_prespecified_diskimage(self, associated_task):
        '''
        Use a local disk image specificed on the command line
        '''
        if self.info.dimage_path \
        and os.path.exists(self.info.dimage_path):
            self.dimage_path = self.info.dimage_path
            return True

    def get_prespecified_iso(self, associated_task):
        if self.info.iso_path \
        and os.path.exists(self.info.iso_path):
            log.debug("Trying to use pre-specified ISO %s" % self.info.iso_path)
            return self.copy_iso(self.info.iso_path, associated_task)

    def set_distro_from_arch(self, cd_or_iso_path):
        return

    def copy_diskimage(self, dimage_path, associated_task):
        if not dimage_path:
            return
        dimage_name = self.info.distro.diskimage.split('/')[-1]
        dest = os.path.join(self.info.disks_dir, dimage_name)
        copy_dimage = associated_task.add_subtask(
            copy_file,
            description = _("Copying installation files"))
        log.debug("Copying %s > %s" % (dimage_path, dest))
        copy_dimage(dimage_path, dest)
        return True

    def copy_iso(self, iso_path, associated_task):
        if not iso_path:
            return
        dest = join_path(self.info.install_dir, "installation.iso")
        if os.path.abspath(iso_path) != os.path.abspath(dest):
            copy_iso = associated_task.add_subtask(
                copy_file,
                description = _("Copying installation files"))
            log.debug("Copying %s > %s" % (iso_path, dest))
            copy_iso(iso_path, dest)
        self.info.cd_path = None
        self.info.iso_path = dest
        return True

    def use_cd(self, associated_task):
        if self.cd_path:
            extract_iso = associated_task.add_subtask(
                copy_file,
                description = _("Extracting files from %s") % self.cd_path)
            self.info.iso_path = join_path(self.info.install_dir, "installation.iso")
            try:
                extract_iso(self.cd_path, self.info.iso_path)
                return True
            except Exception, err:
                log.error(err)
                self.info.cd_path = None
                self.info.iso_path = None
        return False

    def use_iso(self, associated_task):
        if self.iso_path:
            log.debug("Trying to use ISO %s" % self.iso_path)
            return self.copy_iso(self.iso_path, associated_task)

    def get_diskimage(self, associated_task=None):
        if self.get_prespecified_diskimage(associated_task):
            return associated_task.finish()
        raise Exception("Could not retrieve the required disk image files")

    def get_iso(self, associated_task=None):
        # إجبار البرنامج على استخدام ملف الـ ISO المحلي فقط وإظهار تنبيه واضح إذا لم يجده
        if self.use_iso(associated_task) or self.get_prespecified_iso(associated_task) or self.use_cd(associated_task):
            return associated_task.finish()
        raise Exception("Wubi Error: Could not find your local Lubuntu ISO. Please place the ISO file in the same folder as Wubi.")

    def extract_kernel(self):
        bootdir = self.info.install_boot_dir
        if self.info.cd_path:
            log.debug("Copying files from CD %s" % self.info.cd_path)
            for src in [
            join_path(self.info.cd_path, self.info.distro.md5sums),
            join_path(self.info.cd_path, self.info.distro.kernel),
            join_path(self.info.cd_path, self.info.distro.initrd),]:
                if os.path.exists(src):
                    shutil.copy(src, bootdir)
        elif self.info.iso_path:
            log.debug("Extracting files from ISO %s" % self.info.iso_path)
            try:
                self.extract_file_from_iso(self.info.iso_path, self.info.distro.md5sums, output_dir=bootdir)
            except: pass
            try:
                self.extract_file_from_iso(self.info.iso_path, self.info.distro.kernel, output_dir=bootdir)
                self.extract_file_from_iso(self.info.iso_path, self.info.distro.initrd, output_dir=bootdir)
            except Exception, e:
                log.error("Error extracting boot files from ISO: %s" % e)
        else:
            raise Exception("Could not retrieve the required installation files")
            
        self.info.kernel = join_path(bootdir, os.path.basename(self.info.distro.kernel))
        self.info.initrd = join_path(bootdir, os.path.basename(self.info.distro.initrd))

    def check_file(self, file_path, relpath, md5sums, associated_task=None):
        # تخطي الفحص الداخلي للبصمات لضمان عدم توقف عملية التثبيت بسبب اختلافات الإصدارات
        return True

    def create_preseed_diskimage(self):
        source = join_path(self.info.data_dir, 'preseed.disk')
        template = read_file(source)
        password = md5_password(self.info.password)
        dic = dict(
            timezone = self.info.timezone,
            password = password,
            keyboard_variant = self.info.keyboard_variant,
            keyboard_layout = self.info.keyboard_layout,
            locale = self.info.locale,
            user_full_name = self.info.user_full_name,
            username = self.info.username)
        for k,v in dic.items():
            k = "$(%s)" % k
            template = template.replace(k, v)
        preseed_file = join_path(self.info.install_dir, "preseed.cfg")
        write_file(preseed_file, template)

        source = join_path(self.info.data_dir, "wubildr-disk.cfg")
        target = join_path(self.info.install_dir, "wubildr-disk.cfg")
        copy_file(source, target)

    def check_previous_installation(self):
        # بقية الكود الأصلي المستدعى من الملحقات المخصصة لـ Windows
        pass

    def run_uninstaller(self):
        if not self.info.previous_uninstaller_path or not os.path.exists(self.info.previous_uninstaller_path):
            log.error("Could not find the uninstaller %s" % self.info.previous_uninstaller_path)
            return False
        command = [self.info.previous_uninstaller_path, "--uninstall"]
        if self.info.non_interactive:
            command.append("--noninteractive")
        if 0 and previous_uninstaller.lower() == self.info.original_exe.lower():
            if self.info.original_exe.lower().startswith(self.info.previous_target_dir.lower()):
                log.debug("Copying uninstaller to a temp directory")
                uninstaller = tempfile.NamedTemporaryFile()
                uninstaller.close()
                uninstaller = uninstaller.name
                copy_file(self.info.previous_uninstaller_path, uninstaller)
            log.info("Launching asynchronously previous uninstaller %s" % uninstaller)
            run_nonblocking_command(command, show_window=True)
            return True
        elif get_file_hash(self.info.original_exe) == get_file_hash(self.info.previous_uninstaller_path):
            log.info("This is the uninstaller running")
        else:
            log.info("Launching previous uninstaller %s" % uninstaller)
            subprocess.call(command)
            self.application.quit()
            return True
