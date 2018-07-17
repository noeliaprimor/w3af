"""
process.py

Copyright 2018 Andres Riancho

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
import os
import re
import time
import signal
import select
import threading
import subprocess

import w3af.core.controllers.output_manager as om

from w3af.core.controllers.misc.homeDir import get_home_dir
from w3af.core.controllers.dependency_check.external.chrome import get_chrome_path, get_chrome_version


class ChromeProcess(object):

    CHROME_PATH = get_chrome_path()
    DEFAULT_FLAGS = [
             '--headless',
             '--disable-gpu',
             '--window-size=1920,1200',

             # Initial page load performance
             '--homepage=about:blank',

             # Do not load image
             '--blink-settings=imagesEnabled=false',

             # Disable some security features
             '--ignore-certificate-errors',
             '--reduce-security-for-testing',
             '--allow-running-insecure-content',
    ]

    DEVTOOLS_PORT_RE = re.compile('DevTools listening on ws://127.0.0.1:(\d*?)/devtools/')
    START_TIMEOUT_SEC = 5.0

    def __init__(self):
        self.devtools_port = 0
        self.proxy_host = None
        self.proxy_port = None
        self.data_dir = self.get_default_user_data_dir()

        self.stdout = []
        self.stderr = []
        self.proc = None

        self.thread = None

    def get_default_user_data_dir(self):
        return os.path.join(get_home_dir(), 'chrome')

    def set_devtools_port(self, devtools_port):
        """
        By default 0 is sent to the remote-debugging-port. This will start the
        browser and bind to a random unused port.

        :param devtools_port: Port number to bind to
        """
        self.devtools_port = devtools_port

    def get_devtools_port(self):
        return self.devtools_port

    def set_proxy(self, proxy_host, proxy_port):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port

    def get_cmd(self):
        flags = self.DEFAULT_FLAGS[:]

        flags.append('--remote-debugging-port=%s' % self.devtools_port)
        flags.append('--user-data-dir=%s' % self.data_dir)

        if self.proxy_port and self.proxy_host:
            flags.append('--proxy-server=%s:%s' % (self.proxy_host, self.proxy_port))

        flags = ' '.join(flags)

        cmd = '%s %s' % (self.CHROME_PATH, flags)
        return cmd

    def start(self):
        """
        Create a new thread and use it to run the Chrome process.

        :return: The method returns right after creating the thread.
        """
        args = (get_chrome_version(),)
        msg = 'ChromeProcess is using "%s"'
        om.out.debug(msg % args)

        self.thread = threading.Thread(name='ChromeThread',
                                       target=self.run)
        self.thread.daemon = True
        self.thread.start()

    def run(self):
        """
        Create a new subprocess to run Chrome. This method will block
        until Chrome is either killed (self.proc.terminate()) or decides
        to stop.

        If you want a non-blocking version of the same thing, just use
        start().

        :return: None
        """
        (stdout_r, stdout_w) = os.pipe()
        (stderr_r, stderr_w) = os.pipe()

        self.proc = subprocess.Popen(self.get_cmd(),
                                     shell=True,
                                     stdout=stdout_w,
                                     stderr=stderr_w,
                                     close_fds=True,
                                     preexec_fn=os.setsid)

        while self.proc.poll() is None:

            read_ready, write_ready, _ = select.select([stdout_r, stderr_r],
                                                       [],
                                                       [],
                                                       0.2)

            for fd in read_ready:
                data = os.read(fd, 1024)
                if fd == stdout_r:
                    self.store_stdout(data)
                else:
                    self.store_stderr(data)

        os.close(stdout_r)
        os.close(stdout_w)
        os.close(stderr_r)
        os.close(stderr_w)

    def wait_for_start(self):
        tries = 100
        wait = self.START_TIMEOUT_SEC / tries

        for _ in xrange(tries):
            if self.get_devtools_port():
                return True

            time.sleep(wait)

        return False

    def terminate(self):
        self.devtools_port = 0

        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except OSError:
                # In some cases the process is already dead, calling terminate()
                # will try to kill a process that doesn't exist anymore
                pass

        if self.thread is not None:
            self.thread.join()
            self.thread = None

    def store_stdout(self, stdout_data):
        """
        Stores stdout data, and in some cases extracts information from it.

        :param stdout_data: String we read from Chrome's stdout
        :return: None
        """
        if stdout_data is None:
            return

        [self.stdout.append(l) for l in stdout_data.split('\n')]
        [self.extract_data(l) for l in stdout_data.split('\n')]

    def store_stderr(self, stderr_data):
        """
        Stores stderr data, and in some cases extracts information from it.

        :param stderr_data: String we read from Chrome's stderr
        :return: None
        """
        if stderr_data is None:
            return

        [self.stderr.append(l) for l in stderr_data.split('\n')]
        [self.extract_data(l) for l in stderr_data.split('\n')]

    def extract_data(self, line):
        """
        Extract important data from string

        :param line: A line printed to std(out|err) by Chrome
        :return: None
        """
        self.extract_devtools_port(line)

    def extract_devtools_port(self, line):
        """
        Find lines like:

            DevTools listening on ws://127.0.0.1:36375/devtools/browser/{uuid-4}

        And extract the port. Set the port to self.devtools_port
        """
        match_object = self.DEVTOOLS_PORT_RE.search(line)
        if not match_object:
            return

        devtools_port = match_object.group(1)
        self.devtools_port = int(devtools_port)