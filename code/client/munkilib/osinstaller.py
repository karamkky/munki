# encoding: utf-8
#
# Copyright 2017 Greg Neagle.
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
osinstaller.py

Created by Greg Neagle on 2017-03-29.

Support for using startosinstall to install macOS.
"""

# stdlib imports
import os
import signal
import subprocess
import time

# our imports
from . import FoundationPlist
from . import display
from . import dmgutils
from . import launchd
from . import munkilog
from . import munkistatus
from . import osutils
from . import pkgutils
from . import prefs
from . import processes


CHECKANDINSTALLATSTARTUPFLAG = \
           '/Users/Shared/.com.googlecode.munki.checkandinstallatstartup'


def find_install_macos_app(dir_path):
    '''Returns the path to the first Install macOS.app found the top level of
    dir_path, or None'''
    for item in osutils.listdir(dir_path):
        item_path = os.path.join(dir_path, item)
        startosinstall_path = os.path.join(
            item_path, 'Contents/Resources/startosinstall')
        if os.path.exists(startosinstall_path):
            return item_path
    # if we get here we didn't find one
    return None


def get_os_version(app_path):
    '''Returns the os version from the OS Installer app'''
    installinfo_plist = os.path.join(
        app_path, 'Contents/SharedSupport/InstallInfo.plist')
    if not os.path.isfile(installinfo_plist):
        # no Contents/SharedSupport/InstallInfo.plist
        return ''
    try:
        info = FoundationPlist.readPlist(installinfo_plist)
        return info['System Image Info']['version']
    except (FoundationPlist.FoundationPlistException,
            IOError, KeyError, AttributeError, TypeError):
        return ''


class StartOSInstallError(Exception):
    '''Exception to raise if starting the macOS install fails'''
    pass


class StartOSInstallRunner(object):
    '''Handles running startosinstall to set up and kick off an upgrade install
    of macOS'''
    def __init__(self, installer, finishing_tasks=None):
        self.installer = installer
        self.finishing_tasks = finishing_tasks
        self.dmg_mountpoint = None
        self.got_sigusr1 = False

    def sigusr1_handler(self, dummy_signum, dummy_frame):
        '''Signal handler for SIGUSR1 from startosinstall, which tells us it's
        done setting up the macOS install and is ready and waiting to reboot'''
        display.display_debug1('Got SIGUSR1 from startosinstall')
        self.got_sigusr1 = True
        # do stuff here: cleanup, record-keeping, notifications
        if self.finishing_tasks:
            self.finishing_tasks()
        # set Munki to run at boot after the OS upgrade is complete
        try:
            open(CHECKANDINSTALLATSTARTUPFLAG, 'w').close()
        except (OSError, IOError), err:
            display.display_error(
                'Could not set up Munki to run after OS upgrade is complete: '
                "%s", err)
        # then tell startosinstall it's OK to proceed with restart
        # can't use os.kill now that we wrap the call of startosinstall
        #os.kill(self.startosinstall_pid, signal.SIGUSR1)
        # so just target processes named 'startosinstall'
        subprocess.call(['/usr/bin/killall', '-SIGUSR1', 'startosinstall'])

    def get_app_path(self, itempath):
        '''Mounts dmgpath and returns path to the Install macOS.app'''
        if itempath.endswith('.app'):
            return itempath
        if pkgutils.hasValidDiskImageExt(itempath):
            display.display_info("Mounting disk image %s" % itempath)
            mountpoints = dmgutils.mountdmg(itempath, random_mountpoint=False)
            if mountpoints:
                # look in the first mountpoint for apps
                self.dmg_mountpoint = mountpoints[0]
                app_path = find_install_macos_app(self.dmg_mountpoint)
                if app_path:
                    # leave dmg mounted
                    return app_path
                # if we get here we didn't find an Install macOS.app with the
                # expected contents
                dmgutils.unmountdmg(self.dmg_mountpoint)
                self.dmg_mountpoint = None
                raise StartOSInstallError(
                    'Valid Install macOS.app not found on %s' % itempath)
            else:
                raise StartOSInstallError(
                    u'No filesystems mounted from %s' % itempath)
        else:
            raise StartOSInstallError(
                u'%s doesn\'t appear to be an application or disk image'
                % itempath)

    def start(self):
        '''Starts a macOS install from an Install macOS.app stored at the root
        of a disk image, or from a locally installed Install macOS.app.
        Will always reboot after if the setup is successful.
        Therefore this must be done at the end of all other actions that Munki
        performs during a managedsoftwareupdate run.'''

        # set up our signal handler
        signal.signal(signal.SIGUSR1, self.sigusr1_handler)

        # get our tool paths
        app_path = self.get_app_path(self.installer)
        startosinstall_path = os.path.join(
            app_path, 'Contents/Resources/startosinstall')

        os_version = get_os_version(app_path)

        # run startosinstall via subprocess

        # we need to wrap our call to startosinstall with a utility
        # that makes startosinstall think it is connected to a tty-like
        # device so its output is unbuffered so we can get progress info
        # otherwise we get nothing until the process exits.
        #
        # Try to find our ptyexec tool
        # first look in the parent directory of this file's directory
        # (../)
        parent_dir = (
            os.path.dirname(
                os.path.dirname(
                    os.path.abspath(__file__))))
        ptyexec_path = os.path.join(parent_dir, 'ptyexec')
        if not os.path.exists(ptyexec_path):
            # try absolute path in munki's normal install dir
            ptyexec_path = '/usr/local/munki/ptyexec'
        if os.path.exists(ptyexec_path):
            cmd = [ptyexec_path]
        else:
            # fall back to /usr/bin/script
            # this is not preferred because it uses way too much CPU
            # checking stdin for input that will never come...
            cmd = ['/usr/bin/script', '-q', '-t', '1', '/dev/null']

        cmd.extend([startosinstall_path,
                    '--agreetolicense',
                    '--applicationpath', app_path,
                    '--rebootdelay', '300',
                    '--pidtosignal', str(os.getpid()),
                    '--nointeraction'])

        if pkgutils.MunkiLooseVersion(
                os_version) < pkgutils.MunkiLooseVersion('10.12.4'):
            # --volume option is _required_ prior to 10.12.4 installer
            # and must _not_ be included in 10.12.4 installer's startosinstall
            cmd.extend(['--volume', '/'])

        # more magic to get startosinstall to not buffer its output for
        # percent complete
        env = {'NSUnbufferedIO': 'YES'}

        try:
            job = launchd.Job(cmd, environment_vars=env, cleanup_at_exit=False)
            job.start()
        except launchd.LaunchdJobException as err:
            display.display_error(
                'Error with launchd job (%s): %s', cmd, err)
            display.display_error('Aborting startosinstall run.')
            raise StartOSInstallError(err)

        startosinstall_output = []
        timeout = 2 * 60 * 60
        inactive = 0
        while True:
            if processes.stop_requested():
                job.stop()
                break

            info_output = job.stdout.readline()
            if not info_output:
                if job.returncode() is not None:
                    break
                else:
                    # no data, but we're still running
                    inactive += 1
                    if inactive >= timeout:
                        # no output for too long, kill the job
                        display.display_error(
                            "startosinstall timeout after %d seconds"
                            % timeout)
                        job.stop()
                        break
                    # sleep a bit before checking for more output
                    time.sleep(1)
                    continue

            # we got non-empty output, reset inactive timer
            inactive = 0

            info_output = info_output.decode('UTF-8')
            # save all startosinstall output in case there is
            # an error so we can dump it to the log
            startosinstall_output.append(info_output)

            # parse output for useful progress info
            msg = info_output.rstrip('\n')
            if msg.startswith('Preparing to '):
                display.display_status_minor(msg)
            elif msg.startswith('Preparing '):
                # percent-complete messages
                try:
                    percent = int(float(msg[10:].rstrip().rstrip('.')))
                except ValueError:
                    percent = -1
                display.display_percent_done(percent, 100)
            elif msg.startswith(('By using the agreetolicense option',
                                 'If you do not agree,')):
                # annoying legalese
                pass
            elif msg.startswith(
                    ('Signaling PID:', 'Waiting to reboot',
                     'Process signaled okay')):
                # messages around the SIGUSR1 signalling
                display.display_debug1('startosinstall: %s', msg)
            else:
                # none of the above, just display
                display.display_status_minor(msg)

        # startosinstall exited
        munkistatus.percent(100)
        retcode = job.returncode()
        if retcode:
            if self.dmg_mountpoint:
                dmgutils.unmountdmg(self.dmg_mountpoint)
            # append stderr to our startosinstall_output
            if job.stderr:
                startosinstall_output.extend(job.stderr.read().splitlines())
            display.display_status_minor(
                "Starting macOS install failed with return code %s" % retcode)
            display.display_error("-"*78)
            for line in startosinstall_output:
                display.display_error(line.rstrip("\n"))
            display.display_error("-"*78)
            raise StartOSInstallError(
                'startosinstall failed with return code %s' % retcode)
        if self.got_sigusr1:
            # startosinstall got far enough along to signal us it was ready
            # to finish and reboot, so we can believe it was successful
            munkilog.log("macOS install successfully set up.")
            munkilog.log(
                'Starting macOS install of %s: SUCCESSFUL' % os_version,
                'Install.log')
        else:
            if self.dmg_mountpoint:
                dmgutils.unmountdmg(self.dmg_mountpoint)
            raise StartOSInstallError(
                'startosinstall did not complete successfully. '
                'See /var/log/install.log for details.')


def get_catalog_info(mounted_dmgpath):
    '''Returns catalog info (pkginfo) for a macOS installer on a disk image'''
    app_path = find_install_macos_app(mounted_dmgpath)
    if app_path:
        display_name = os.path.splitext(os.path.basename(app_path))[0]
        name = display_name.replace(' ', '_')
        vers = get_os_version(app_path)
        description = 'Installs macOS version %s' % vers
        return {'RestartAction': 'RequireRestart',
                'apple_item': True,
                'description': description,
                'display_name': display_name,
                'installed_size': 9227469,
                #    8.8GB - http://www.apple.com/macos/how-to-upgrade/
                'installer_type': 'startosinstall',
                'minimum_munki_version': '3.0.0.3211',
                'minimum_os_version': '10.8',
                'name': name,
                'uninstallable': False,
                'version': vers}
    return None


def startosinstall(installer, finishing_tasks=None):
    '''Run startosinstall to set up an install of macOS, using a Install app
    installed locally or located on a given disk image. Returns True if
    startosinstall completes successfully, False otherwise.'''
    try:
        StartOSInstallRunner(
            installer, finishing_tasks=finishing_tasks).start()
        return True
    except StartOSInstallError, err:
        display.display_error(
            u'Error starting macOS install: %s', unicode(err))
        munkilog.log(
            'Starting macOS install: FAILED: %s' % unicode(err), 'Install.log')
        return False


def run(finishing_tasks=None):
    '''Runs the first startosinstall item in InstallInfo.plist's
    managed_installs. Returns True if successful, False otherwise'''
    managedinstallbase = prefs.pref('ManagedInstallDir')
    cachedir = os.path.join(managedinstallbase, 'Cache')
    installinfopath = os.path.join(managedinstallbase, 'InstallInfo.plist')
    try:
        installinfo = FoundationPlist.readPlist(installinfopath)
    except FoundationPlist.NSPropertyListSerializationException:
        display.display_error("Invalid %s" % installinfopath)
        return False

    if prefs.pref('SuppressStopButtonOnInstall'):
        munkistatus.hideStopButton()

    munkilog.log("### Beginning os installer session ###")
    success = False
    if "managed_installs" in installinfo:
        if not processes.stop_requested():
            # filter list to items that need to be installed
            installlist = [
                item for item in installinfo['managed_installs']
                if item.get('installer_type') == 'startosinstall']
            if installlist:
                item = installlist[0]
                if 'installer_item' in item:
                    display.display_status_major(
                        'Starting macOS %s install...'
                        % item['version_to_install'])
                    # set indeterminate progress bar
                    munkistatus.percent(-1)
                    itempath = os.path.join(cachedir, item["installer_item"])
                    success = startosinstall(
                        itempath, finishing_tasks=finishing_tasks)
    munkilog.log("### Ending os installer session ###")
    return success


if __name__ == '__main__':
    print 'This is a library of support tools for the Munki Suite.'
