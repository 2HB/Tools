#!/usr/bin/python
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

import os
import sys
from android import adb
from android import rsync

def get_device():
    while True:
        devices = adb.adb_get_devices()
        # TODO: use options to select one device in particular?
        if len(devices) > 0:
            return devices[0]
        print "No devices connected.  Waiting for connection..."
        adb.adb_wait_for_device()

def report_warning(warn):
    print("[WARNING] " + warn)
    
def main():
    # Step 1: Create a device
    print("Creating ADB device\n")
    device = get_device()
    # Step 2: Copy some files to the attached device (if they have changed)
    LOCAL = 'example_data'
    REMOTE = '/sdcard/adb_test'
    print("Synchronizing files.\nSource folder \"%s\" -> Destination folder \"%s\"\n" % (LOCAL, REMOTE))
    rsync.rsync(device, 
        LOCAL, REMOTE,
        warning=report_warning)
    # Step 3: Execute shell command on the attached device
    print("Listing files on device\n")
    remote_files = device.simple_shell('ls -lisa %s' % REMOTE)
    print(remote_files)

main()