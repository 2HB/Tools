# -*- python -*-
#
# Copyright 2008 - 2015 Double Fine Productions
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), 
# to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, 
# and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, 
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, 
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
#
# Speaks the android adb protocol, which is mostly "documented" in:
#
#   /android/system/core/adb/
#     file_sync_service.h
#     file_sync_service.c
#     file_sync_client.c
#     commandline.c
#

import os
import sys
import stat
import struct
import socket
from cStringIO import StringIO
from contextlib import closing
from contextlib import contextmanager
from collections import namedtuple

from android.utils import AdbError, ProtocolError

ADB_PORT = 5037
SYNC_DATA_MAX = (64*1024)       # hardcoded in file_sync_service.h

# I don't know what "adb get-state" reports for the other states, so I'm
# leaving them undefined for now.
# CS_OFFLINE, CS_HOST, CS_RECOVERY, CS_NOPERM, CS_SIDELOAD
CS_DEVICE = 'device'
CS_BOOTLOADER = 'bootloader'

# ----------------------------------------------------------------------
# Protocol helpers
# ----------------------------------------------------------------------

def _recvall(sock, size):
    datas, remain = [], size
    while remain > 0:
        data = sock.recv(remain)
        remain -= len(data)
        datas.append(data)
    return ''.join(datas)

def adb_connect():
  """Return a tcp socket.  No handshaking is done.
  Raise AdbError if server not reachable."""
  try:
      sock = socket.socket()
      sock.connect(("localhost", 5037))
  except socket.error:
      raise AdbError("Cannot contact server; try 'adb start-server'")
  return sock

def adb_send_command(sock, cmd):
    """Wrap envelope around *cmd*, send, receive ack.
    May raise AdbError"""
    cmd = "%04x%s" % (len(cmd), cmd)
    sock.sendall(cmd)
    try:
        adb_recv_stat(sock)
    except AdbError:
        sock.close()
        raise

def adb_connect_and_send(cmd):
    """adb_connect() and adb_send_command() rolled up into one convenient burrito.
    Returns sock."""
    sock = adb_connect()
    adb_send_command(sock, cmd)
    return sock

def adb_connect_and_send_withret(cmd):
    """adb_connect(), adb_send_command(), send command, receive ack.
    Also!  Receive reply.  Returns reply."""
    with closing(adb_connect_and_send(cmd)) as sock:
        # Some commands encode length as 4 bytes of ASCII hex; some do not!  Very weird!
        size = int(_recvall(sock,4), 16)
        return _recvall(sock,size)

def adb_recv_stat(sock):
    """Receive 4 bytes of status ('OKAY' or 'FAIL'), and raise AdbError on failure.
    Not normally needed, unless you're bypassing adb_send_command."""
    stat = _recvall(sock,4)
    if stat == "OKAY":
        return
    elif stat == "FAIL":
        size = int(_recvall(sock,4), 16)
        val = _recvall(sock, size)
        raise AdbError(val)
    else:
        raise AdbError("Bad response: %r" % (stat,))

# ----------------------------------------------------------------------
# AdbClient
# ----------------------------------------------------------------------

def adb_version():
    return int(adb_connect_and_send_withret("host:version"), 16)

def adb_wait_for_device(type='any'):
    """Block until the device list contains a device of type *type*.
    :param type: One of 'any', 'usb', 'local'"""
    assert type in ('any', 'usb', 'local')
    with closing(adb_connect_and_send("host:wait-for-%s" % type)) as sock:
        _recvall(sock,4)              # no idea why, but we get two OKAYs

def adb_get_devices():
    """Return list<AdbDevice>."""
    try:
        reply = adb_connect_and_send_withret("host:devices-l")
    except AdbError as e:
        if e.args[0] == 'unknown host service':
            raise AdbError("Your version of adb seems old; update your Android SDK")
        raise
    devices = []
    for line in reply.split('\n'):
        if not line: continue
        device_data = line.split(None, 3)
        serial, state = device_data[0:2]
        try: devpath = device_data[2]
        except IndexError: devpath = ""
        try: notes = device_data[4]
        except IndexError: notes = ""
        device = AdbDevice(serial, state, devpath, notes)
        devices.append(device)
    return devices

def adb_kill():
    """Terminate the adb server process."""
    sock = adb_connect_and_send("host:kill")
    sock.close()

# ----------------------------------------------------------------------
# AdbDevice
# ----------------------------------------------------------------------

class AdbDevice(object):
    """adb client for a specific device."""
    def __init__(self, serial, state, devpath, notes):
        self.serial = serial
        self.state = state      # CS_OFFLINE, CS_BOOTLOADER, or CS_DEVICE
        self.devpath = devpath  # also called "qualifier" by adb help
        self.notes = notes

    def __str__(self):
        return "<AdbDevice: %s %s (%s)>" % (self.serial, self.devpath, self.state)

    def connect_and_send(self, cmd):
        """Helper method.
        Like adb_connect_and_send, but targets this particular device."""
        # sock = adb_connect_and_send('host:transport-any')
        # sock = adb_connect_and_send('host:transport:usb:IDENTIFIER')
        # sock = adb_connect_and_send('host:transport:0abcdef123456')
        # both self.serial and self.devpath will work
        device_id = self.devpath
        if not device_id or len(device_id) == 0:
            print "Your devpath appears incorrect; connecting via serial instead."
            device_id = self.serial
        sock = adb_connect_and_send("host:transport:%s" % (device_id,))
        adb_send_command(sock, cmd)
        return sock

    def get_state(self):
        """Refresh self.state."""
        self.state = adb_connect_and_send_withret("host-serial:%s:get-state" % self.serial)
        return self.state

    def wait_until_running(self):
        # can also wait-for-usb/local/any.  Maybe wait-for-bootloader too?  but I think we pass
        # a device path and not a device serial in that case.
        sock = adb_connect_and_send("host-serial:%s:wait-for-device" % self.serial)
        data = _recvall(sock,4)        # for some reason we get an extra 'OKAY'!?
        assert data == 'OKAY'
        self.state = CS_DEVICE

    def simple_shell(self, cmd):
        outf = StringIO()
        self.shell(cmd, outf)
        return outf.getvalue()

    def shell(self, cmd, outf):
        """Run *cmd* in a remote shell. Result is written to outf."""
        sock = self.connect_and_send('shell:'+cmd)
        while True:
            data = sock.recv(1024)
            if data == '': break
            outf.write(data)

    def lolcat(self, outf, tags=""):
        """adb lolcat"""
        self.shell('export ANDROID_LOG_TAGS="%s" ; exec logcat' % (tags,), outf)

    def bugreport(self, outf):
        """adb bugreport"""
        self.shell('bugreport')

    def _walk(self, sock, root):
        files = []
        dirs = []
        for (mode, size, mtime, name) in device.sync_iterlist(sock, root):
            if stat.S_ISDIR(mode):
                dirs.append( (mode, size, mtime, name) )
            elif stat.S_ISREG(mode):
                files.append( (mode, size, mtime, name) )
        yield (root, dirs, files)
        for subdir in dirs:
            for tup in android_walk(sock, root+'/'+subdir):
                yield tup

    def walk(self, root):
        """Like os.walk.  Yields (root, dirs, files) tuples.
        *dirs* and *files* are lists of (mode, size, mtime, name) tuples."""
        with self.sync_transaction() as sock:
            for x in sync_walk(sock, root):
                yield x

    def does_mtime_work(self):
        """Return True if this build of Android properly supports mtime on /sdcard"""
        script = """function _df_test_mtime() {
  if touch -t 01010101 /sdcard/_test_mtime; then echo OKAY; else echo FAIL; fi
  rm -f /sdcard/_test_mtime
}
df_test_mtime"""
        try:
            val = self._does_mtime_work
        except AttributeError:
            outf = StringIO()
            self.shell(script, outf)
            val = self._does_mtime_work = 'OKAY' in outf.getvalue()
        return val

    def get_build_props(self):
        """Return /system/build.prop as a dict."""
        outf = StringIO()
        self.shell('cat /system/build.prop', outf)
        props = {}
        for line in outf.getvalue().split('\n'):
            if '=' not in line: continue
            try: k,v = line.strip().split('=',1)
            except ValueError: continue
            props[k] = v
        return props

    # Sync protocol

    @contextmanager
    def sync_transaction(self):
        """Returns a socket you can use with all the sync_* methods, cleaning it up when you're done."""
        sock = self.connect_and_send('sync:')
        try:
            yield sock
        finally:
            try: sync_send_req(sock, 'QUIT', '')
            except socket.error: pass
            try: sock.close()
            except socket.error: pass

    def sync_iterlist(self, sock, path):
        """List directory on device.
        Yields (mode, size, mtime, name)"""
        sync_send_req(sock, 'LIST', path)
        while True:
            (id,mode,size,mtime,name) = sync_recv_dirent(sock)
            if id == 'DONE': break
            yield (mode, size, mtime, name)

    def sync_stat(self, sock, remote_file):
        """Helper: return st_mode, st_size, st_mtime.
        *sock* must be connected and in "stat mode"."""
        sync_send_req(sock, 'STAT', remote_file)
        _, mode, size, mtime = sync_recv_stat(sock)
        return (mode, size, mtime)

    def sync_push(self, sock, local_file, remote_file):
        """Like adb push, except *remote_file* must not be an existing directory.
        *local_file* may be a filename, or a file-like object.
        WARNING: mtime is not reliable on /sdcard."""
        mode = self.sync_stat(sock, remote_file)[0]
        if mode != 0 and stat.S_ISDIR(mode):
            raise AdbError("Cannot push onto %s: is S_ISDIR" % remote_file)

        # Handle case of file-like object.
        if hasattr(local_file, 'read'):
            mode, mtime = 0644, 0
            sync_send_req(sock, 'SEND', "%s,%d" % (remote_file, mode))
            while True:
                data = local_file.read(SYNC_DATA_MAX)
                if data == '': break
                sync_send_data_data(sock, data)
            sync_send_data_done(sock, mtime)
            sync_recv_status(sock)
            return

        st = os.stat(local_file)
        if not stat.S_ISREG(st.st_mode):
            raise AdbError("Cannot push %s: not S_ISREG" % local_file)

        with file(local_file, 'rb') as inf:
            augmented_remote_file = "%s,%d" % (remote_file, mode)
            sync_send_req(sock, 'SEND', augmented_remote_file)
            while True:
                data = inf.read(SYNC_DATA_MAX)
                if data == '': break
                sync_send_data_data(sock, data)
            sync_send_data_done(sock, st.st_mtime)
            sync_recv_status(sock)

    def sync_pull(self, sock, remote_file, local_file):
        """Like adb pull.  Copies mtime but not permissions.
        *local_file* may be a filename, or a file-like object."""
        mode, _, mtime = self.sync_stat(sock, remote_file)
        if mode == 0:
            raise AdbError("Cannot pull %s: file does not exist" % remote_file)
        if not stat.S_ISREG(mode):
            raise AdbError("Cannot pull %s: not S_ISREG" % remote_file)

        # Handle the case of a file-like object.
        if hasattr(local_file, 'write'):
            sync_send_req(sock, 'RECV', remote_file)
            while True:
                id, data = sync_recv_data(sock)
                if id == 'DONE': break
                local_file.write(data)
            return

        # Check up-front for directories (because we're about to ignore any errors creating file)
        try:
            st = os.stat(local_file)
        except OSError:
            pass
        else:
            if stat.S_ISDIR(st.mode):
                raise AdbError("Cannot pull onto %s: is S_ISDIR" % local_file)

        try: os.makedirs(os.path.dirname(local_file))
        except OSError: pass

        tmp_file = local_file + '.part'
        try:
            with file(tmp_file, 'wb') as outf:
                sync_send_req(sock, 'RECV', remote_file)
                while True:
                    id, data = sync_recv_data(sock)
                    if id == 'DONE': break
                    outf.write(data)
            try: os.unlink(local_file)
            except OSError: pass
            os.rename(tmp_file, local_file)
            os.utime(local_file, (mtime, mtime))
        finally:
            try: os.unlink(tmp_file)
            except OSError: pass

# ----------------------------------------------------------------------
# The 'sync:' protocol
# ----------------------------------------------------------------------

# See system/core/adb/file_sync_service.h, union syncmsg
# The union contains 5 message types: req, stat, dent (dirent), data, status.

def sync_send_req(sock, id, data):
    """Send a syncmsg::req message"""
    # id may be 'list', ...?
    sock.send(struct.pack('<4sI', id, len(data)))
    if len(data):
        sock.send(data)

def sync_recv_stat(sock):
    """Receive a syncmsg::stat message.
    Return (id, mode, size, time).
    id is always 'STAT'."""
    # "stat": ("IIII", struct.calcsize("IIII")),   # id, mode, size, time
    id, mode, size, time = struct.unpack('<4s3I', _recvall(sock,4*4))
    if id != 'STAT':
        raise ProtocolError("msg.stat contained weird id %s" % (id,))
    return (id,mode,size,time)

def sync_recv_dirent(sock):
    """Receive a syncmsg::dirent message.
    Return (id, mode, size, time, name).
    id is one of 'DONE' (in which case message is all zeroes), 'DENT'."""
    id, mode, size, time, namelen = struct.unpack('<4s4I', _recvall(sock, 5*4))
    name = '' if namelen == 0 else _recvall(sock,namelen)
    if id not in ('DONE', 'DENT'):
        raise ProtocolError("msg.dent contained weird id %s" % (id,))
    return (id,mode,size,time,name)

def sync_send_data_data(sock, data):
    """Send a syncmsg::data message containing data."""
    sock.send(struct.pack('<4sI', 'DATA', len(data)))
    sock.send(data)
def sync_send_data_done(sock, mtime):
    """Send a syncmsg::data message containing "end of file" data (which includes a timestamp)"""
    sock.send(struct.pack('<4sI', 'DONE', mtime))

def sync_recv_data(sock):
    """Receive a syncmsg::data message.
    id is one of 'DONE' (in which case data is empty), 'DATA'."""
    id, datalen = struct.unpack('<4sI', _recvall(sock,2*4))
    data = '' if datalen == 0 else _recvall(sock, datalen)
    if id not in ('DATA', 'DONE'):
        raise ProtocolError("msg.data contained weird id %s" % (id,))
    return (id, data)

def sync_recv_status(sock):
    """Receive a syncmsg::status msg.
    On error, raise AdbError; otherwise return nothing."""
    id, msglen = struct.unpack('<4sI', _recvall(sock,2*4))
    message = '' if msglen == 0 else _recvall(sock, msglen)
    if id == 'OKAY':
        if message:
            ProtocolError("Unexpected data %s with OKAY status msg: %s" % (message,))
    else:
        raise AdbError("Received FAIL: %s" % message)

dirent = namedtuple('dirent', 'mode size mtime name')
def sync_walk(sock, root):
    """Not really part of the sync: suite of commands, but a useful
    high-level function patterned after os.walk.
    Yields (root, dirs, files) tuples.
    *dirs* and *files* are lists of *dirent* instances."""
    dirs, files = [], []

    sync_send_req(sock, 'LIST', root)
    while True:
        (id,mode,size,mtime,name) = sync_recv_dirent(sock)
        if id == 'DONE': break;
        if stat.S_ISDIR(mode):
            if name != '.' and name != '..':
                dirs.append( dirent(mode, size, mtime, name) )
        elif stat.S_ISREG(mode):
            files.append( dirent(mode, size, mtime, name) )

    yield (root, dirs, files)

    for subdir in dirs:
        for tup in sync_walk(sock, root+'/'+subdir.name):
            yield tup


# ----------------------------------------------------------------------
# Testing
# ----------------------------------------------------------------------

def _monkeypatch_sock(sock):
    # For testing: hack the socket's send/recv to be spammy
    def spammysend(data, send=sock.send):
        n = send(data)
        print '-> %04x/%04x %r' % (n,len(data),data[:50])
        return n
    def spammyrecv(n, recv=sock.recv):
        data = recv(n)
        print '<- %04x/%04x %r' % (len(data),n,data[:50])
        return data
    sock.send = spammysend
    sock.recv = spammyrecv

def _fmt_unixtime(t):
    import time
    return "%s %d" % (time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(t)), t)

def test():
    def print_stat(tup):
        (flags, size, t) = tup
        print "%06o  %05x  %s" % (flags, size, _fmt_unixtime(t))
    c = AdbClient()
    device = c.devices()[0]
    print repr(device.does_mtime_work())
    for k,v in sorted(device.get_build_props().items()):
        print k,v

if __name__=='__main__':
    test()
