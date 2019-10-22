#!/usr/bin/env python

import argparse
import json
import logging
import os
import random
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import cryptography.hazmat.backends
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.utils import base64url_decode, bytes_to_number

from dcos_internal_utils import bootstrap, exhibitor, utils

log = logging.getLogger(__name__)


def _known_exec_directory():
    """
    Returns a directory which we have told users to mark as ``exec``.
    """
    # This directory must be outside /tmp to support
    # environments where /tmp is mounted noexec.
    known_directory = Path('/var/lib/dcos/exec')
    known_directory.mkdir(parents=True, exist_ok=True)
    return known_directory


def _create_private_directory(path, owner):
    """
    Create a directory which ``owner`` can create, modify and delete files in
    but other non-root users cannot.

    Args:
        path (pathlib.Path): The path to the directory to create.
        owner (str): The owner of the directory.
    """
    path.mkdir(exist_ok=True)
    path.chmod(0o700)
    shutil.chown(str(path), user=owner)


def check_root(fun):
    def wrapper(b, opts):
        if os.getuid() != 0:
            log.error('bootstrap must be run as root')
            sys.exit(1)
        fun(b, opts)
    return wrapper


@check_root
def dcos_adminrouter(b, opts):
    b.cluster_id()

    # Require the IAM to already be up and running. The IAM contains logic for
    # achieving consensus about a key pair, and exposes the public key
    # information via its JWKS endpoint. Talk directly to the local IAM instance
    # which is reachable via the local network interface.
    r = requests.get('http://127.0.0.1:8101/acs/api/v1/auth/jwks')

    if r.status_code != 200:
        log.info('JWKS retrieval failed. Got %s with body: %s', r, r.text)
        sys.exit(1)

    jwks = r.json()

    # The first key in the JSON Web Key Set corresponds to the current private
    # key used for signing authentiction tokens.
    key = jwks['keys'][0]

    exponent_bytes = base64url_decode(key['e'].encode('ascii'))
    exponent_int = bytes_to_number(exponent_bytes)
    modulus_bytes = base64url_decode(key['n'].encode('ascii'))
    modulus_int = bytes_to_number(modulus_bytes)
    # Generate a `cryptography` public key object instance from these numbers.
    public_numbers = rsa.RSAPublicNumbers(n=modulus_int, e=exponent_int)
    public_key = public_numbers.public_key(
        backend=cryptography.hazmat.backends.default_backend())

    # Serialize public key into the OpenSSL PEM public key format RFC 5280).
    pubkey_pem_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)

    os.makedirs('/run/dcos/dcos-adminrouter', exist_ok=True)
    pubkey_path = '/run/dcos/dcos-adminrouter/auth-token-verification-key'
    _write_file_bytes(pubkey_path, pubkey_pem_bytes, 0o644)
    shutil.chown(pubkey_path, user='dcos_adminrouter')


@check_root
def dcos_signal(b, opts):
    b.cluster_id()


@check_root
def dcos_telegraf_master(b, opts):
    b.cluster_id()


@check_root
def dcos_telegraf_agent(b, opts):
    b.cluster_id(readonly=True)


@check_root
def dcos_net(b, opts):
    if 'master' in get_roles():
        dcos_net_master(b, opts)
    else:
        dcos_net_agent(b, opts)


@check_root
def dcos_net_master(b, opts):
    b.cluster_id()


@check_root
def dcos_net_agent(b, opts):
    b.cluster_id(readonly=True)


@check_root
def dcos_bouncer(b, opts):
    os.makedirs('/run/dcos/dcos-bouncer', exist_ok=True)
    shutil.chown('/run/dcos/dcos-bouncer', user='dcos_bouncer')
    # Permissions are restricted to the dcos_bouncer user as this directory
    # contains sensitive data.  See
    # https://jira.mesosphere.com/browse/DCOS-18350

    # The ``bouncer_tmpdir`` directory path corresponds to the
    # TMPDIR environment variable configured in the dcos-bouncer.service file.
    user = 'dcos_bouncer'
    bouncer_tmpdir = _known_exec_directory() / user
    _create_private_directory(path=bouncer_tmpdir, owner=user)


@check_root
def dcos_cockroach_config_change(b, opts):
    # Permissions are restricted to the dcos_cockroach user in case this
    # directory contains sensitive data - we also want to avoid the security
    # risk of other users writing to this directory.
    # See https://jira.mesosphere.com/browse/DCOS-18350 for a related change to
    # dcos-bouncer.
    #
    # The ``dcos_cockroach`` user is the ``User`` used in the
    # ``dcos-cockroachdb-config-change.service``

    # The ``cockroach_tmpdir`` directory path corresponds to the
    # dcos-cockroachdb-config-change.service.
    user = 'dcos_cockroach'
    cockroach_tmpdir = _known_exec_directory() / user
    _create_private_directory(path=cockroach_tmpdir, owner=user)


def noop(b, opts):
    return


bootstrappers = {
    'dcos-adminrouter': dcos_adminrouter,
    'dcos-bouncer': dcos_bouncer,
    'dcos-signal': dcos_signal,
    'dcos-diagnostics-master': noop,
    'dcos-diagnostics-agent': noop,
    'dcos-checks-master': noop,
    'dcos-checks-agent': noop,
    'dcos-fluent-bit-master': noop,
    'dcos-fluent-bit-agent': noop,
    'dcos-marathon': noop,
    'dcos-mesos-master': noop,
    'dcos-mesos-slave': noop,
    'dcos-mesos-slave-public': noop,
    'dcos-cosmos': noop,
    'dcos-cockroach': noop,
    'dcos-cockroach-config-change': dcos_cockroach_config_change,
    'dcos-metronome': noop,
    'dcos-mesos-dns': noop,
    'dcos-net': dcos_net,
    'dcos-telegraf-master': dcos_telegraf_master,
    'dcos-telegraf-agent': dcos_telegraf_agent,
    'dcos-ui-update-service': noop,
}


def get_roles():
    return os.listdir('/opt/mesosphere/etc/roles')


def main():
    opts = parse_args()

    # Display the pid in each log message to distinguish concurrent runs
    log_format = 'pid={}:[%(levelname)s] %(message)s'.format(os.getpid())
    logging.basicConfig(format=log_format, level='INFO')
    log.setLevel(logging.DEBUG)

    log.info('Clearing proxy environment variables')
    for name in ['HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY']:
        os.environ.pop(name, None)
        os.environ.pop(name.lower(), None)

    if 'master' in get_roles():
        exhibitor.wait(opts.master_count)

    b = bootstrap.Bootstrapper(opts.zk)

    for service in opts.services:
        if service not in bootstrappers:
            log.error('Unknown service: {}'.format(service))
            sys.exit(1)
        utils.apply_service_configuration(service)
        log.info('bootstrapping {}'.format(service))
        bootstrappers[service](b, opts)


def get_zookeeper_address_agent():
    if os.getenv('MASTER_SOURCE') == 'master_list':
        # dcos-net agents with static master list
        with open('/opt/mesosphere/etc/master_list', 'r') as f:
            master_list = json.load(f)
        assert len(master_list) > 0
        return random.choice(master_list) + ':2181'
    elif os.getenv('EXHIBITOR_ADDRESS'):
        # dcos-net agents on AWS
        return os.getenv('EXHIBITOR_ADDRESS') + ':2181'
    else:
        # any other agent service
        return 'zk-1.zk:2181,zk-2.zk:2181,zk-3.zk:2181,zk-4.zk:2181,zk-5.zk:2181'


def get_zookeeper_address():
    # Masters use a special zk address since dcos-net and the like aren't up yet.
    roles = get_roles()
    if 'master' in roles:
        return '127.0.0.1:2181'

    if 'slave' in roles or 'slave_public' in roles:
        return get_zookeeper_address_agent()

    raise Exception("Can't get zookeeper address. Unknown role: {}".format(roles))


def parse_args():
    zk_default = get_zookeeper_address()

    parser = argparse.ArgumentParser()
    parser.add_argument('services', nargs='+')
    parser.add_argument(
        '--zk',
        type=str,
        default=zk_default,
        help='Host string passed to Kazoo client constructor.')
    parser.add_argument(
        '--master_count',
        type=str,
        default='/opt/mesosphere/etc/master_count',
        help='File with number of master servers')
    return parser.parse_args()


def _write_file_bytes(path, data, mode):
    """
    Atomically write `data` to `path` using the file permissions
    `stat.S_IMODE(mode)`.

    File consumers can rely on seeing valid file contents once they are able to
    open the file. This is achieved by performing all relevant operations on a
    temporary file followed by a `os.replace()` which, if successful, renames to
    the desired path (and overwrites upon conflict) in an atomic operation (on
    both, Windows, and Linux).

    If acting on the temporary file fails (be it writing, closing, chmodding,
    replacing) an attempt is performed to remove the temporary file; and the
    original exception is re-raised.
    """
    assert isinstance(data, bytes)

    basename = os.path.basename(path)
    tmpfile_dir = os.path.dirname(os.path.realpath(path))

    fd, tmpfile_path = tempfile.mkstemp(prefix=basename, dir=tmpfile_dir)

    try:
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.chmod(tmpfile_path, stat.S_IMODE(mode))
        os.replace(tmpfile_path, path)
    except Exception:
        os.remove(tmpfile_path)
        raise
