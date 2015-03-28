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
# We assume these things:
# 
# - file db might contain a subset, but never a superset, of the files on device
# - 'size' is accurate (matches the file on device)
# - 'mtime' is accurate (matches the source/local file; device mtime might be bogus)
#
# User must watch out for these potential problems:
# 
# - Do not alter the files on /sdcard.  On many devices, we cannot rely on
#   mtime to detect the modficiation.  In fast mode, we also cannot rely on file
#   size to detect the modification.
#
# - If you use fast mode, do not add or remove /sdcard files by hand.
#   Your modification will not be detected, and the files will not be
#   removed or re-copied until you run once in normal mode.
#

import os
import stat
import time
import pickle
from cStringIO import StringIO
from itertools import izip

import android.adb as adb
from android.utils import posixjoin
from android.progress import progress

__all__ = ('rsync',)

_DB_NAME = 'files.pickle'

# ----------------------------------------------------------------------
# Little utils
# ----------------------------------------------------------------------

def _fmt_sec(sec):
    sec = int(sec)
    if sec < 60: return "%ds" % sec
    min,sec = divmod(sec,60)
    if min < 60: return "%dm:%02ds" % (min,sec)
    return "??m:??s"

def _fmt_bytes(n):
    n = float(n)
    if n < 1024: return "%d bytes" % n
    else: n /= 1024.0
    if n < 1024: return "%.4gK" % n
    else: n /= 1024.0
    if n < 1024: return "%.4gM" % n
    else: n /= 1024.0
    return "%.4gG" % n

def _plural(n, noun):
    if type(n) not in (int, float): n = len(n)
    if n==1: return "1 %s" % noun
    return "%d %ss" % (n, noun)


# Helper for calculating bytes/sec
class TimeEstimator(object):
    """Helper for estimating the time t at which a time-changing value
    reaches some final value."""
    def __init__(self, total, decay_time=10.0):
        """*decay_time* is the time taken for the smoothed value to
        exponentially decay 90% of the way towards the instantaneous value.
        Think of it as an averaging window."""
        self.decay_time = decay_time
        self.t = time.time()
        self.v = 0              # current value
        self.v1 = total         # final value
        self.dvdt = 1           # smoothed with iir filter

    def increment(self, dv):
        """Return (progress_pct, eta_seconds)"""
        if dv > 0:
            t = time.time()
            dt = t - self.t
            self.t = t
            self.v += dv
            k = 0.1 ** (dt / self.decay_time)
            self.dvdt = k * self.dvdt + (1-k) * dv/dt
        progress_pct = (1+self.v*100)//(1+self.v1)
        return (progress_pct, (self.v1-self.v)/self.dvdt)


def _local_walk(root, warning):
    """Walk local fs like os.walk,
    but return info in the same form as device.walk"""
    names = os.listdir(root)

    dirs, files = [], []
    for name in names:
        full = posixjoin(root,name)
        try: st = os.stat(full)
        except OSError:
            warning("Unreadable: %s" % full)
            continue
        if stat.S_ISDIR(st.st_mode):
            dirs.append(adb.dirent(st.st_mode, st.st_size, st.st_mtime, name))
        elif stat.S_ISREG(st.st_mode):
            files.append(adb.dirent(st.st_mode, st.st_size, st.st_mtime, name))

    yield root, dirs, files

    for subdir in dirs:
        for tup in _local_walk(posixjoin(root, subdir.name), warning):
            yield tup


# ----------------------------------------------------------------------
# db stuff
# ----------------------------------------------------------------------

# db is dict mapping canonical relative pathname -> (mtime, size) tuples

def _get_db(device, remote_folder):
    outf = StringIO()
    try:
        with device.sync_transaction() as sock:
            device.sync_pull(sock, posixjoin(remote_folder, _DB_NAME), outf)
            db = pickle.loads(outf.getvalue())
            # XXX bugfix
            #if '/' in db: db[''] = db.pop('/')
            return db
    except adb.AdbError:
        return {}


def _put_db(device, sock, remote_folder, dct):
    inf = StringIO(pickle.dumps(dct, -1))
    device.sync_push(sock, inf, posixjoin(remote_folder, _DB_NAME))

        
def _db_walk(db, root):
    """Exactly same api as device.walk.  This one doesn't bother communicating
    with the device; it assumes that the db is complete and valid"""
    # Convert flat list of files to a tree structure

    class _Directory(object):
        def __init__(self, path):
            self.path = path    # relative to root of db
            self.child_files = []
            self.child_dirs = []

    def _get_dir(path):
        try: return dir_map[path]
        except KeyError: pass
        dir_map[path] = dbdir = _Directory(path)
        if path != '':
            parent = _get_dir(os.path.dirname(path))
            assert parent is not dbdir, (path,parent.path,root)
            parent.child_dirs.append(dbdir)
        return dbdir

    def _to_dirent(directory):
        return adb.dirent(0755, 0, 0, os.path.basename(directory.path))

    def _walk(directory):
        # caller wants list<dirent>, not list<_Directory>
        dirent_to_child = dict( (_to_dirent(c), c) for c in directory.child_dirs )
        dirs  = dirent_to_child.keys()
        # child_files is already list<dirent>
        files = directory.child_files           # already in correct format
        yield (posixjoin(root, directory.path), dirs, files)
        # child_dirs may have been mutated.  New dirents may even have been added.
        for dirent in dirs:
            try:
                child = dirent_to_child[dirent]
            except KeyError:
                # This happens because rsync() plays games, inserting nonexistent dirs into the dirs list
                child = _Directory(posixjoin(directory.path, dirent.name))
            for x in _walk(child):
                yield x

    # Ensure there is always a root dir, even in empty db
    dir_map = { '': _Directory('') }
    for (path,(mtime,size)) in db.iteritems():
        dirent = adb.dirent(0644, size, mtime, os.path.basename(path))
        parent = _get_dir(os.path.dirname(path))
        parent.child_files.append(dirent)

    return _walk(dir_map[''])
    

# ----------------------------------------------------------------------
# rsync
# ----------------------------------------------------------------------

def rsync(device, local_folder, remote_folder, #report,
          warning=None,
          fast=False,
          trial_run=False):
    """Make *remote_folder* match *local_folder*.

    If *warning*, call that function for all warnings.
    If *fast*, query db instead of remote filesystem.  See discussion in header.
    If *trial_run*, do not do any copying or removing.
    """

    pathExists = os.path.exists(local_folder)
    if not pathExists:
        print("path does not exist: " + local_folder)
    assert pathExists
    if warning is None:
        def warning(w): print w

    db = _get_db(device, remote_folder)
    db_mtimes = dict( (name, mtime) for (name, (mtime,size)) in db.iteritems() )
    can_use_mtime = device.does_mtime_work()
    
    l_walk = _local_walk(local_folder, warning)
    if fast: r_walk = _db_walk(db, remote_folder)
    else:    r_walk = device.walk(remote_folder)

    def _to_dct_and_set(dirents):
        d = dict( (de.name.lower(), de) for de in dirents )
        s = set(d.iterkeys())
        return d,s
    
    def _different(l_dirent, r_dirent, r_mtime):
        # Return True if files might be different.  False positives are OK; false negatives are not.
        # use r_mtime instead of r_dirent.mtime because /sdcard fs can't be trusted
        if l_dirent.size != r_dirent.size: return True
        if abs(l_dirent.mtime-r_mtime) > 5: return True
        return False

    to_add = []
    to_remove = []
    to_remove_dir = []
    new_db = {}                 # easier to create from scratch than to mutate prev db
    first = True

    if fast: progress("Scanning %s" % (local_folder,))
    else:    progress("Comparing %s to %s" % (local_folder, remote_folder,))

    for ((l_root, l_dirs, l_files), (r_root, r_dirs, r_files)) in izip(l_walk, r_walk):
        # Verify that the walks are proceeding in lockstep
        assert first or os.path.basename(l_root).lower() == os.path.basename(r_root).lower(), (
            l_root, r_root)
        first = False

        # classify files
        l_files_dct, l_files_set = _to_dct_and_set(l_files)
        r_files_dct, r_files_set = _to_dct_and_set(r_files)

        for missing in l_files_set - r_files_set:
            to_add.append( (l_root, l_files_dct[missing], r_root) )

        for extra in r_files_set - l_files_set:
            # Special case: don't remove our mtime db!
            if extra == _DB_NAME and r_root == remote_folder:
                continue
            to_remove.append( "%s/%s" % (r_root, r_files_dct[extra].name) )

        for common in r_files_set & l_files_set:
            # db key is the path relative to the root, in canonical form
            db_key = ("%s/%s" % (r_root, common))
            db_key = db_key[len(remote_folder)+1:].lower()
            assert db_key != '/', (r_root,common,remote_folder)
            l_dirent = l_files_dct[common]
            r_dirent = r_files_dct[common]
            if can_use_mtime:
                db_mtimes[db_key] = r_dirent.mtime
            if _different(l_dirent, r_dirent, db_mtimes.get(db_key,0)):
                to_add.append( (l_root, l_dirent, r_root) )
            else:
                try:
                    new_db[db_key] = db[db_key]
                except KeyError:
                    # db doesn't contain info about a remote file, but it's identical?  Hmm.
                    tmp = (r_dirent.mtime if can_use_mtime else l_dirent.mtime)
                    new_db[db_key] = (tmp, r_dirent.size)

        # classify_dirs
        l_dirs_dct, l_dirs_set = _to_dct_and_set(l_dirs)
        r_dirs_dct, r_dirs_set = _to_dct_and_set(r_dirs)
        for missing in l_dirs_set - r_dirs_set:
            # It so happens that adb doesn't barf if you try to listdir a nonexistent directory.
            # It just returns nothing.  So, let's pretend the remote dir exists and is empty,
            # and iterate into it; that way all file-adds are handled the same way
            r_dirs_set.add(missing)
            r_dirs_dct[missing] = adb.dirent(None,None,None,l_dirs_dct[missing].name)
        for extra in r_dirs_set - l_dirs_set:
            to_remove_dir.append( "%s/%s" % (r_root, r_dirs_dct[extra].name) )
        # Mutate the directory lists in-place to control the iteration's future
        del l_dirs[:], r_dirs[:]
        for common in r_dirs_set & l_dirs_set:
            l_dirs.append(l_dirs_dct[common])
            r_dirs.append(r_dirs_dct[common])

    if trial_run:
        # Just report on what we would do.
        if to_remove_dir:
            progress("Would remove %s" % _plural(to_remove_dir, 'dir'), 1)
        if to_remove:
            progress("Would remove %s" % _plural(to_remove, 'file'), 1)
        if to_add:
            nb = sum(tup[1].size for tup in to_add)
            progress("Would copy %s in %s" % (_fmt_bytes(nb), _plural(to_add, 'file')), 1)
        return
        
    # Perform operations and finish creating new_db
    with device.sync_transaction() as sock:
        _put_db(device, sock, remote_folder, new_db)     # checkpoint it
        n = 0 ; total = len(to_remove_dir) + len(to_remove) + len(to_add)
        # Process removals before adds, because dirs might be in the way of files
        for r_full in to_remove_dir:
            n += 1 ; pct = n*100//total
            progress("[%3d%%] Rmdir %s/" % (pct, os.path.relpath(r_full, remote_folder)))
            if not r_full.startswith('/sdcard/dfp'):
                warning("Trying to rmdir %s: do it by hand instead." % r_full)
                continue
            device.simple_shell("rm -r '%s'" % r_full)

        for r_full in to_remove:
            n += 1 ; pct = n*100//total
            progress("[%3d%%] Remove %s" % (pct, os.path.relpath(r_full, remote_folder)))
            device.simple_shell("rm '%s'" % r_full)

        AUTOSAVE_INTERVAL = 10
        estimator = TimeEstimator(sum(tup[1].size for tup in to_add))
        t_savedb = time.time() + AUTOSAVE_INTERVAL
        if len(to_add):
            progress("Copying %s in %s" % (_fmt_bytes(estimator.v1), _plural(to_add, 'file')), 1)

        prev_pct = None
        for (l_root, l_dirent, r_root) in to_add:
            l_full = "%s/%s" % (l_root, l_dirent.name)
            r_full = "%s/%s" % (r_root, l_dirent.name)
            db_key = r_full[len(remote_folder)+1:].lower()

            new_db[db_key] = ( l_dirent.mtime, l_dirent.size )
            device.sync_push(sock, l_full, r_full)

            pct, eta = estimator.increment(l_dirent.size)
            if True or pct != prev_pct:
                # Cut down on the console traffic a bit
                prev_pct = pct
                progress("[%3d%%] [%s] %s/s %s" % (
                        pct, _fmt_sec(eta), _fmt_bytes(estimator.dvdt),
                        os.path.relpath(l_full, local_folder)))

            # Save the db every few seconds
            t = time.time()
            if t > t_savedb:
                t_savedb = t + AUTOSAVE_INTERVAL
                _put_db(device, sock, remote_folder, new_db)

        _put_db(device, sock, remote_folder, new_db)
            

if __name__ == '__main__':
    pass
