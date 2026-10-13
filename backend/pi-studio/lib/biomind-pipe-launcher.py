#!/usr/bin/env python3
"""Bounded POSIX stdio compatibility launcher for the complete BioMind entry.

Reuse the byte-bridge mechanism witnessed in BioMind's posix-pipe-relay.py:
Node child stdio socketpairs are converted to actual pipes for the Python/Node
entry chain. This layer never parses JSON, acknowledges commands, owns sessions
or cancels domain jobs. Only server transport EOF closes the child's stdin.
"""
import os
import select
import signal
import subprocess
import sys
import time


def main():
    if os.name != "posix" or len(sys.argv) < 2:
        sys.stderr.write("BIOMIND_PIPE_LAUNCHER_REQUIRES_POSIX_COMMAND\n")
        return 2
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    stdin, stdout, stderr = child.stdin.fileno(), child.stdout.fileno(), child.stderr.fileno()
    # source -> destination, independent bounded queues and independent EOF.
    destinations = {0: stdin, stdout: 1, stderr: 2}
    limits = {stdin: 12 * 1024 * 1024, 1: 24 * 1024 * 1024, 2: 1024 * 1024}
    buffers = {fd: bytearray() for fd in limits}
    input_eof = False
    shutdown_at = None
    terminated = False

    def stop(_signum, _frame):
        nonlocal input_eof, shutdown_at
        destinations.pop(0, None)
        input_eof = True
        if shutdown_at is None:
            shutdown_at = time.monotonic()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    for fd in (0, 1, 2, stdin, stdout, stderr):
        os.set_blocking(fd, False)
    try:
        while destinations or any(buffers.values()):
            if input_eof and not buffers[stdin] and not child.stdin.closed:
                child.stdin.close()
            # This grace period begins only when the entire server transport
            # ends; it is not an agent/scientific-job execution deadline.
            if shutdown_at is not None and child.poll() is None and time.monotonic() - shutdown_at > 15:
                if not terminated:
                    child.terminate()
                    terminated = True
                elif time.monotonic() - shutdown_at > 20:
                    child.kill()
            readers = [source for source, destination in destinations.items() if len(buffers[destination]) < limits[destination]]
            writers = [destination for destination, buffer in buffers.items() if buffer]
            readable, writable, _ = select.select(readers, writers, [], 0.25)
            for source in readable:
                destination = destinations.get(source)
                if destination is None:
                    continue
                try:
                    data = os.read(source, min(65536, limits[destination] - len(buffers[destination])))
                except BlockingIOError:
                    continue
                if not data:
                    destinations.pop(source, None)
                    if source == 0:
                        input_eof = True
                        shutdown_at = shutdown_at or time.monotonic()
                    continue
                buffers[destination].extend(data)
            for destination in writable:
                try:
                    count = os.write(destination, buffers[destination])
                    del buffers[destination][:count]
                except BlockingIOError:
                    continue
                except (BrokenPipeError, OSError):
                    buffers[destination].clear()
                    for source, target in list(destinations.items()):
                        if target == destination:
                            destinations.pop(source, None)
                    if destination in (1, 2):
                        stop(signal.SIGTERM, None)
            if child.poll() is not None:
                destinations.pop(0, None)
                buffers[stdin].clear()
                if not destinations and not any(buffers.values()):
                    break
        return child.wait()
    finally:
        if not child.stdin.closed:
            child.stdin.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        sys.stderr.write("BIOMIND_PIPE_TRANSPORT_FAILED\n")
        raise SystemExit(2)
