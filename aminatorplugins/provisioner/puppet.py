# -*- coding: utf-8 -*-

#
#
#  Copyright 2013 Netflix
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
#
#

"""
aminator.plugins.provisioner.puppet
================================
"""
import os
import re
import glob
import shutil
import time
import socket
import logging
from collections import namedtuple
import json

from aminator.plugins.provisioner.base import BaseProvisionerPlugin
from aminator.plugins.provisioner.yum import yum_clean_metadata
from aminator.util.linux import monitor_command, mkdir_p, Chroot
from aminator.config import conf_action

__all__ = ('PuppetProvisionerPlugin',)
log = logging.getLogger(__name__)

CommandResult = namedtuple('CommandResult', 'success result')
CommandOutput = namedtuple('CommandOutput', 'std_out std_err')


class PuppetProvisionerPlugin(BaseProvisionerPlugin):
    """
    PuppetProvisionerPlugin takes the majority of its behavior from BaseProvisionerPlugin
    See BaseProvisionerPlugin for details
    """
    _name = 'puppet'

    def add_plugin_args(self):
        puppet_config = self._parser.add_argument_group(title='Puppet Options',
                                                      description='Options for the puppet provisioner')

        puppet_config.add_argument('--puppet-args', dest='puppet_args',
                                    action=conf_action(self._config.plugins[self.full_name]),
                                    help='Extra arguments for Puppet.  Can be used to include a Puppet class with -e.')

        puppet_config.add_argument('--puppet-env-vars', dest='puppet_env_vars',
                                    action=conf_action(self._config.plugins[self.full_name]),
                                    help='Environment arguments for Puppet, delimited by a semi-colon.  Can be used to do things like pass custom Facter facts.')

        puppet_config.add_argument('--puppet-master', dest='puppet_master',
                                    action=conf_action(self._config.plugins[self.full_name]),
                                    help='Hostname of Puppet Master')

        puppet_config.add_argument('--puppet-certs-dir', dest='puppet_certs_dir',
                                    action=conf_action(self._config.plugins[self.full_name]),
                                    help='Used when generating/copying certs for use with Puppet Master')

        puppet_config.add_argument('--puppet-private-keys-dir', dest='puppet_private_keys_dir',
                                    action=conf_action(self._config.plugins[self.full_name]),
                                    help='Used when generating/copying certs for use with Puppet Master')

        puppet_config.add_argument('--puppet-hieradata', dest='puppet_hieradata',
                                    action=conf_action(self._config.plugins[self.full_name]),
                                    help='The name of the tarball containing a hiera.yaml file and hieradata directory.  This option requires Puppet >= 3.1.')

        puppet_config.add_argument('--puppet-install-cmd', dest='puppet_install_cmd',
                                    action=conf_action(self._config.plugins[self.full_name]),
                                    help='The command to use to install Puppet.  The native package manager will be used by default.')

        puppet_config.add_argument('--puppet-hiera-install-cmd', dest='puppet_hiera_install_cmd',
                                    action=conf_action(self._config.plugins[self.full_name]),
                                    help='The command to use to install Hiera.  Gem will be used by default.')

    def _get_config_value(self, name, default = ''):
        config = self._config.plugins[self.full_name]

        if config.get(name):
            return config.get(name)

        self._config.plugins[self.full_name].__setattr__(name, default)
        return default

    def provision(self):
        """
        overrides the base provision
      * generate certificates
      * install the certificates on the target volume
          * install puppet on the target volume
      * run the puppet agent in the target chroot environment
        """

        log.info('=========================================================================================================')
        log.debug('Starting provision')

        context = self._config.context
        config = self._config

        self._decide_puppet_run_mode()
        self._pre_chroot_block()

        if re.compile('\S=\S').search(self._get_config_value('puppet_env_vars')):
            parsed_env_vars = dict([x.split('=') for x in re.compile("\s?;\s?").split(self._get_config_value('puppet_env_vars'))])
            log.info("\tAdding to Puppet apply environment:")
            for key, value in parsed_env_vars.iteritems():
                log.info("\t{0}={1}".format(key,value))
            log.info('=========================================================================================================')
            os.environ.update( parsed_env_vars )

        log.debug('Entering chroot at {0}'.format(self._distro._mountpoint))
        with Chroot(self._distro._mountpoint):

            self._install_puppet()

            puppet_args = self._get_config_value('puppet_args', '' )

            if self._puppet_run_mode is 'master':
                log.info('Running puppet agent')
                result = puppet_agent( puppet_args, context.package.arg, self._get_config_value('puppet_master', socket.gethostname()) )
                self._rm_puppet_certs_dirs()
            elif self._puppet_run_mode is 'apply':
                if self._puppet_apply_file is '':
                    log.info('Running puppet apply')
                else:
                    log.info('Running puppet apply for {0}'.format(self._puppet_apply_file))
                result = puppet_apply( puppet_args, self._puppet_apply_file )

            # * --detailed-exitcodes:
            #   Provide transaction information via exit codes. If this is enabled, an exit
            #   code of '2' means there were changes, an exit code of '4' means there were
            #   failures during the transaction, and an exit code of '6' means there were both
            #   changes and failures.
            log.info('Puppet status code {0} with result {1}'.format(result.result.status_code, result.result))
            if not (result.result.status_code in [0,2]):
                log.critical('Puppet run failed: {0.std_err}'.format(result.result))
                return False

        log.info('=========================================================================================================')
        log.debug('Exited chroot')

        return True

    def _pre_chroot_block(self):
        context = self._config.context

        log.debug("Setting metadata release to {0}".format(time.strftime("%Y%m%d%H%M")))
        context.package.attributes = {'name': '', 'version': 'puppet', 'release': time.strftime("%Y%m%d%H%M") }

        if self._puppet_run_mode is 'master':
            self._set_up_puppet_certs(context.package.arg)
        elif self._puppet_run_mode is 'apply':
            self._set_up_puppet_manifests(context.package.arg)

            puppet_hieradata = self._get_config_value('puppet_hieradata', '' )
            if puppet_hieradata != '':
                self._set_up_hieradata(puppet_hieradata)

    def _store_package_metadata(self):
        ""

    def _provision_package(self):
        ""

    def _set_up_puppet_certs(self, pem_file_name):
        certs_dir = self._get_config_value('puppet_certs_dir')
        private_keys_dir = self._get_config_value('puppet_private_keys_dir')

        mkdir_p(self._distro._mountpoint + certs_dir)
        mkdir_p(self._distro._mountpoint + private_keys_dir)

        cert = os.path.join(certs_dir,pem_file_name + '.pem')
        key = os.path.join(private_keys_dir, pem_file_name + '.pem')

        if not os.access( cert, os.F_OK ):
            generate_certificate(self._config.context.package.arg)

        log.debug('Placing certs for {0} into mountpoint {1}'.format(pem_file_name, self._distro._mountpoint))
        shutil.copy(os.path.join(certs_dir, 'ca.pem'),              self._distro._mountpoint + certs_dir)
        shutil.copy(cert , self._distro._mountpoint + certs_dir)
        shutil.copy(key, self._distro._mountpoint + private_keys_dir)

    def _set_up_puppet_manifests(self, manifests):
        import tarfile
        import shutil

        if tarfile.is_tarfile(manifests):
            self._puppet_apply_file = ''
            tar = tarfile.open(manifests,'r:gz')

            dest_dir = os.path.join(self._distro._mountpoint,'etc','puppet') if 'modules' in tar.getnames() else os.path.join(self._distro._mountpoint,'etc','puppet','modules')

            mkdir_p(dest_dir)
            log.debug('Untarring to {0}'.format(dest_dir))
            tar.extractall(dest_dir)
            tar.close

            self._list_files(self._distro._mountpoint + '/etc/puppet')

        else:
            self._puppet_apply_file = os.path.join('etc','puppet','modules', os.path.basename(manifests))
            dest_file = os.path.join(self._distro._mountpoint,'etc','puppet','modules', os.path.basename(manifests))
            mkdir_p(os.path.join(self._distro._mountpoint,'etc','puppet','modules'))
            log.debug('Trying to copy \'{0}\' to \'{1}\''.format(manifests, dest_file))
            shutil.copy2(manifests, dest_file)

    def _set_up_hieradata(self, hieradata):
        import tarfile
        import shutil

        if tarfile.is_tarfile(hieradata):
            tar = tarfile.open(hieradata,'r:gz')

            dest_dir = os.path.join(self._distro._mountpoint,'etc','puppet')

            mkdir_p(dest_dir)
            log.debug('Untarring {0} to {1}'.format(hieradata, dest_dir))
            tar.extractall(dest_dir)
            tar.close

            self._list_files(self._distro._mountpoint + '/etc/puppet/hieradata')
        else:
            log.debug('Hieradata file \'{0}\' is not a tarball.', format(hieradata))

    def _rm_puppet_certs_dirs(self):
        shutil.rmtree(self._get_config_value('puppet_certs_dir'))
        shutil.rmtree(self._get_config_value('puppet_private_keys_dir'))

    def _list_files(self, startpath):
        log.debug("********************************************************")
        start_len = len(startpath.split('/'))
        for root, dir, files in os.walk(startpath):
            path = root.split('/')
            log.debug((len(path) - start_len) * '---' + ' {0}'.format(os.path.basename(root)))
            for file in files:
                log.debug((len(path) - start_len + 1) * '---' + '{0}'.format(file))
        log.debug("********************************************************")

    def _decide_puppet_run_mode(self):
        if os.access( self._config.context.package.arg, os.F_OK ):
            log.info("{0} appears to be a file.  Running Puppet in Masterless mode with that as our Puppet manifests.".format(self._config.context.package.arg))
            self._puppet_run_mode = 'apply'
        else:
            log.info("{0} does not appear to be a file.  Using that as our Puppet certname.".format(self._config.context.package.arg))
            self._puppet_run_mode = 'master'

    def _install_puppet(self):
        puppet_install_cmd = self._get_config_value('puppet_install_cmd', '')
        if puppet_install_cmd != '':
            log.info('Installing Puppet with command \'{0}\'.', format(puppet_install_cmd))
            monitor_command(puppet_install_cmd)
        else:
            if self._distro._name is 'redhat':
                log.info('Installing Puppet with yum.')
                yum_clean_metadata()
                monitor_command('yum --nogpgcheck -y install puppet')
            else:
                log.info('Installing Puppet with apt.')
                monitor_command('apt-get update')
                monitor_command('apt-get -y install puppet')

        puppet_hieradata = self._get_config_value('puppet_hieradata', '' )
        if puppet_hieradata != '':
            puppet_hiera_install_cmd = self._get_config_value('puppet_hiera_install_cmd', '')
            if puppet_hiera_install_cmd != '':
                log.info('Installing Hiera with \'{0}\'.', format(puppet_hiera_install_cmd))
                monitor_command(puppet_hiera_install_cmd)
            else:
                log.info('Installing Hiera Ruby gem.')
                monitor_command('gem install hiera')


def puppet_agent( puppet_args, certname, puppet_master):
    return monitor_command("puppet agent --detailed-exitcodes --no-daemonize --logdest console --onetime --certname {0} --server {1}".format(certname, puppet_master,puppet_args))

def puppet_apply( puppet_args, puppet_apply_file ):
    return monitor_command("puppet apply --detailed-exitcodes --logdest console --debug --verbose {0} {1}".format(puppet_args, puppet_apply_file))

def generate_certificate(certname):
    log.debug('Generating certificate for {0}'.format(certname))
    return monitor_command(['puppetca','generate',certname])
