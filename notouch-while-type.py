#!/usr/bin/env python3
#
#   notouch-while-type: disable touchpad while you type
#   Copyright (C) 2022  idlehands99
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License along
#   with this program; if not, write to the Free Software Foundation, Inc.,
#   51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
#   https://github.com/idlehands99/notouch-while-type.git
#
# $Id$


# standard libraries
import argparse
import ctypes
import errno
import fcntl
import fnmatch
import glob
import io
import json
import logging
import logging.handlers
import os
import os.path
import random
import selectors
import signal
import stat
import sys
import time
# we use two external libraries. you can install them by
# 'sudo apt install python3-libevdev python3-pyudev' on ubuntu.
import libevdev
import pyudev

# we also use 'column' external command in notouch_listdevices()
# to print device tables.
# 'column' is in 'bsdmainutils' package on ubuntu, installed
# under standard installation.


__version__ = '1.0.0'
NOTOUCH_VERSION_TUPLE = tuple(__version__.split('.'))

CONFIGDIR = os.environ.get('NOTOUCH_WHILE_TYPE_CONFIGDIR', '/etc/notouch-while-type')
RUNDIR = os.environ.get('NOTOUCH_WHILE_TYPE_RUNDIR', '/run/notouch-while-type')


class input_event(ctypes.Structure):
    """struct input_event from linux/input.h."""
    # from /usr/include/linux/input.h
    #
    # struct input_event {
    #     struct timeval time;
    #     unsigned short type;
    #     unsigned short code;
    #     unsigned int value;
    # };
    #
    # from sys/time.h
    #
    # struct timeval {
    #    time_t      tv_sec;     /* seconds */
    #    suseconds_t tv_usec;    /* microseconds */
    # };
    #
    # time_t and suseconds_t are both 'long'
    #
    # libevdev (and obviously python-libevdev too) cannot handle hot-
    # unplugging of devices. so we use python-libevdev for ioctl-s but do i/o
    # operations by ourselves. good thing libevdev wouldn't do anything
    # unless you specifically asked to.
    #
    # this _fields_ is identical to those used in python-libevdev code.
    # so it should be as safe as python-libevdev itself. and this code
    # depends on python-libevdev, so we're not compromising anything :)
    _fields_ = [
        ("sec",   ctypes.c_long),
        ("usec",  ctypes.c_long),
        ("type",  ctypes.c_uint16),
        ("code",  ctypes.c_uint16),
        ("value", ctypes.c_uint32),
    ]
    # this means on some 32-bit devices, time_t is still a 32-bit integer.
    # recent os releases provides compile-time switch to 64-bit time_t for
    # applications (search for _TIME_BITS=64 or __USE_TIME_BITS64), but this
    # is going to be hell for ctypes users, as we slowly approaching to Y2038.
    # by the way, python-libevdev sets event device's clock-id (EVIOCSCLOCKID)
    # to monotonic, so at least we don't have to worry about the overflow.
    #
    # usage:
    #   input_event(1234, 567890, libevdev.EV_KEY.value, libevdev.EV_KEY.KEY_A.value, 1)
    #   input_event(sec=1234, usec=567890, type=libevdev.EV_KEY.value, code=libevdev.EV_KEY.KEY_A.value, value=1)
    #   input_event.from_buffer_copy(eventfile.read(ctypes.sizeof(input_event)))

    @property
    def time(self):
        """Returns the event time as a float."""
        return self.sec + self.usec / 1000000

    def InputEvent(self):
        """Returns libevdev.InputEvent translation of myself."""
        return libevdev.InputEvent(libevdev.evbit(self.type, self.code), self.value, self.sec, self.usec)


class ConfigError(RuntimeError):
    pass

class notouch_config(object):
    """notouch-while-type configuration object.

    raises: ConfigError (RuntimeError)
    """

    SIGNITURE = "notouch-while-type configuration"
    FORMATVERSION = 1
    DEVICEOPTS = ("name", "id", "usb", "uniq", "dev")

    def __init__(self, path=None, file=None, string=None):
        """Initialize from file path, file object or string."""
        self.clear()
        if path:
            try:
                with open(path) as f:
                    self.load(f)
            except OSError as e:
                raise ConfigError(str(e))
            except RuntimeError as e:
                raise ConfigError(path + ': ' + str(e))
        elif file:
            self.load(file)
        elif string is not None:
            self.loads(string)

    def clear(self):
        self.keyboard_devices = {}
        self.keyboard_ignore = ()
        self.keyboard_timeout = 0.0
        self.pointer_devices = {}

    def load(self, f, flock=True):
        """Load config from a file object.

        we expect writers of the config file do the following:
          fcntl.flock(f, LOCK_EX);
          f.write(json_buffer);
          fcntl.flock(f, LOCK_UN);
        or, in case of shell scripts:
          flock -x notouch.conf -c 'cat json_source >notouch.conf'
        """
        try:
            if flock:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                self.loads(f.read())
            finally:
                if flock:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError as e:
            raise ConfigError(str(e))

    def loads(self, s):
        """Load config from string."""
        try:
            self.clear()
            cfg = json.loads(s)
            if cfg.get("format") != self.SIGNITURE:
                raise ValueError("not a notouch configuration")
            if cfg.get("formatversion") != self.FORMATVERSION:
                raise ValueError("notouch configuration version != " + str(self.FORMATVERSION))

            keyboard = cfg.get("keyboard")
            if not isinstance(keyboard, dict):
                raise ValueError("'keyboard' is not an object")

            devices = keyboard.get("devices")
            if not isinstance(devices, dict):
                raise ValueError("'keyboard/devices' is not an object")
            for (devopt, devlist) in devices.items():
                if devopt in self.DEVICEOPTS:
                    if not isinstance(devlist, list):
                        raise ValueError("'keyboard/devices/%s' is not an array" % devopt)
                    devs = []
                    for i in devlist:
                        if not isinstance(i, str):
                            raise ValueError("non-string in 'keyboard/devices/%s'" % devopt)
                        devs.append(i)
                    self.keyboard_devices[devopt] = tuple(sorted(set(devs)))

            ignore = keyboard.get("ignore")
            if not isinstance(ignore, list):
                raise ValueError("'keyboard/ignore' is not an array")
            # convert key names to key codes
            codes = []
            for i in ignore:
                if not isinstance(i, str):
                    raise ValueError(str(i) + ": non-string in 'keyboard/ignore' key list")
                key = libevdev.evbit('EV_KEY', i)
                if not key:
                    raise ValueError(i + ": wrong key symbol in 'keyborad/ignore'")
                codes.append(key.value)
            self.keyboard_ignore = tuple(sorted(set(codes)))

            timeout = keyboard.get("timeout")
            if not isinstance(timeout, (int, float)):
                raise ValueError(repr(timeout) + ": non-number in 'keyboard/timeout'")
            self.keyboard_timeout = timeout

            pointer = cfg.get("pointer")
            if not isinstance(pointer, dict):
                raise ValueError("'pointer' is not an object")

            devices = pointer.get("devices")
            if not isinstance(devices, dict):
                raise ValueError("'pointer/devices' is not an object")
            for (devopt, devlist) in devices.items():
                if devopt in self.DEVICEOPTS:
                    if not isinstance(devlist, list):
                        raise ValueError("'pointer/devices/%s' is not an array" % devopt)
                    devs = []
                    for i in devlist:
                        if not isinstance(i, str):
                            raise ValueError("non-string in 'pointer/devices/%s'" % devopt)
                        devs.append(i)
                    self.pointer_devices[devopt] = tuple(sorted(set(devs)))
        except (ValueError, json.JSONDecodeError) as e:
            raise ConfigError(str(e))

    @classmethod
    def args_devices(cls, args):
        """build a devices criteria dict from argparse-result namespace object."""
        devices = {}
        for devopt in cls.DEVICEOPTS:
            devlist = getattr(args, devopt)
            if isinstance(devlist, list):
                devices[devopt] = tuple(sorted(set(x for x in devlist if isinstance(x, str))))
        return devices


class devselect_context(object):
    """list event devices or choose by criteria."""

    def __init__(self, logger=None):
        self.logger = logger
        self._udevctx = None
        self._all_devices = None

    @property
    def udevctx(self):
        if self._udevctx is None:
            self._udevctx = pyudev.Context()
        return self._udevctx

    # a value in input_id.bustype
    BUS_CODE_USB = 0x03

    # this is a map to follow udev tree down, from usb-device to eventX
    USB_TO_EVENT = [
        {   # this is /dev/bus/usb/NNN/NNN, usb-device
            'device_type': 'usb_device',
            'driver': 'usb',
            'subsystem': 'usb',
        },
        {   # this is usb-interface
            'device_type': 'usb_interface',
            'subsystem': 'usb',
        },
        None,   # this means wild-card '*'
        {   # this is inputX
            'device_type': None,
            'driver': None,
            'subsystem': 'input',
        },
        {   # this is eventX, our goal, which should have device node
            'device_type': None,
            'driver': None,
            'subsystem': 'input',
        },
    ]
    USB_DEVICE = USB_TO_EVENT[0]

    @staticmethod
    def udevtype_match(udev, criteria):
        """Returns whether udev (pyudev.Device) satisfies criteria (an entry from USB_TO_EVENT.)"""
        for i in criteria:
            if getattr(udev, i) != criteria[i]:
                return False
        return True

    def device_record(self, path):
        """device record represents an event device we may have interested in.

        It's an dict with keys 'name'(name), 'node'(device node),
        'TYPE'('KEY', or 'MOUSE'), (from here on optional) 'id'(event device id),
        'usbdev'(usb BUS#/DEV#), and 'uniq'(BT address).
        """
        try:
            s = os.stat(path)
            if not stat.S_ISCHR(s.st_mode):
                raise RuntimeError("not a character device")
            # we report error on 'open', everything else is just an exception
            try:
                f = open(path, 'rb')
            except OSError as e:
                if self.logger:
                    self.logger.warning(str(e))
                raise
            try:
                edev = libevdev.Device(f)
                name, id, uniq = edev.name, edev.id, edev.uniq
            finally:
                f.close()
            udev = pyudev.Devices.from_device_file(self.udevctx, path)
            type = None
            for i in ('ID_INPUT_MOUSE', 'ID_INPUT_KEY'):
                if udev.properties.get(i) == '1':
                    type = i[len('ID_INPUT_'):]
                    break
            devdict = { 'name': name, 'node': path }
            if id is not None and tuple(id.values()) != (0, 0, 0, 0):
                devdict['id'] = "%04x:%04x:%04x:%04x" % tuple(id[x] for x in ('bustype', 'vendor', 'product', 'version'))
            if type:
                devdict['TYPE'] = type
            if id['bustype'] == self.BUS_CODE_USB:
                for i in udev.ancestors:
                    if self.udevtype_match(i, self.USB_DEVICE):
                        if i.device_node.startswith('/dev/bus/usb/'):
                            devdict['usbdev'] = i.device_node[len('/dev/bus/usb/'):]
                        break
            if uniq is not None:
                devdict['uniq'] = uniq
            return devdict
        except (OSError, pyudev.DeviceNotFoundError) as e:
            raise RuntimeError(str(e))

    @property
    def all_devices(self):
        """An list of all event devices (records) available."""
        if self._all_devices is None:
            self._all_devices = []
            for path in glob.glob('/dev/input/*'):
                try:
                    self._all_devices.append(self.device_record(path))
                except RuntimeError:
                    pass
        return self._all_devices

    def down_udev_tree(self, udev, udevpool, criteria_list, matched):
        """Subroutine of match_by_usbdev.

        Goes one step down udev device tree, along USB_TO_EVENT map.
        """
        if len(criteria_list) > 0:
            for i in [x for x in udevpool if x.parent == udev]:
                n = 1 if criteria_list[0] is None else 0
                if self.udevtype_match(i, criteria_list[n]):
                    if len(criteria_list) > n + 1:
                        self.down_udev_tree(i, udevpool, criteria_list[n + 1:], matched)
                    else:
                        matched.append(i)
                elif criteria_list[0] is None:
                    self.down_udev_tree(i, udevpool, criteria_list, matched)

    def match_by_usbdev(self, usbdev):
        """Maps USB bus position (BUS#/DEV#) to event devices.

        returns a list of event device records those corresponds to specified
        USB device.
        """
        devices = []
        for path in glob.glob(os.path.join('/dev/bus/usb', usbdev)):
            try:
                udev = pyudev.Devices.from_device_file(self.udevctx, path)
                if self.udevtype_match(udev, self.USB_TO_EVENT[0]):
                    # follow device tree usb_device => usb_interface => * => input/input => input/event
                    descendants = tuple(pyudev.Enumerator(self.udevctx).match(parent=udev))
                    matched = []
                    self.down_udev_tree(udev, descendants, self.USB_TO_EVENT[1:], matched)
                    for i in matched:
                        try:
                            devices.append(self.device_record(i.device_node))
                        except RuntimeError:
                            pass
            except (OSError, RuntimeError, pyudev.DeviceNotFoundError):
                pass
        return devices

    def match_by(self, key, val):
        """returns a list of device records those match 'key' and 'val'.

        'key' is one of notouch_config.DEVICEOPTS ('name', 'id', ...)
        'val' is corresponding criterion string ('/dev/input/event...', '0123:4567:*', ...)
        """
        return [ x for x in self.all_devices if x.get(key) is not None and fnmatch.fnmatch(x[key], val) ]

    def match_devices(self, devicedict):
        """Get a devicedict, returns device matching dictionary.

        Devicedict is a criteria dictionary just like notouch_config.device.

        Retruns:
            a dictionary that maps request criterion (key) to a list of
            matched event device records (value). Input criteria dictionary's
            "request_key:request_value" becomes this dictionary's key.
        """
        matchdict = {}
        for (devopt, vallist) in devicedict.items():
            if devopt == "dev":
                matchdict[devopt] = [x for y in vallist for x in self.match_by('node', y)]
            else:
                for value in vallist:
                    if devopt == "usb":
                        devs = self.match_by_usbdev(value)
                    else:
                        devs = self.match_by(devopt, value)
                    matchdict["%s:%s" % (devopt, value)] = devs
        # convert values to device node path names
        for i in matchdict.keys():
            matchdict[i] = sorted(set(x['node'] for x in matchdict[i]))
        return matchdict


class daemon(object):
    """*nix daemon class.

    Inspired by many samples on net.
    Usage resembles 'threading.Thread' object.
    To use it, subclass it and implement run() method.

    The idea is to use flock(2) on pid file to synchronize processes around
    daemon. Daemon acquires exclusive lock on it before it writes pid, then
    changes it to shared lock and keeps it that way its whole life.
    When you call daemon.start(), it blocks until daemon's run() (or
    target()) calls daemon.daemon_ready(), so daemon can prepare whatever
    necessary (sockets, signals, etc) before it returns.
    When you call daemon.kill(wait=True) with wait option, it would block
    until daemon dies.

    Usage:
        ## define subclass:
            class MyDaemon(daemon):
                def run():
                    prepare_everything()
                    self.daemon_ready() # caller's daemon.start() blocks
                                        # until we call this.
                                        # make sure you prepare everything
                                        # needed before you call it.
                    while go_on:
                        do_some_work()
                    return 0    # this becomes daemon's exit code

        ## then, server side (server api is start() only):
            d = MyDaemon('/run/mydaemon.pid')
            try:
                d.start()
                sys.stdout.write("daemon pid = " + str(d.pid()) + "\\n")
                os._exit(0) # beware, when MyDaemon is set not to fork
                            # (fork==False and session!=SESSION_DETACH),
                            # start() returns its control after daemon
                            # process finihsed. in that case you may want
                            # to call sys.exit(), not os._exit().
            except daemon.DaemonError as e:
                sys.stderr.write(str(e) + "\\n")
                sys.exit(1)

        ## and, client side (client apis are pid() and kill()):
            try:
                MyDaemon('/var/run/mydaemon.pid').kill(SIGHUP)
            except daemon.DaemonError as e:
                sys.stderr.write(str(e) + "\\n")

    Requires: errno, fcntl, io, os, os.path, random, signal, sys, time

    This class runs on both python2 and python3.
    This code is not thread safe. Use it from main thread only. You can
    create any number of thread from daemon.run() as you like.
    When you use timeout options (pidf_locktimeout in constructor, timeout
    in pid(), kill() and is_alive()), it will use SIGALRM. Make sure you
    don't mix it up.
    """

    class DaemonError(Exception):
        pass

    class NotRunningError(DaemonError):
        """Daemon not ruuning -- (mostly) client side error."""
        def __init__(self, name):
            super(daemon.NotRunningError, self).__init__("daemon " + name + " not running")
            self.name = name

    class AlreadyRunningError(DaemonError):
        """Daemon already running -- server side error.

        Attributes:  pid: process id of the running daemon.
        """
        def __init__(self, name, pid):
            super(daemon.AlreadyRunningError, self).__init__("daemon %s (pid = %d) already running" % (name, pid))
            self.name = name
            self.pid = pid

    class LaunchFailedError(DaemonError):
        """Launching of a daemon process failed -- server and client side error."""
        def __init__(self, name):
            super(daemon.LaunchFailedError, self).__init__("%s faild launching daemon process" % name)
            self.name = name

    class CannotLockError(DaemonError):
        """Timeout on locking pid file -- server side error."""
        def __init__(self, name, pidfile):
            super(daemon.CannotLockError, self).__init__("%s cannot get file lock on %s" % (name, pidfile))
            self.name = name
            self.pidfile = pidfile

    # session enums
    SESSION_NONE = 0        # Do not created session
    SESSION_CREATE = 1      # Create new session but do not detach
    SESSION_DETACH = 2      # Create and detach session

    def __init__(self, pidfile, pidf_mode=0o644, pidf_locktimeout=-1,
                 name=None, fork=True, session=SESSION_DETACH,
                 chdir='/', detach_stdio=True,
                 target=None, args=(), kwargs={}):
        """Constructor.

        Args:
        pidfile (str):              Pid file path, required.
        pidf_mode (int):            Pid file mode on create. default=0o644.
        pidf_locktimeout (number):  Time limit on pid file lock. (this affect
                                only on start(). pid(), kill() and is_alive()
                                have thier own timeout argument.)
                                default=-1 (no limit -- try forever).
        name (str):                 Instance name, default=sys.argv[0].
        fork (bool):                Fork or not daemon process, pass False for
                                debugging. default=True.
        session (int):              One of: SESSION_NONE (0: do not create
                                session), SESSION_CREATE (1: create new session
                                but do not detach), SESSION_DETACH (2: create
                                and detach, which also means it will fork).
                                default=SESSION_DETACH (create and detach)
        chdir (str):                Change working directory to this one, pass
                                'None' not to change directory. default='/'.
        detach_stdio (bool):        Redirect stdin, stdout, stderr to
                                /dev/null. default=True.
        target (callable):          If you prefer not to subclass this, pass
                                daemon function here. default=None.
        args (list/tuple):          Positional arguments for 'target'.
                                default=() (empty).
        kwargs (dict):              Keyword arguments for 'target'.
                                defualt={} (empty).
        """
        self.pidf_path        = os.path.abspath(pidfile)
        self.pidf_mode        = pidf_mode
        self.pidf_locktimeout = pidf_locktimeout
        self.pidfd            = None
        self.name             = (name or os.path.basename(sys.argv[0]))
        self.fork             = fork
        self.session          = session
        self.chdir            = chdir
        self.detach_stdio     = detach_stdio
        self.target           = target
        self.args             = args
        self.kwargs           = kwargs

    # alarm exception
    class AlarmInterrupt(Exception):
        """An internal exception to propagate SIGALRM."""
        pass

    # alarm signal handler
    @classmethod
    def alarmhandler(cls, signal, frame):
        """Internal signal hander for SIGALRM."""
        cls.signal.signal(cls.signal.SIGALRM, cls.signal.SIG_IGN)
        raise cls.AlarmInterrupt()

    def start(self):
        """Start daemon.

        Setups daemon environments and runs a daemon with run().
        When start() returns, it is guaranteed the pid file has been
        written.

        Returns:
            If you let it fork, then 0.
            Otherwise whatever run() returns.
            It forks when (fork==True or session==SESSION_DETACH)

        Raises:
            IOError, OSError:
                Something wrong with opening, creating or locking the pid
                file, or you don't have permission to send signals to
                running daemon.
            AlreadyRunningError:
                There's an already running daemon. Its pid is in
                'AlreadyRunningError.pid'.
            CannotLockError:
                Timeout. Couldn't get exclusive lock within
                'pidf_locktimeout' period.
            LaunchFailedError:
                Creating daemon process failed.
        """

        # create and lock pid file, truncate it, but not write pid yet.

        if self.pidfd is not None:
            raise self.AlreadyRunningError(self.name, os.getpid())
        # temporarily assign umask 0 while creating pid file
        last_umask = os.umask(0)
        # open pid file. do NOT destroy existing one.
        try:
            pidfd = os.open(self.pidf_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, self.pidf_mode)
        finally:
            os.umask(last_umask)

        # try get flock on pid file, optionally under time constraint
        last_alarm = None
        try:
            flock_flag = 0
            # setup time constraint
            if self.pidf_locktimeout > 0:
                last_alarm = signal.signal(signal.SIGALRM, self.alarmhandler)
                signal.alarm(self.pidf_locktimeout)
            elif self.pidf_locktimeout == 0:
                flock_flag = fcntl.LOCK_NB
            # try-flock loop
            while True:
                # first try shared lock
                fcntl.flock(pidfd, fcntl.LOCK_SH | flock_flag)
                # got shared lock, verify if there's already a running daemon
                try:
                    with io.open(pidfd, 'r', closefd=False) as f:
                        pid = int(f.read())
                        if self.is_alive(pid):
                            raise self.AlreadyRunningError(self.name, pid)
                except ValueError:
                    # something wrong with pid file contents -- going to overrun
                    pass
                # raise lock to exclusive -- nonblocking
                try:
                    fcntl.flock(pidfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break   # got exclusive lock
                except (IOError, OSError) as e:
                    if e.errno != errno.EWOULDBLOCK or self.pidf_locktimeout == 0:
                        raise
                # was EWOULDBLOCK -- race condition? -- hand out lock, sleep
                # short period of time, try again
                fcntl.flock(pidfd, fcntl.LOCK_UN)
                t = self.pidf_locktimeout / 10 if self.pidf_locktimeout > 0 else 1.0
                time.sleep(random.uniform(0.01, max(min(t, 1.0), 0.02)))
        except Exception as e:
            os.close(pidfd)
            if isinstance(e, self.AlarmInterrupt):
                raise self.CannotLockError(self.name, self.pidf_path)
            elif isinstance(e, (IOError, OSError)) and e.errno == errno.EWOULDBLOCK:
                raise self.CannotLockError(self.name, self.pidf_path)
            else:
                raise
        finally:
            if last_alarm is not None:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, last_alarm)

        # truncate pid file as we've got an exclusive lock
        self.pidfd = pidfd
        os.lseek(self.pidfd, 0, os.SEEK_SET)
        os.ftruncate(self.pidfd, 0)

        # try fist fork
        if self.fork:
            if os.fork() != 0:
                # either single fork or double fork, I am the parent
                os.close(self.pidfd)
                self.pidfd = None
                # is_alive() will open pid file and acquire shared lock. this
                # will block until daemon process calls daemon_ready() (or
                # die).
                if not self.is_alive():
                    raise self.LaunchFailedError(self.name)
                return 0    # parent process return

        # become session leader
        if self.session >= self.SESSION_CREATE and os.getpgid(0) != os.getpid():
            os.setsid()

        # set working directory
        if self.chdir:
            os.chdir(self.chdir)

        # redirect stdin, stdout, and stderr to /dev/null
        if self.detach_stdio:
            sys.stdin.flush()
            sys.stdout.flush()
            sys.stderr.flush()
            nullfd = os.open(os.devnull, os.O_RDWR)
            os.dup2(nullfd, 0)
            os.dup2(nullfd, 1)
            os.dup2(nullfd, 2)
            os.close(nullfd)

        # try second fork (or first fork if fork==False)
        if self.session >= self.SESSION_DETACH:
            if os.fork() != 0:
                # I am either parent or first child
                os.close(self.pidfd)
                self.pidfd = None
                if self.fork:   # I am first child
                    os._exit(0)    # first child exit
                else:           # I am parent
                    # is_alive() will block until daemon process calls
                    # daemon_ready() (or die).
                    if not self.is_alive():
                        raise self.LaunchFailedError(self.name)
                    return 0    # parent process return

        # do the main job
        rcode = self.run()

        # clean up
        self.cleanup()
        if self.fork or self.session >= self.SESSION_DETACH:
            sys.exit(rcode)
        else:
            return rcode

    def daemon_ready(self):
        """Inside server api to release daemon.start() from blocking.

        call it from your overridden daemon.run(), after you've prepared
        all necessary resources to run daemon.
        """
        if not self.pidfd:
            raise self.NotRunningError(self.name)
        if os.lseek(self.pidfd, 0, os.SEEK_CUR) != 0:
            # we've already written pid
            raise self.AlreadyRunningError(self.name, os.getpid())
        # write out pid
        os.write(self.pidfd, (str(os.getpid()) + "\n").encode())
        # drop lock to shared
        fcntl.flock(self.pidfd, fcntl.LOCK_SH)

    def run(self):
        """Override this to implement daemon job.

        Call self.daemon_ready() to resume parent's start(), so that it
        knows daemon is ready.
        If your overridden run() exits a program without returning, you may
        want to call cleanup() before exit.
        In case you didn't override this, it calls 'target' given to the
        constructor. Don't forget to call daemon.daemon_ready() from it.
        In case of target(), we don't provide daemon instance in its
        arguments, you have to provide it yourself (to be able to call
        daemon.daemon_ready())
        """
        if self.target:
            return self.target(*self.args, **self.kwargs)
        return 0

    def cleanup(self):
        """Clean up pid file."""
        if self.pidfd is None:
            raise self.NotRunningError(self.name)
        #
        try:
            try:
                os.remove(self.pidf_path)
            except Exception:
                pass
            if not self.fork and self.session < self.SESSION_DETACH:
                # we didn't fork. try closing pid file before returning to caller.
                try:
                    os.close(self.pidfd)
                except Exception:
                    pass
            # if we did fork, then take shared-locked pid file to the grave...
        finally:
            self.pidfd = None

    def pid(self, timeout=-1):
        """Returns running daemon's process id as integer.

        Raises:
            NotRunningError:  No daemon running.
            CannotLockError:  Couldn't get pid file lock. Daemon stuck at startup?
        """
        if self.pidfd is not None:
            return os.getpid()

        last_alarm = None
        flock_flag = 0
        try:
            if timeout > 0:
                last_alarm = signal.signal(signal.SIGALRM, self.alarmhandler)
                signal.alarm(timeout)
            elif timeout == 0:
                flock_flag = fcntl.LOCK_NB
            # open pid file
            with open(self.pidf_path) as pidf:
                fcntl.flock(pidf, fcntl.LOCK_SH | flock_flag)
                return int(pidf.read())
        except (IOError, OSError) as e:
            if e.errno == errno.ENOENT:
                raise self.NotRunningError(self.name)
            elif e.errno == errno.EWOULDBLOCK:
                raise self.CannotLockError(self.name, self.pidf_path)
            else:
                raise
        except ValueError:
            raise self.NotRunningError(self.name)
        except self.AlarmInterrupt:
            raise self.CannotLockError(self.name, self.pidf_path)
        finally:
            if last_alarm is not None:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, last_alarm)

    def kill(self, sig=signal.SIGTERM, pid=None, wait=False, timeout=-1):
        """Send a signal to already running daemon.

        Raises:
            NotRunningError:  No daemon running.
            CannotLockError:  Couldn't get pid file lock. Daemon stuck at startup?
        """
        last_alarm = None
        pidf = None
        try:
            if timeout > 0:
                last_alarm = signal.signal(signal.SIGALRM, self.alarmhandler)
                signal.alarm(timeout)
            if pid is None:
                pid = self.pid(timeout=(0 if timeout == 0 else -1))
            if wait:
                pidf = open(self.pidf_path) # open it before we send segnal
            os.kill(pid, sig)
            if wait:
                # daemon keeps shared lock. acquiring exclusive lock blocks
                # until daemon dies.
                fcntl.flock(pidf, fcntl.LOCK_EX)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ESRCH):
                raise self.NotRunningError(self.name)
            else:
                raise
        except self.AlarmInterrupt:
            raise self.CannotLockError(self.name, self.pidf_path)
        finally:
            if pidf is not None:
                pidf.close()
            if last_alarm is not None:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, last_alarm)

    def is_alive(self, pid=None, timeout=-1):
        """Tells if the daemon is alive.

        This sends signal 0 to the supposed process.
        """
        try:
            self.kill(sig=0, pid=pid, wait=False, timeout=timeout)
            return True
        except self.NotRunningError:
            return False


class notouch_daemon(daemon):
    class TerminateInterrupt(Exception):
        pass

    class ReconfigureInterrupt(Exception):
        pass

    def sigterminate(self, sig, frame):
        """Signal hander to terminate."""
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGQUIT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if self.logger:
            self.logger.debug('sigterminate: signal ' + str(sig))
        raise self.TerminateInterrupt('signal ' + str(sig))

    def sigreconfigure(self, sig, frame):
        """Signal handler to reconfigure."""
        raise self.ReconfigureInterrupt('signal ' + str(sig))
        if self.logger:
            self.logger.debug('sigreconfigure: signal ' + str(sig))
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def excepthook(self, type, value, traceback):
        """Exception hook to log an error."""
        if self.logger:
            self.logger.error(traceback.format_exception(type, value, traceback))

    def __init__(self, name, config, check_config=False, foreground=False, logger=None):
        """Constructor."""
        # make every path names absolute before we chdir
        pidfilepath = os.path.abspath(os.path.join(RUNDIR, config + '.pid'))
        self.configpath = os.path.abspath(os.path.join(CONFIGDIR, config + '.conf'))
        if check_config:
            # raise error if config is invalid
            notouch_config(self.configpath)
        # build arguments to parent's __init__
        kwargs = { 'name': name }
        if foreground:
            kwargs['fork']         = False
            kwargs['session']      = self.SESSION_NONE
            kwargs['detach_stdio'] = False
        super(notouch_daemon, self).__init__(pidfilepath, **kwargs)
        # setup attributes
        self.logger = logger
        self.selector = None
        self.kbd_devices = {}   # fileobj => (device_name, evdevdevice) of devices in use.
        self.ptr_devices = {}   # same as above
        self.timeout = 0
        self.ignore_keys = ()

    def configure_devices(self):
        """Read config file, find event devices, and open them.

        as a result, kbd_devices and ptr_devices are constructed.
        both are dictionaries, keys are python-file-object of the event
        devices, values are (path, libevdev.Device) tuples.
        """
        if self.logger:
            self.logger.debug('configure_devices')
        self.kbd_devices.clear()
        self.ptr_devices.clear()
        self.timeout = 0
        self.ignore_keys = ()
        try:
            config = notouch_config(self.configpath)
            self.timeout = config.keyboard_timeout
            self.ignore_keys = config.keyboard_ignore
            devselector = devselect_context(logger=self.logger)
            kmatch = devselector.match_devices(config.keyboard_devices)
            pmatch = devselector.match_devices(config.pointer_devices)
            if self.logger:
                for (criterion, evdevs) in list(kmatch.items()) + list(pmatch.items()):
                    self.logger.info("<" + criterion + "> matched [" + ", ".join(evdevs) + "]")
            kdevs = tuple(sorted(set(x for y in kmatch.values() for x in y)))
            pdevs = tuple(sorted(set(x for y in pmatch.values() for x in y)))
            if self.timeout <= 0:
                if self.logger:
                    self.logger.warning("no timeout given; going to sleep until reconfigure")
            else:
                for names, devdict in ((kdevs, self.kbd_devices), (pdevs, self.ptr_devices)):
                    for path in names:
                        try:
                            f = open(path, 'rb')
                            fcntl.fcntl(f, fcntl.F_SETFL, os.O_NONBLOCK)
                            edev = libevdev.Device(f)
                            self.selector.register(f, selectors.EVENT_READ)
                            devdict[f] = (path, edev)
                        except (IOError, OSError) as e:
                            if self.logger:
                                self.logger.error(str(e))
                if len(self.kbd_devices) <= 0 or len(self.ptr_devices) <= 0:
                    if self.logger:
                        self.logger.warning("no %s devices available; going to sleep until reconfigure" % \
                                                " or ".join((["keyboard"] if len(self.kbd_devices) <= 0 else []) + \
                                                             (["pointer"] if len(self.ptr_devices) <= 0 else [])))
                    self.close_devices()
        except (IOError, OSError, ValueError, RuntimeError) as e:
            if self.logger:
                self.logger.error(str(e))

    def close_devices(self, files=None):
        """All devices stored in kbd_devices and ptr_devices are closed."""
        if self.logger:
            self.logger.debug('close_devices')
        if files is None:
            files = list(self.kbd_devices.keys()) + list(self.ptr_devices.keys())
        for f in files:
            path, edev = '', None
            if f in self.kbd_devices:
                path, edev = self.kbd_devices[f]
                del self.kbd_devices[f]
            elif f in self.ptr_devices:
                path, edev = self.ptr_devices[f]
                del self.ptr_devices[f]
            self.selector.unregister(f)
            try:
                f.close()
            except (IOError, OSError) as e:
                if self.logger:
                    self.logger.error(path + ': ' + str(e))

    @staticmethod
    def event_time(ev):
        return ev.sec + ev.usec / 1000000

    def read_device(self, file):
        """Read as much event as possible from event device 'file'.

        returns an libevdev.InputEvent object, which is EV_KEY event and
        latest time stamp, but could be None in case no such event.
        """
        path, edev = '', None
        if file in self.kbd_devices:
            path, edev = self.kbd_devices[file]
        elif file in self.ptr_devices:
            path, edev = self.ptr_devices[file]
        try:
            last_etime = -1
            last_event = None
            while True:
                try:
                    iodata = file.read(ctypes.sizeof(input_event))
                    if iodata is None:  # == would block
                        break
                    if len(iodata) != ctypes.sizeof(input_event):
                        raise RuntimeError("read size != %d" % ctypes.sizeof(input_event))
                    ev = input_event.from_buffer_copy(iodata).InputEvent()
                    t = self.event_time(ev)
                    if ev.matches(libevdev.EV_KEY) and t >= last_etime:
                        last_etime = t
                        last_event = ev
                except (IOError, OSError) as e:
                    if e.errno == errno.EINTR:
                        pass
                    raise
            return last_event
        except (IOError, OSError, RuntimeError) as e:
            if self.logger:
                self.logger.error(path + ': ' + str(e))
            self.close_devices((file, ))
            return None

    def notouch_while_type(self):
        """wait-for-key, grab, wait-for-timeout, ungrab loop."""
        # this big loop keeps running until signals kick in.
        # after that, all devices will be closed, so don't worry about
        # exiting while devices under grab.
        while True:
            # wait for key
            last_event = None
            while last_event is None:
                events = self.selector.select()
                for key, _ in events:
                    ev = self.read_device(key.fileobj)
                    if ev and key.fileobj in self.kbd_devices and \
                            ev.matches(libevdev.EV_KEY) and \
                            ev.code.value not in self.ignore_keys and \
                            (last_event is None or self.event_time(ev) > self.event_time(last_event)):
                        last_event = ev
            # grab
            if self.logger:
                self.logger.debug("grub; time = %f" % self.event_time(last_event))
            for file, pair in list(self.ptr_devices.items()):
                path, edev = pair
                try:
                    edev.grab()
                except (OSError, libevdev.DeviceGrabError) as e:
                    if self.logger:
                        self.logger.error(path + ': ' + str(e))
                    self.close_devices((file, ))
            # wait for timeout
            while True:
                timeout = self.event_time(last_event) + self.timeout - time.monotonic()
                if timeout <= 0:
                    break
                events = self.selector.select(timeout=timeout)
                for key, _ in events:
                    ev = self.read_device(key.fileobj)
                    if ev and key.fileobj in self.kbd_devices and \
                            ev.matches(libevdev.EV_KEY) and \
                            ev.code.value not in self.ignore_keys and \
                            self.event_time(ev) > self.event_time(last_event):
                        last_event = ev
            # ungrab
            if self.logger:
                self.logger.debug("ungrub; time = %f" % time.monotonic())
            for file, pair in list(self.ptr_devices.items()):
                path, edev = pair
                try:
                    edev.ungrab()
                except (OSError, libevdev.DeviceGrabError) as e:
                    if self.logger:
                        self.logger.error(path + ': ' + str(e))
                    self.close_devices((file, ))

    def run(self):
        """daemon loop."""
        if self.logger:
            self.logger.info(self.name + ' running')

        last_excepthook = None
        # run until TerminateInterrupt
        try:
            # set up system exception handler
            if self.detach_stdio:
                last_excepthook = sys.excepthook
                sys.excepthook = self.excepthook
            # set up signal handlers
            signal.signal(signal.SIGINT, self.sigterminate)
            signal.signal(signal.SIGQUIT, self.sigterminate)
            signal.signal(signal.SIGTERM, self.sigterminate)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)

            # create selector
            self.selector = selectors.DefaultSelector()

            self.daemon_ready()

            # configure-run loop
            while True:
                try:
                    signal.signal(signal.SIGHUP, self.sigreconfigure)
                    # read config and select devices
                    self.configure_devices()
                    if len(self.kbd_devices) > 0 and len(self.ptr_devices) > 0 and self.timeout > 0:
                        # notouch_while_type() won't come back until signal breaks
                        self.notouch_while_type()
                    else:
                        signal.pause()
                except self.ReconfigureInterrupt as e:
                    if self.logger:
                        self.logger.info('reconfiguring by ' + str(e))
                finally:
                    self.close_devices()
        except self.TerminateInterrupt as e:
            if self.logger:
                self.logger.info('terminated by ' + str(e))
        finally:
            if self.selector is not None:
                self.selector.close()
                self.selector = None
            if last_excepthook is not None:
                sys.excepthook = last_excepthook
        # last word
        if self.logger:
            self.logger.info(self.name + ' exit')
        return 0


def notouch_listdevices(progname, args, logger=None):
    """'list-devices' mode main.

    On this mode, this program lists all event devices eligible.
    Baisc idea is go through /dev/input/*, if the device has a key
    (ID_INPUT_KEY='1') or a pointer (ID_INPUT_MOUSE='1') in udev database,
    list it.
    """
    devices = devselect_context(logger=logger).all_devices
    for type in ('KEY', 'MOUSE'):
        print('### %s DEVICES ###' % type, flush=True)
        of = os.popen('column -t -s"\t"', 'w')
        of.write("\t".join(("NAME", "ID(BUS:VND:PRD:VER)", "UNIQ", "USB", "DEVICE-NODE")) + "\n")
        for j in (x for x in devices if x.get('TYPE') == type):
            of.write("\t".join(( ('"%s"' % j['name']) if x == 'name' else j.get(x, '-')
                                    for x in ('name', 'id', 'uniq', 'usbdev', 'node') )) + "\n")
        of.close()
        print('')
    return 0


def notouch_monitor(progname, args, logger=None):
    """'monitor devices' mode main."""

    class SignalInterrupt(Exception):
        def __init__(self, signal, frame):
            super(SignalInterrupt, self).__init__('Signal ' + str(signal))

    def sighandler(sig, frame):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGQUIT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SignalInterrupt(sig, frame)

    devreq = notouch_config.args_devices(args)
    if not devreq:
        print(progname + ": error: no device spcified", file=sys.stderr)
        return 2
    #
    try:
        matched = devselect_context(logger=logger).match_devices(devreq)
    except (OSError, ValueError, RuntimeError) as e:
        print(progname + ": error: " + str(e), file=sys.stderr)
        return 1
    for (criterion, evdevs) in matched.items():
        print("<" + criterion + "> matched [" + ", ".join(evdevs) + "]", flush=True)
    event_devices = tuple(sorted(set(x for y in matched.values() for x in y)))
    if len(event_devices) <= 0:
        print(progname + ": error: no event device found", file=sys.stderr)
        return 1
    print("using event device(s): [" + ", ".join(event_devices) + "]", flush=True)
    #
    signal.signal(signal.SIGHUP, sighandler)
    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGQUIT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)
    rcode = 0       # return code
    devices = []    # a list of device file objects
    selector = selectors.DefaultSelector()
    try:
        for devname in event_devices:
            try:
                f = open(devname, 'rb')
                fcntl.fcntl(f, fcntl.F_SETFL, os.O_NONBLOCK)
                edev = libevdev.Device(f)
            except (IOError, OSError) as e:
                raise RuntimeError(devname + ": " + str(e))
            devices.append(f)
            selector.register(f, selectors.EVENT_READ)
        #
        print("Type Ctrl-C to quit.", flush=True)
        while len(selector.get_map()) > 0:
            events = selector.select()
            for key, _ in events:
                f = key.fileobj
                try:
                    while True:
                        iodata = f.read(ctypes.sizeof(input_event))
                        if iodata is None:  # == would block
                            break
                        if len(iodata) != ctypes.sizeof(input_event):
                            raise RuntimeError("read size != %d" % ctypes.sizeof(input_event))
                        e = input_event.from_buffer_copy(iodata).InputEvent()
                        print(e.type.name, e.code.name, ctypes.c_int(e.value).value, flush=True)
                except (IOError, OSError, RuntimeError) as e:
                    print(progname + ": error: " + str(e), file=sys.stderr, flush=True)
                    selector.unregister(f)
                    devices.remove(f)
                    try:
                        f.close()
                    except (IOError, OSError):
                        pass
    except (IOError, OSError, RuntimeError) as e:
        print(progname + ": error: " + str(e), file=sys.stderr)
        rcode = 1
    except SignalInterrupt:
        pass
    finally:
        # try cleanup
        for f in devices:
            try:
                f.close()
            except (IOError, OSError):
                pass
        selector.close()
    #
    return rcode


def main(argv):
    """Disable touchpad while you type."""

    progname = os.path.basename(argv[0])

    ### build a complex argument parser ###

    # main parser
    parser = argparse.ArgumentParser(prog=progname,
                description="%(prog)s: disable touchpad while you type")
    parser.add_argument("-v", "--version", action='version',
                version='%(prog)s version ' + __version__)
    subparsers = parser.add_subparsers(help='subcommand to choose what to do', dest='verb1')

    # device options
    device_optoins = argparse.ArgumentParser(add_help=False)
    device_optoins.add_argument("--name",
                help="choose devices by its name (e.g. \"AT Translated Set 2 keyboard\")",
                type=str, nargs='+')
    device_optoins.add_argument("--id",
                help="choose devices by its id (BUS:VENDOR:PRODUCT:VERSION, USB devices can be choosen by '0003:VVVV:PPPP:*', where VVVV is vendor ID and PPPP is product ID, VERSION can usally be '*')",
                type=str, nargs='+')
    device_optoins.add_argument("--usb",
                help="choose devices by USB bus#/device# (e.g. 001/002); find it using 'lsusb' command",
                type=str, nargs='+')
    device_optoins.add_argument("--uniq",
                help="choose devices by its unique id (for bluetooth devices, an address)",
                type=str, nargs='+')
    device_optoins.add_argument("--dev",
                help="choose devices by its event device node (e.g. /dev/input/eventN)",
                type=str, nargs='+')

    # config file options
    config_options = argparse.ArgumentParser(add_help=False)
    config_options.add_argument("-c", "--config",
                help="config name (default:%(default)s)" \
                " -- config file is found in '/etc/notouch-while-type' " \
                "(or in $NOTOUCH_WHILE_TYPE_CONFIGDIR) as a file named 'CONFIG.conf'",
                type=str, default="default")

    # parser for the "daemon" command
    parser_daemon = subparsers.add_parser('daemon',
                help='start, stop or reload daemon ("help daemon" for more help)',
                description="%(prog)s: notouch-while-type daemon")
    daemon_subparsers = parser_daemon.add_subparsers(help='what to do with daemon', dest='verb2')

    # parser for the "daemon start" command
    parser_daemon_start = daemon_subparsers.add_parser('start',
                help='start daemon ("help daemon start" for more help)',
                description="%(prog)s: start notouch-while-type daemon",
                parents=[config_options])
    parser_daemon_start.add_argument("-f", "--foreground",
                help="stay forground, do not daemonize, log goes to stderr",
                action="store_true")
    parser_daemon_start.add_argument("--loglevel",
                help="log level (default:%(default)s)",
                choices=['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG', 'ALL'],
                type=str, default="INFO")

    # parser for the "daemon stop" command
    parser_stop = daemon_subparsers.add_parser('stop',
                help='tell stop to running daemon ("help daemon stop" for more help)',
                description="%(prog)s: stop notouch-while-type daemon",
                parents=[config_options])

    # parser for the "daemon reload" command
    parser_reload = daemon_subparsers.add_parser('reload',
                help='tell reload to running daemon ("help daemon reload" for more help)',
                description="%(prog)s: reload notouchpad-while-type daemon config",
                parents=[config_options])

    # parser for the "listkbd" command
    parser_listkbd = subparsers.add_parser('list-devices',
                help='list currently available keyboard and mouse devices ("help list-devices" for more help)',
                description="%(prog)s: list all event devices this program can use")

    # parser for the "monitor" command
    parser_showkeycode = subparsers.add_parser('monitor',
                help='show events from devices ("help monitor" for more help)',
                description="%(prog)s: show events from devices as you press keys or move pointers",
                parents=[device_optoins])

    # parser for the "help" command
    parser_help = subparsers.add_parser('help',
                help='show help and exit ("help help" for more help)',
                description="%(prog)s: show command help")
    parser_help.add_argument("subcommand",
                help="choose from {" + ','.join(subparsers.choices.keys()) + "}",
                type=str, nargs=argparse.REMAINDER)


    ### real job starts here ###

    args = parser.parse_args(argv[1:])
    #print(args)

    # setup logger
    logger = logging.getLogger(progname)
    stdouthandler = logging.StreamHandler()
    stdouthandler.setFormatter(logging.Formatter('%(name)s: %(message)s'))
    logger.addHandler(stdouthandler)

    if args.verb1 == 'daemon':
        # log to both stderr and syslog until we become daemon
        sysloghandler = logging.handlers.SysLogHandler(address='/dev/log', facility=logging.handlers.SysLogHandler.LOG_DAEMON)
        sysloghandler.setFormatter(logging.Formatter('%(name)s[%(process)d]: %(message)s'))
        logger.addHandler(sysloghandler)
        if args.verb2 == 'start':
            # adjust logger
            if args.foreground:
                logger.removeHandler(sysloghandler)
            else:
                logger.removeHandler(stdouthandler)
            # loglevel
            if args.loglevel:
                # convert user friendly arg to python internal
                if args.loglevel == 'ALL':
                    # supposed to set 'NOTSET', but it would disable logging when
                    # using child loggers
                    args.loglevel = 1
                logger.setLevel(args.loglevel)
            #
            try:
                # create RUNDIR, if there isn't
                if not os.path.isdir(RUNDIR):
                    os.mkdir(RUNDIR)
                # run daemon
                rcode = notouch_daemon(progname, args.config, check_config=True, foreground=args.foreground, logger=logger).start()
                # if daemon forked, then _exit
                if not args.foreground:
                    os._exit(rcode)
            except (OSError, ValueError, RuntimeError, daemon.DaemonError) as e:
                if not args.foreground:
                    logger.addHandler(stdouthandler)
                logger.error("error: " + str(e))
                if isinstance(e, ConfigError):
                    rcode = 78  # BSD/CONFIG
                else:
                    rcode = 1
        elif args.verb2 in ('stop', 'reload'):
            # signal to send, wait for death or not
            sig, wait = { 'stop': (signal.SIGTERM, True),
                          'reload': (signal.SIGHUP, False) }[args.verb2]
            try:
                notouch_daemon(progname, args.config).kill(sig, wait=wait)
                rcode = 0
            except (OSError, ValueError, RuntimeError, daemon.DaemonError) as e:
                logger.error("error: " + str(e))
                if isinstance(e, daemon.NotRunningError):
                    rcode = 7   # LSB/NOTRUNNING
                else:
                    rcode = 1
        elif args.verb2 is None:
            subparsers.choices['daemon'].print_help()
            rcode = 2   # LSB/INVALIDARGUMENT
        else:   # cannot_happen
            print("%s: error: wrong subcommand: daemon %s" % (progname, args.verb2), file=sys.stderr)
            rcode = 2   # LSB/INVALIDARGUMENT
    elif args.verb1 == 'list-devices':
        rcode = notouch_listdevices(progname, args, logger)
    elif args.verb1 == 'monitor':
        rcode = notouch_monitor(progname, args, logger)
    elif args.verb1 == 'help':
        if len(args.subcommand) == 0:
            parser.print_help()
            rcode = 0
        elif len(args.subcommand) == 1 and args.subcommand[0] in subparsers.choices:
            subparsers.choices[args.subcommand[0]].print_help()
            rcode = 0
        elif len(args.subcommand) == 2 and args.subcommand[0] == 'daemon' and \
                            args.subcommand[1] in daemon_subparsers.choices:
            daemon_subparsers.choices[args.subcommand[1]].print_help()
            rcode = 0
        else:
            parser_help.print_usage()
            print(progname + ": error: " + " ".join(args.subcommand) + ": no such subcommand", file=sys.stderr)
            rcode = 2   # LSB/INVALIDARGUMENT
    elif args.verb1 is None:
        parser.print_help()
        rcode = 2   # LSB/INVALIDARGUMENT
    else:   # cannot_happen
        print("%s: error: wrong subcommand: %s" % (progname, args.verb1), file=sys.stderr)
        rcode = 2   # LSB/INVALIDARGUMENT

    return rcode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
