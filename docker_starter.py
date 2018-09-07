import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import requests

WORK_DIR = os.path.abspath(sys.path[0])
HOME_DIR = os.path.expanduser('~')
DATA_PATH = os.path.join(HOME_DIR, '.docker_starter')


def get_ip_address():
    s = socket.socket(type=socket.SOCK_DGRAM)
    s.connect(('8.8.8.8', 80))
    return s.getsockname()[0]


def get_arch() -> str:
    aarch = {'x86_64': 'amd64', 'aarch64': 'arm64v8', 'armv7l': 'arm32v7'}
    return aarch.get(os.uname()[4], 'unknown')


def _docker_remote_sha256(rep_tag: str):
    registry, tag = rep_tag.rsplit(':', 1)
    params = {
        'service': 'registry.docker.io',
        'scope': 'repository:{}:pull'.format(registry),
    }
    headers = {'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}
    try:
        rq = requests.request('GET', 'https://auth.docker.io/token', params=params, headers=headers, stream=False)
    except (requests.exceptions.HTTPError, requests.exceptions.RequestException) as e:
        print('Auth request error: {}'.format(e))
        return None

    if rq.status_code != 200:
        print('Auth response code: {}'.format(rq.status_code))
        return None
    try:
        json = rq.json()
    except ValueError as e:
        print('Auth response error {}'.format(e))
        return None
    else:
        token = json.get('token')
    if token is None or not token:
        print('Auth error: no token')
        return None

    headers['Authorization'] = 'Bearer {}'.format(token)
    try:
        rq = requests.head('https://registry-1.docker.io/v2/{}/manifests/{}'.format(registry, tag), headers=headers)
    except (requests.exceptions.HTTPError, requests.exceptions.RequestException) as e:
        print('Registry request error: {}'.format(e))
        return None

    if rq.status_code != 200:
        print('Registry response code: {}'.format(rq.status_code))
        return None

    data = rq.headers
    if data is None:
        print('Registry error - no headers')
        return None
    sha256 = data.get('Docker-Content-Digest')
    sha256 = sha256 or data.get('Etag')
    if sha256 is None:
        print('Registry error headers parsing - sha256 not found')

    return sha256.strip('"')


def __docker_run_fatal(cmd: list, fatal: bool = False, stderr=subprocess.PIPE, stdout=subprocess.PIPE):
    run = subprocess.run(['docker', ] + cmd, stderr=stderr, stdout=stdout)
    if run.returncode:
        if fatal:
            raise RuntimeError('Error docker {}: {}'.format(' '.join(cmd), run.stderr.decode()))
    return run


def _docker_containers() -> list:
    # вернет список из [имя контейнера, ], ps -a --format "{{.Names}}"
    run = __docker_run_fatal(['ps', '-a', '--format', '{{.Names}}'], True)
    return [line for line in run.stdout.decode().split('\n') if len(line) > 2]


def _docker_stop(name: str) -> bool:
    return not __docker_run_fatal(['stop', name]).returncode


def _docker_start(name: str) -> bool:
    return not __docker_run_fatal(['start', name]).returncode


def _docker_rm(name: str) -> bool:
    return not __docker_run_fatal(['rm', name]).returncode


def _docker_rmi(rep_tag: str) -> bool:
    return not __docker_run_fatal(['rmi', rep_tag]).returncode


def _docker_pull(rep_tag: str) -> bool:
    # noinspection PyTypeChecker
    return not __docker_run_fatal(cmd=['pull', rep_tag], stdout=None, stderr=None).returncode


def _docker_build(rep_tag: str, file: str, path: str) -> bool:
    cmd = ['build', '--rm', '--no-cache', '-t', rep_tag, '-f', file, path]
    # noinspection PyTypeChecker
    return not __docker_run_fatal(cmd=cmd, stdout=None, stderr=None).returncode


def _docker_run(cmd: list):
    return not __docker_run_fatal(['run', ] + cmd).returncode


def _docker_images_sha256() -> dict:
    # docker images --digests --format "{{.Repository}}:{{.Tag}} {{.Digest}}"
    run = __docker_run_fatal(['images', '--digests', '--format', '{{.Repository}}:{{.Tag}} {{.Digest}}'], True)
    data = {}
    for line in run.stdout.decode().strip('\n').rsplit('\n'):
        rep_tag, sha256 = line.rsplit(' ', 1)
        data[rep_tag] = sha256
    return data


def _docker_images_id() -> dict:
    # docker images --format {{.Repository}}:{{.Tag}} {{.ID}}
    run = __docker_run_fatal(['images', '--format', '{{.Repository}}:{{.Tag}} {{.ID}}'], True)
    data = {}
    for line in run.stdout.decode().strip('\n').rsplit('\n'):
        rep_tag, id_ = line.rsplit(' ', 1)
        data[rep_tag] = id_
    return data


def _docker_repo_id() -> set:
    # docker images --format {{.ID}}
    run = __docker_run_fatal(['images', '--format', '{{.ID}}'], True)
    return {line for line in run.stdout.decode().strip('\n').rsplit('\n')}


class DockerStarter:
    def __init__(self, cfg: dict or list):
        self._cfg = cfg if type(cfg) is list else [cfg, ]
        self._args = self._cli_parse()
        self._containers = _docker_containers()
        self._check()
        if self._args.t:
            self._all_once()
        else:
            self._one_by_one()

    def _check(self):
        images = set()
        names = set()
        for cfg in self._cfg:
            if cfg['name'] in names:
                print('Container name {} duplicated. It must be unique. UNACCEPTABLE!'.format(cfg['name']))
                exit(1)
            names.add(cfg['name'])
            if self._args.t and cfg['image'] in images:
                print('Image {} duplicated and threading enabled, don\'t use -t. UNACCEPTABLE!'.format(cfg['image']))
                exit(1)
            images.add(cfg['image'])

    def _all_once(self):
        runs = [_StarterWorker(cfg, self._args, self._containers) for cfg in self._cfg]
        work = True
        while work:
            work = False
            time.sleep(2)
            for run in runs:
                if run.status() is None:
                    work = True
                    break

    def _one_by_one(self):
        for cfg in self._cfg:
            run = _StarterWorker(cfg, self._args, self._containers)
            while run.status() is None:
                time.sleep(2)

    @staticmethod
    def _cli_parse():
        parser = argparse.ArgumentParser()
        one = parser.add_mutually_exclusive_group(required=True)
        one.add_argument('--start', action='store_true', help='Start container')
        one.add_argument('--stop', action='store_true', help='Stop container')
        one.add_argument('--update', action='store_true', help='Check image update')
        one.add_argument('--upgrade', action='store_true', help='Upgrade image and re-create container')
        one.add_argument('--remove', action='store_true', help='Remove container and image')
        one.add_argument('--purge', action='store_true', help='Remove container, image and data')

        parser.add_argument('-e', action='append', metavar='KEY=VAL', help='Add more env')
        parser.add_argument('-b', action='store_true', help='Build images from Dockerfile, no pull from hub')
        parser.add_argument('-t', action='store_true', help='Threaded works (Dangerous)')
        parser.add_argument('-f', action='store_true', help='Allow upgrade image from other source (hub or -b)')
        return parser.parse_args()


class _StarterWorker(threading.Thread):
    def __init__(self, cfg, cli, containers):
        super().__init__()
        self._cfg = cfg
        self._cli = cli
        self._containers = containers
        self._status = None
        self.start()

    def status(self):
        return self._status

    def run(self):
        if not self._config_check():
            pass
        elif self._cli.start:
            self._start()
        elif self._cli.stop:
            self._stop()
        elif self._cli.update:
            self._update()
        elif self._cli.upgrade:
            self._upgrade()
        elif self._cli.remove:
            self._remove()
        elif self._cli.purge:
            self._purge()
        self._status = 0

    def _config_check(self):
        for key in ['name', 'image', 'data_path']:
            if key not in self._cfg:
                print('\'{}\' must be present'.format(key))
                return False
        type_check = {
            str: ['name', 'image', 'data_path', 'dockerfile', 'data_path', 'restart'],
            dict: ['p', 'v', 'e'],
            list: ['any', ]
        }
        for key in type_check:
            for opt in type_check[key]:
                if opt in self._cfg and not isinstance(self._cfg[opt], key):
                    print('Option \'{}\' must be {}, not {}'.format(opt, key.__name__, type(self._cfg[opt]).__name__))
                    return False
        return True

    def _start(self):
        if self._cfg['name'] in self._containers:
            return _docker_start(self._cfg['name'])
        if self._cfg['image'] not in _docker_images_sha256() and not self._pull():
            return print('Runtime error, exit.')
        self._run()

    def _stop(self) -> bool:
        if self._cfg['name'] in self._containers:
            if _docker_stop(self._cfg['name']):
                print('stop {}'.format(self._cfg['name']))
            else:
                print('Error stopping {}'.format(self._cfg['name']))
                return False
        elif self._cli.stop:
            print('Container {} not found.'.format(self._cfg['name']))
        return True

    def _update(self, old_sha=None):
        old_sha = old_sha or _docker_images_sha256().get(self._cfg['image'])
        if old_sha is None:
            if self._cli.update:
                print('Local {} not found. Use --start'.format(self._cfg['image']))
            return True
        new_sha = _docker_remote_sha256(self._cfg['image'])
        if new_sha is None:
            return False
        if old_sha == new_sha:
            print('{} up to date'.format(self._cfg['image']))
            return False
        else:
            if self._cli.update:
                msg = '. You build image? Use -b, -f for force pull' if old_sha == '<none>' else ''
                print('{} update found. Use --upgrade{}'.format(self._cfg['image'], msg))
            return True

    def _upgrade(self):
        old_sha = _docker_images_sha256().get(self._cfg['image'])
        if old_sha is None:
            return self._start()
        old_id_ = _docker_images_id().get(self._cfg['image'])
        if old_id_ is None:
            return print('Runtime error,{} ID not found, exit'.format(self._cfg['name']))
        if not self._allow_source_change(old_sha):
            return
        if not (self._update(old_sha) or self._cli.b):
            return
        if not self._pull():
            return print('Runtime error, exit')
        if self._stop() and self.__remove():
            self._run()
        name, _ = self._cfg['image'].rsplit(':', 1)
        self._rmi(old_id_)

    def _allow_source_change(self, old_sha):
        # Переключится с хаба на локальные сборки или обратно можно только с -f
        sources = ['hub', 'local build']
        old = sources[0] if old_sha != '<none>' else sources[1]
        new = sources[0] if not self._cli.b else sources[1]
        if old != new and not self._cli.f:
            print('Disallow! Use -f for change image source from {} to {}'.format(old, new))
            return False
        return True

    def _pull(self):
        if self._cli.b:
            return _docker_build(self._cfg['image'], self._cfg['dockerfile'], self._cfg['docker_path'])
        else:
            return _docker_pull(self._cfg['image'])

    def _remove(self):
        if self._stop() and self.__remove():
            pass
        return self._rmi()

    def __remove(self):
        if self._cfg['name'] in self._containers:
            return _docker_rm(self._cfg['name'])
        return True

    def _rmi(self, id_=None) -> bool:
        # IMAGE ID or registry:tag
        if (id_ is not None and id_ in _docker_repo_id()) or (self._cfg['image'] in _docker_images_sha256()):
            if not _docker_rmi(id_ or self._cfg['image']):
                print('Error delete {} image. Maybe containers use it?'.format(id_ or self._cfg['image']))
                return False
        return True

    def _purge(self):
        if self._remove():
            shutil.rmtree(self._cfg['data_path'], ignore_errors=True)

    def _run(self):
        makedir = False
        cmd = ['-d', ]
        for key, val in self._cfg.get('p', {}).items():
            cmd.extend(['-p', '{}:{}'.format(key, val)])

        for key, val in self._cfg.get('v', {}).items():
            makedir = True
            cmd.extend(['-v', '{}:{}'.format(os.path.join(self._cfg['data_path'], key), val)])

        for key, val in self._cfg.get('e', {}).items():
            cmd.extend(['-e', '{}={}'.format(key, val)])

        if self._cli.e is not None:
            for env in self._cli.e:
                cmd.extend(['-e', '{}'.format(env)])

        for el in self._cfg.get('any', []):
            if el[1] == ' ':
                cmd.extend([el[0], el[2]])
            else:
                cmd.append('{}{}{}'.format(*el))

        cmd.append('--restart={}'.format(self._cfg.get('restart', 'always')))
        cmd.extend(['--name', self._cfg['name']])
        cmd.append(self._cfg['image'])
        if makedir:
            os.makedirs(self._cfg['data_path'], exist_ok=True)
        result = _docker_run(cmd)
        msg = 'docker run' if result else 'Failed docker run'
        print('{} {}'.format(msg, ' '.join(cmd)))
        return result
