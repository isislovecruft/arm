"""
Background tasks for gathering information about the tor process.

::

  get_connection_tracker - provides a ConnectionTracker for our tor process
  get_resource_tracker - provides a ResourceTracker for our tor process

  stop_trackers - halts any active trackers

  Daemon - common parent for resolvers
    |- run_counter - number of successful runs
    |- get_rate - provides the rate at which we run
    |- set_rate - sets the rate at which we run
    |- set_paused - pauses or continues work
    +- stop - stops further work by the daemon

  ConnectionTracker - periodically checks the connections established by tor
    |- get_custom_resolver - provide the custom conntion resolver we're using
    |- set_custom_resolver - overwrites automatic resolver selecion with a custom resolver
    +- get_connections - provides our latest connection results

  ResourceTracker - periodically checks the resource usage of tor
    +- get_resource_usage - provides our latest resource usage results

.. data:: Resources

  Resource usage information retrieved about the tor process.

  :var float cpu_sample: average cpu usage since we last checked
  :var float cpu_average: average cpu usage since we first started tracking the process
  :var float cpu_total: total cpu time the process has used since starting
  :var int memory_bytes: memory usage of the process in bytes
  :var float memory_percent: percentage of our memory used by this process
  :var float timestamp: unix timestamp for when this information was fetched
"""

import collections
import time
import threading

from stem.control import State
from stem.util import conf, connection, log, proc, str_tools, system

from arm.util import tor_controller, debug, info, notice

CONFIG = conf.config_dict('arm', {
  'queries.connections.rate': 5,
  'queries.resources.rate': 5,
  'queries.port_usage.rate': 5,
})

CONNECTION_TRACKER = None
RESOURCE_TRACKER = None
PORT_USAGE_TRACKER = None

Resources = collections.namedtuple('Resources', [
  'cpu_sample',
  'cpu_average',
  'cpu_total',
  'memory_bytes',
  'memory_percent',
  'timestamp',
])


def get_connection_tracker():
  """
  Singleton for tracking the connections established by tor.
  """

  global CONNECTION_TRACKER

  if CONNECTION_TRACKER is None:
    CONNECTION_TRACKER = ConnectionTracker(CONFIG['queries.connections.rate'])

  return CONNECTION_TRACKER


def get_resource_tracker():
  """
  Singleton for tracking the resource usage of our tor process.
  """

  global RESOURCE_TRACKER

  if RESOURCE_TRACKER is None:
    RESOURCE_TRACKER = ResourceTracker(CONFIG['queries.resources.rate'])

  return RESOURCE_TRACKER


def get_port_usage_tracker():
  """
  Singleton for tracking the process using a set of ports.
  """

  global PORT_USAGE_TRACKER

  if PORT_USAGE_TRACKER is None:
    PORT_USAGE_TRACKER = PortUsageTracker(CONFIG['queries.port_usage.rate'])

  return PORT_USAGE_TRACKER


def stop_trackers():
  """
  Halts active trackers, providing back the thread shutting them down.

  :returns: **threading.Thread** shutting down the daemons
  """

  def halt_trackers():
    trackers = filter(lambda t: t.is_alive(), [
      get_resource_tracker(),
      get_connection_tracker(),
    ])

    for tracker in trackers:
      tracker.stop()

    for tracker in trackers:
      tracker.join()

  halt_thread = threading.Thread(target = halt_trackers)
  halt_thread.setDaemon(True)
  halt_thread.start()
  return halt_thread


def _resources_via_ps(pid):
  """
  Fetches resource usage information about a given process via ps. This returns
  a tuple of the form...

    (total_cpu_time, uptime, memory_in_bytes, memory_in_percent)

  :param int pid: process to be queried

  :returns: **tuple** with the resource usage information

  :raises: **IOError** if unsuccessful
  """

  # ps results are of the form...
  #
  #     TIME     ELAPSED   RSS %MEM
  # 3-08:06:32 21-00:00:12 121844 23.5
  #
  # ... or if Tor has only recently been started...
  #
  #     TIME      ELAPSED    RSS %MEM
  #  0:04.40        37:57  18772  0.9

  ps_call = system.call("ps -p {pid} -o cputime,etime,rss,%mem".format(pid = pid))

  if ps_call and len(ps_call) >= 2:
    stats = ps_call[1].strip().split()

    if len(stats) == 4:
      try:
        total_cpu_time = str_tools.parse_short_time_label(stats[0])
        uptime = str_tools.parse_short_time_label(stats[1])
        memory_bytes = int(stats[2]) * 1024  # ps size is in kb
        memory_percent = float(stats[3]) / 100.0

        return (total_cpu_time, uptime, memory_bytes, memory_percent)
      except ValueError:
        pass

  raise IOError("unrecognized output from ps: %s" % ps_call)


def _resources_via_proc(pid):
  """
  Fetches resource usage information about a given process via proc. This
  returns a tuple of the form...

    (total_cpu_time, uptime, memory_in_bytes, memory_in_percent)

  :param int pid: process to be queried

  :returns: **tuple** with the resource usage information

  :raises: **IOError** if unsuccessful
  """

  utime, stime, start_time = proc.get_stats(
    pid,
    proc.Stat.CPU_UTIME,
    proc.Stat.CPU_STIME,
    proc.Stat.START_TIME,
  )

  total_cpu_time = float(utime) + float(stime)
  memory_in_bytes = proc.get_memory_usage(pid)[0]
  total_memory = proc.get_physical_memory()

  uptime = time.time() - float(start_time)
  memory_in_percent = float(memory_in_bytes) / total_memory

  return (total_cpu_time, uptime, memory_in_bytes, memory_in_percent)


def _process_for_ports(local_ports, remote_ports):
  """
  Provides the name of the process using the given ports.

  :param list local_ports: local port numbers to look up
  :param list remote_ports: remote port numbers to look up

  :returns: **dict** mapping the ports to the associated process names

  :raises: **IOError** if unsuccessful
  """

  def _parse_lsof_line(line):
    line_comp = line.split()

    if not line:
      return None, None, None  # blank line
    elif len(line_comp) != 10:
      raise ValueError('lines are expected to have ten fields')
    elif line_comp[9] != '(ESTABLISHED)':
      return None, None, None  # connection isn't established

    cmd = line_comp[0]
    port_map = line_comp[8]

    if '->' not in port_map:
      raise ValueError("'%s' is expected to be a '->' separated mapping" % port_map)

    local, remote = port_map.split('->', 1)

    if ':' not in local or ':' not in remote:
      raise ValueError("'%s' is expected to be 'address:port' entries" % port_map)

    local_port = local.split(':', 1)[1]
    remote_port = remote.split(':', 1)[1]

    if not connection.is_valid_port(local_port):
      raise ValueError("'%s' isn't a valid port" % local_port)
    elif not connection.is_valid_port(remote_port):
      raise ValueError("'%s' isn't a valid port" % remote_port)

    return int(local_port), int(remote_port), cmd

  # atagar@fenrir:~/Desktop/arm$ lsof -i tcp:51849 -i tcp:37277
  # COMMAND  PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
  # tor     2001 atagar   14u  IPv4  14048      0t0  TCP localhost:9051->localhost:37277 (ESTABLISHED)
  # tor     2001 atagar   15u  IPv4  22024      0t0  TCP localhost:9051->localhost:51849 (ESTABLISHED)
  # python  2462 atagar    3u  IPv4  14047      0t0  TCP localhost:37277->localhost:9051 (ESTABLISHED)
  # python  3444 atagar    3u  IPv4  22023      0t0  TCP localhost:51849->localhost:9051 (ESTABLISHED)

  lsof_cmd = 'lsof -nP ' + ' '.join(['-i tcp:%s' % port for port in (local_ports + remote_ports)])
  lsof_call = system.call(lsof_cmd)

  if lsof_call:
    results = {}

    if lsof_call[0].startswith('COMMAND  '):
      lsof_call = lsof_call[1:]  # strip the title line

    for line in lsof_call:
      try:
        local_port, remote_port, cmd = _parse_lsof_line(line)

        if local_port in local_ports:
          results[local_port] = cmd
        elif remote_port in remote_ports:
          results[remote_port] = cmd
      except ValueError as exc:
        raise IOError("unrecognized output from lsof (%s): %s" % (exc, line))

    return results

  raise IOError("no results from lsof")


class Daemon(threading.Thread):
  """
  Daemon that can perform a given action at a set rate. Subclasses are expected
  to implement our _task() method with the work to be done.
  """

  def __init__(self, rate):
    super(Daemon, self).__init__()
    self.setDaemon(True)

    self._process_lock = threading.RLock()
    self._process_pid = None
    self._process_name = None

    self._rate = rate
    self._last_ran = -1  # time when we last ran
    self._run_counter = 0  # counter for the number of successful runs

    self._is_paused = False
    self._pause_condition = threading.Condition()
    self._halt = False  # terminates thread if true

    controller = tor_controller()
    controller.add_status_listener(self._tor_status_listener)
    self._tor_status_listener(controller, State.INIT, None)

  def run(self):
    while not self._halt:
      time_since_last_ran = time.time() - self._last_ran

      if self._is_paused or time_since_last_ran < self._rate:
        sleep_duration = max(0.02, self._rate - time_since_last_ran)

        with self._pause_condition:
          if not self._halt:
            self._pause_condition.wait(sleep_duration)

        continue  # done waiting, try again

      with self._process_lock:
        if self._process_pid is not None:
          is_successful = self._task(self._process_pid, self._process_name)
        else:
          is_successful = False

        if is_successful:
          self._run_counter += 1

      self._last_ran = time.time()

  def _task(self, process_pid, process_name):
    """
    Task the resolver is meant to perform. This should be implemented by
    subclasses.

    :param int process_pid: pid of the process we're tracking
    :param str process_name: name of the process we're tracking

    :returns: **bool** indicating if our run was successful or not
    """

    return True

  def run_counter(self):
    """
    Provides the number of times we've successful runs so far. This can be used
    by callers to determine if our results have been seen by them before or
    not.

    :returns: **int** for the run count we're on
    """

    return self._run_counter

  def get_rate(self):
    """
    Provides the rate at which we perform our task.

    :returns: **float** for the rate in seconds at which we perform our task
    """

    return self._rate

  def set_rate(self, rate):
    """
    Sets the rate at which we perform our task in seconds.

    :param float rate: rate at which to perform work in seconds
    """

    self._rate = rate

  def set_paused(self, pause):
    """
    Either resumes or holds off on doing further work.

    :param bool pause: halts work if **True**, resumes otherwise
    """

    self._is_paused = pause

  def stop(self):
    """
    Halts further work and terminates the thread.
    """

    with self._pause_condition:
      self._halt = True
      self._pause_condition.notifyAll()

  def _tor_status_listener(self, controller, event_type, _):
    with self._process_lock:
      if not self._halt and event_type in (State.INIT, State.RESET):
        tor_pid = controller.get_pid(None)
        tor_cmd = system.get_name_by_pid(tor_pid) if tor_pid else None

        self._process_pid = tor_pid
        self._process_name = tor_cmd if tor_cmd else 'tor'

  def __enter__(self):
    self.start()
    return self

  def __exit__(self, exit_type, value, traceback):
    self.stop()
    self.join()


class ConnectionTracker(Daemon):
  """
  Periodically retrieves the connections established by tor.
  """

  def __init__(self, rate):
    super(ConnectionTracker, self).__init__(rate)

    self._connections = []
    self._resolvers = connection.get_system_resolvers()
    self._custom_resolver = None

    # Number of times in a row we've either failed with our current resolver or
    # concluded that our rate is too low.

    self._failure_count = 0
    self._rate_too_low_count = 0

  def _task(self, process_pid, process_name):
    if self._custom_resolver:
      resolver = self._custom_resolver
      is_default_resolver = False
    elif self._resolvers:
      resolver = self._resolvers[0]
      is_default_resolver = True
    else:
      return False  # nothing to resolve with

    try:
      start_time = time.time()

      self._connections = connection.get_connections(
        resolver,
        process_pid = process_pid,
        process_name = process_name,
      )

      runtime = time.time() - start_time

      if is_default_resolver:
        self._failure_count = 0

      # Reduce our rate if connection resolution is taking a long time. This is
      # most often an issue for extremely busy relays.

      min_rate = 100 * runtime

      if self.get_rate() < min_rate:
        self._rate_too_low_count += 1

        if self._rate_too_low_count >= 3:
          min_rate += 1  # little extra padding so we don't frequently update this
          self.set_rate(min_rate)
          self._rate_too_low_count = 0
          debug('tracker.lookup_rate_increased', seconds = "%0.1f" % min_rate)
      else:
        self._rate_too_low_count = 0

      return True
    except IOError as exc:
      log.info(exc)

      # Fail over to another resolver if we've repeatedly been unable to use
      # this one.

      if is_default_resolver:
        self._failure_count += 1

        if self._failure_count >= 3:
          self._resolvers.pop(0)
          self._failure_count = 0

          if self._resolvers:
            notice(
              'tracker.unable_to_use_resolver',
              old_resolver = resolver,
              new_resolver = self._resolvers[0],
            )
          else:
            notice('tracker.unable_to_use_all_resolvers')

      return False

  def get_custom_resolver(self):
    """
    Provides the custom resolver the user has selected. This is **None** if
    we're picking resolvers dynamically.

    :returns: :data:`~stem.util.connection.Resolver` we're overwritten to use
    """

    return self._custom_resolver

  def set_custom_resolver(self, resolver):
    """
    Sets the resolver used for connection resolution. If **None** then this is
    automatically determined based on what is available.

    :param stem.util.connection.Resolver resolver: resolver to use
    """

    self._custom_resolver = resolver

  def get_connections(self):
    """
    Provides a listing of tor's latest connections.

    :returns: **list** of :class:`~stem.util.connection.Connection` we last
      retrieved, an empty list if our tracker's been stopped
    """

    if self._halt:
      return []
    else:
      return list(self._connections)


class ResourceTracker(Daemon):
  """
  Periodically retrieves the resource usage of tor.
  """

  def __init__(self, rate):
    super(ResourceTracker, self).__init__(rate)

    self._resources = None
    self._use_proc = proc.is_available()  # determines if we use proc or ps for lookups
    self._failure_count = 0  # number of times in a row we've failed to get results

  def get_resource_usage(self):
    """
    Provides tor's latest resource usage.

    :returns: latest :data:`~arm.util.tracker.Resources` we've polled
    """

    result = self._resources
    return result if result else Resources(0.0, 0.0, 0.0, 0, 0.0, 0.0)

  def _task(self, process_pid, process_name):
    try:
      resolver = _resources_via_proc if self._use_proc else _resources_via_ps
      total_cpu_time, uptime, memory_in_bytes, memory_in_percent = resolver(process_pid)

      if self._resources:
        cpu_sample = (total_cpu_time - self._resources.cpu_total) / self._resources.cpu_total
      else:
        cpu_sample = 0.0  # we need a prior datapoint to give a sampling

      self._resources = Resources(
        cpu_sample = cpu_sample,
        cpu_average = total_cpu_time / uptime,
        cpu_total = total_cpu_time,
        memory_bytes = memory_in_bytes,
        memory_percent = memory_in_percent,
        timestamp = time.time(),
      )

      self._failure_count = 0
      return True
    except IOError as exc:
      self._failure_count += 1

      if self._use_proc:
        if self._failure_count >= 3:
          # We've failed three times resolving via proc. Warn, and fall back
          # to ps resolutions.

          self._use_proc = False
          self._failure_count = 0

          info(
            'tracker.abort_getting_resources',
            resolver = 'proc',
            response = 'falling back to ps',
            exc = exc,
          )
        else:
          debug('tracker.unable_to_get_resources', resolver = 'proc', exc = exc)
      else:
        if self._failure_count >= 3:
          # Give up on further attempts.

          info(
            'tracker.abort_getting_resources',
            resolver = 'ps',
            response = 'giving up on getting resource usage information',
            exc = exc,
          )

          self.stop()
        else:
          debug('tracker.unable_to_get_resources', resolver = 'ps', exc = exc)

      return False


class PortUsageTracker(Daemon):
  """
  Periodically retrieves the processes using a set of ports.
  """

  def __init__(self, rate):
    super(PortUsageTracker, self).__init__(rate)

    self._last_requested_ports = []
    self._processes_for_ports = {}
    self._failure_count = 0  # number of times in a row we've failed to get results

  def get_processes_using_ports(self, ports):
    """
    Registers a given set of ports for further lookups, and returns the last
    set of 'port => process' mappings we retrieved. Note that this means that
    we will not return the requested ports unless they're requested again after
    a successful lookup has been performed.

    :param list ports: port numbers to look up

    :returns: **dict** mapping port numbers to the process using it
    """

    self._last_requested_ports = ports
    return self._processes_for_ports

  def _task(self, process_pid, process_name):
    ports = self._last_requested_ports

    if not ports:
      return True

    result = {}

    # Use cached results from our last lookup if available.

    for port, process in self._processes_for_ports.items():
      if port in ports:
        result[port] = process
        ports.remove(port)

    try:
      result.update(_process_for_ports(ports, ports))

      self._processes_for_ports = result
      self._failure_count = 0
      return True
    except IOError as exc:
      self._failure_count += 1

      if self._failure_count >= 3:
        info('tracker.abort_getting_port_usage', exc = exc)
        self.stop()
      else:
        debug('tracker.unable_to_get_port_usages', exc = exc)

      return False
