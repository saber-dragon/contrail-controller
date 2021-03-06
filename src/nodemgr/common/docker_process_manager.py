#
# Copyright (c) 2015 Juniper Networks, Inc. All rights reserved.
#

import docker
import gevent
import time

from docker_mem_cpu import DockerMemCpuUsageData
from sandesh_common.vns.ttypes import Module


# code calculates name from labels and then translate it to unit_name
# the unit_name is predefined and hardcoded through all Contrail's code
_docker_label_to_unit_name = {
    Module.ANALYTICS_NODE_MGR: {
        'analytics-collector': 'contrail-collector',
        'analytics-api': 'contrail-analytics-api',
        'analytics-query-engine': 'contrail-query-engine',
        'analytics-alarm-gen': 'contrail-alarm-gen',
        'analytics-nodemgr': 'contrail-analytics-nodemgr'
    },
    Module.CONFIG_NODE_MGR: {
        'config-api': 'contrail-api',
        'config-schema': 'contrail-schema',
        'config-svc-monitor': 'contrail-svc-monitor',
        'config-device-manager': 'contrail-device-manager',
        'config-cassandra': 'cassandra',
        'config-zookeeper': 'zookeeper',
        'config-nodemgr': 'contrail-config-nodemgr'
    },
    Module.CONTROL_NODE_MGR: {
        'control-control': 'contrail-control',
        'control-dns': 'contrail-dns',
        'control-named': 'contrail-named',
        'control-nodemgr': 'contrail-control-nodemgr'
    },
    Module.COMPUTE_NODE_MGR: {
        'vrouter-agent': 'contrail-vrouter-agent',
        'vrouter-nodemgr': 'contrail-vrouter-nodemgr'
    },
    Module.DATABASE_NODE_MGR: {
        'database-cassandra': 'cassandra',
        'database-zookeeper': 'zookeeper',
        'database-kafka': 'kafka',
        'database-nodemgr': 'contrail-database-nodemgr'
    },
}


def _convert_to_process_state(state):
    state_mapping = {
        'running': 'PROCESS_STATE_RUNNING',
        'exited': 'PROCESS_STATE_EXITED',
        'paused': 'PROCESS_STATE_STOPPED',
        'restarting': 'PROCESS_STATE_BACKOFF',
    }
    return state_mapping.get(state, 'PROCESS_STATE_UNKNOWN')


def _dummy_process_info(name):
    info = dict()
    info['name'] = name
    info['group'] = name
    info['pid'] = 0
    info['statename'] = 'PROCESS_STATE_EXITED'
    info['expected'] = -1
    return info


def _convert_to_pi_event(info):
    pi_event = info.copy()
    pi_event['state'] = pi_event.pop('statename')
    if 'start' in pi_event:
        del pi_event['start']
    return pi_event


class DockerProcessInfoManager(object):
    def __init__(self, module_type, unit_names, event_handlers,
                 update_process_list):
        self._module_type = module_type
        self._unit_names = unit_names
        self._event_handlers = event_handlers
        self._update_process_list = update_process_list
        self._cached_process_infos = {}
        self._client = docker.from_env()

    def _get_full_info(self, cid):
        try:
            return self._client.inspect_container(cid)
        except docker.errors.APIError:
            return None

    def _get_name_from_labels(self, container):
        labels = container.get('Labels', dict())
        pod = labels.get('net.juniper.contrail.pod')
        service = labels.get('net.juniper.contrail.service')

        if not pod:
            # try to detect pod from Env.NODE_TYPE
            if 'Env' not in container:
                # list_containers does not return 'Env' information
                info = self._get_full_info(container['Id'])
                if info:
                    container = info['Config']
            env = container.get('Env')
            if env:
                node_type = next(iter(
                    [i for i in env if i.startswith('NODE_TYPE=')]), None)
                if node_type:
                    # for now pod equals to NODE_TYPE
                    pod = node_type.split('=')[1]

        if pod and service:
            return pod + '-' + service

        name = labels.get('net.juniper.contrail')
        if not name:
            name = container.get('Name')
        return name

    def _get_unit_name(self, container):
        name = self._get_name_from_labels(container)
        if not name:
            return None
        names_map = _docker_label_to_unit_name.get(self._module_type)
        return names_map.get(name)

    def _list_containers(self, names):
        containers = dict()
        all_containers = self._client.containers(all=True)
        for container in all_containers:
            name = self._get_unit_name(container)
            if name is None or name not in names:
                continue
            if name not in containers:
                containers[name] = container
                continue
            # case when we already found container with same name and added
            # it to the list. check state of both and choose container with
            # running state.
            cur_state = container['State']
            if cur_state != containers[name]['State']:
                if cur_state == 'running':
                    containers[name] = container
                continue
            # if both has same state - add latest.
            if container['Created'] > containers[name]['Created']:
                containers[name] = container
        return containers

    def _update_cache(self, info):
        name = info['name']
        cached_info = self._cached_process_infos.get(name)
        if cached_info is None:
            self._cached_process_infos[name] = info
            return True
        if info['name'] != cached_info['name'] or \
                info['group'] != cached_info['group'] or \
                info['pid'] != cached_info['pid'] or \
                info['statename'] != cached_info['statename']:
            self._cached_process_infos[name] = info
            return True
        return False

    def _get_start_time(self, info):
        state = info.get('State')
        start_time = state.get('StartedAt') if state else None
        if start_time is None:
            start_time = info.get('Created')
        if not start_time:
            return None
        return time.mktime(time.strptime(start_time.split('.')[0],
                                         '%Y-%m-%dT%H:%M:%S'))

    def _container_to_process_info(self, container):
        info = {}
        cid = container['Id']
        full_info = self._get_full_info(cid)
        name = self._get_unit_name(full_info['Config'] if full_info else
                                   container)
        info['name'] = name
        info['group'] = name
        info['pid'] = int(cid, 16)
        start_time = self._get_start_time(full_info) if full_info else None
        info['start'] = str(int(start_time * 1000000)) if start_time else None
        info['statename'] = _convert_to_process_state(container['State'])
        if info['statename'] == 'PROCESS_STATE_EXITED':
            info['expected'] = -1
        return info

    def _poll_containers(self):
        containers = self._list_containers(self._unit_names)
        for name in self._unit_names:
            container = containers.get(name)
            info = (_dummy_process_info(name) if container is None else
                    self._container_to_process_info(container))
            if self._update_cache(info):
                self._event_handlers['PROCESS_STATE'](_convert_to_pi_event(info))
                if self._update_process_list:
                    self._event_handlers['PROCESS_LIST_UPDATE']()

    def get_all_processes(self):
        processes_info_list = []
        containers = self._list_containers(self._unit_names)
        for name in self._unit_names:
            container = containers.get(name)
            info = (_dummy_process_info(name) if container is None else
                    self._container_to_process_info(container))
            processes_info_list.append(info)
            self._update_cache(info)
        return processes_info_list

    def runforever(self):
        # TODO: probaly use subscription on events..
        while True:
            self._poll_containers()
            gevent.sleep(seconds=5)

    def get_mem_cpu_usage_data(self, pid, last_cpu, last_time):
        return DockerMemCpuUsageData(pid, last_cpu, last_time)

    def exec_cmd(self, unit_name, cmd):
        containers = self._list_containers(names=[unit_name])
        container = containers.get(unit_name)
        if not container:
            raise LookupError(unit_name)
        exec_op = self._client.exec_create(container['Id'], cmd, tty=True)
        res = ''
        try:
            # string or stream result works unstable
            # using socket with own read implementation
            socket = self._client.exec_start(exec_op["Id"], tty=True, socket=True)
            socket.settimeout(10.0)
            while True:
                part = socket.recv(1024)
                if len(part) == 0:
                    break
                res += part
        finally:
            if socket:
                socket.close()
        data = self._client.exec_inspect(exec_op["Id"])
        exit_code = data.get("ExitCode", 0)
        if exit_code != 0:
            raise RuntimeError("Result: {}\nExit data: {}".format(res, data))
        return res
