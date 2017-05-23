# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Copyright © 2017, Continuum Analytics, Inc. All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# ----------------------------------------------------------------------------
from __future__ import absolute_import, print_function

import io
import subprocess
from threading import Thread

try:
    from queue import Queue
except ImportError:  # pragma: no cover (py2 only)
    from Queue import Queue  # pragma: no cover (py2 only)

from anaconda_project.internal import logged_subprocess


# this function exists to be mocked in tests
def _read_from_stream(stream, count):
    return stream.read(count)


def _read_and_queue_data(pipe, queue):
    try:
        while True:
            # Reading one character at a time is ridiculous, but
            # the problem is we want to immediately display each
            # "." in conda's "Doing stuff....." sort of output. If
            # we read >1 char, Python apparently will call the
            # read() system call more than once trying to fill up
            # our requested buffer, which is undesirable because
            # it prevents showing streaming progress to the user.
            data = _read_from_stream(pipe, 1)
            if len(data) == 0:
                break
            remaining = data
            while len(remaining) > 0:
                (start, sep, end) = remaining.partition('\n')
                if sep == '':
                    # no newline found, send chunk along
                    queue.put((pipe, remaining, None))
                    remaining = ''
                else:
                    # newline found, send line and then
                    # look for another line
                    queue.put((pipe, start + sep, None))
                    remaining = end
        queue.put((pipe, None, None))
    except Exception as e:
        queue.put((pipe, None, e))


def _reader_thread(pipe, queue):
    t = Thread(target=_read_and_queue_data, args=(pipe, queue))
    t.daemon = True
    t.start()
    return t


def _combine_lines(datas):
    combined = []
    for data in datas:
        if len(combined) == 0 or combined[-1].endswith("\n"):
            combined.append(data)
        else:
            combined[-1] = combined[-1] + data
    return combined


def popen(args, stdout_callback, stderr_callback, **kwargs):
    def ignore_line(line):
        pass

    if stdout_callback is None:
        stdout_callback = ignore_line
    if stderr_callback is None:
        stderr_callback = ignore_line

    p = logged_subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    queue = Queue()

    # Create/destroy TextIOWrapper outside of the threads, since
    # there have been threading bugs in their destructor.  See for
    # example https://bugs.python.org/issue28387 Note that
    # TextIOWrapper is NOT thread-safe, so we only use it from one
    # thread at a time, even though all creation/deletion is in
    # this main thread, reading is always in the child threads.
    #
    # we use errors=replace primarily because with strict
    # errors, TextIOWrapper can raise an exception
    # "prematurely" (before returning all valid bytes).
    # Arguably replace is nicer anyway for our purposes.
    stdout_wrapper = io.TextIOWrapper(p.stdout, encoding='utf-8', errors='replace')
    stderr_wrapper = io.TextIOWrapper(p.stderr, encoding='utf-8', errors='replace')
    try:

        stdout_thread = _reader_thread(stdout_wrapper, queue)
        stderr_thread = _reader_thread(stderr_wrapper, queue)

        stdout_buffer = []
        stderr_buffer = []

        first_error = None
        stdout_joined = False
        stderr_joined = False
        while not (queue.empty() and (stdout_joined and stderr_joined)):
            (which, data, error) = queue.get()
            if error is not None and first_error is None:
                first_error = error
            if data is None:
                if which is stdout_wrapper:
                    stdout_thread.join()
                    stdout_joined = True
                    assert not stdout_thread.is_alive()
                else:
                    assert which is stderr_wrapper
                    stderr_thread.join()
                    stderr_joined = True
                    assert not stderr_thread.is_alive()
            else:
                if which is stdout_wrapper:
                    stdout_callback(data)
                    stdout_buffer.append(data)
                else:
                    assert which is stderr_wrapper
                    stderr_callback(data)
                    stderr_buffer.append(data)

        p.wait()

        assert queue.empty()

        stdout_buffer = _combine_lines(stdout_buffer)
        stderr_buffer = _combine_lines(stderr_buffer)

        if first_error is not None:
            raise first_error

    finally:
        stdout_wrapper.close()
        stderr_wrapper.close()

    return (p, stdout_buffer, stderr_buffer)
