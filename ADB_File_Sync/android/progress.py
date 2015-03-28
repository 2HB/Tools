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
# Utilities for printing progress bars and junk like that.
#

import sys
from contextlib import contextmanager

__all__ = ('progress', 'pct')

def pct(i,tot):
    pct = ((i+1)*100)/tot
    return "%3d%%" % pct

class _Popper(object):
    # Helper class for Progress.scoped_push
    def __init__(self, progress_instance):
        self.progress = progress_instance
    def __del__(self):
        self.progress.pop()

def _get_terminal_width():
    """Will return None if we can't figure it out.
    May return 0 if unix can't figure it out?"""
    import os, sys

    # This one should work on any unix, because unix has rad ioctls
    # with primitive calling conventions
    def ioctl_get_width(fd):
        try:
            import fcntl, termios
        except ImportError:
            return None
        # Unfortunately, I'm not sure exactly what the failure modes
        # of this next bit are
        import array
        # row, column, xpixel, ypixel
        data = array.array('h', [0,0,0,0])
        try:
            fcntl.ioctl(fd, termios.TIOCGWINSZ, data)
            return data[1]
        except Exception as e:
            return None

    try:
        fd = sys.stdout.fileno()
    except AttributeError:
        width = None
    else:
        width = ioctl_get_width(fd)

    if width is None:
        try:
            fd = os.open(os.ctermid(), os.O_RDONLY)
            width = ioctl_get_width(fd)
            os.close(fd)
        except Exception as e:
            pass

    # Don't check env var, because that loses inside emacs
    return width


class Progress(object):
    """Pseudo-function for displaying pretty progress messages."""
    def __init__(self):
        try:
            self.bTerse = not sys.stdout.isatty()
        except AttributeError:
            self.bTerse = True

        # Let's try to adjust to the terminal width!
        width = _get_terminal_width()
        if width is None or width==0: width = 80
        self._width  = (width-1)
        self._format = "%%-%ds\r" % (self._width)
        self.prefix_stack = ['']
        self.force_flush = (sys.platform in ('darwin',))

    @contextmanager
    def prefix(self, txt):
        """Like scoped_push but better."""
        self.push(txt)
        try: yield
        finally: self.pop()

    def scoped_push(self, txt):
        """Return an object that when destructed, does a progress.pop"""
        self.push(txt)
        return _Popper(self)

    def push(self, txt):
        self.prefix_stack.append(self.prefix_stack[-1]+txt)
        self.__call__('')

    def pop(self):
        self.prefix_stack.pop()
        self.__call__('')

    def __call__(self, txt, bNewline=False):
        if self.bTerse and not bNewline: return
        txt = self._format % (self.prefix_stack[-1] + txt)[:self._width]
        sys.stdout.write(txt)
        if bNewline: sys.stdout.write('\n')
        elif self.force_flush: sys.stdout.flush()

# def progress(txt): print txt
progress = Progress()
