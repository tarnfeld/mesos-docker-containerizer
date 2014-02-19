#!/usr/bin/env python

#      _            _                                  _        _                 _
#   __| | ___   ___| | _____ _ __       ___ ___  _ __ | |_ __ _(_)_ __   ___ _ __(_)_______ _ __
#  / _` |/ _ \ / __| |/ / _ \ '__|____ / __/ _ \| '_ \| __/ _` | | '_ \ / _ \ '__| |_  / _ \ '__|
# | (_| | (_) | (__|   <  __/ | |_____| (_| (_) | | | | || (_| | | | | |  __/ |  | |/ /  __/ |
#  \__,_|\___/ \___|_|\_\___|_|        \___\___/|_| |_|\__\__,_|_|_| |_|\___|_|  |_/___\___|_|
#
#  Pluggable containerizer implementation for Docker with Mesos
#

import subprocess
import argparse
import time
import sys
import os

import google
import mesos_pb2


def _docker_command(args):
    """Return a docker command including any global options based on `args`."""

    command = ["docker"]
    if args.docker_host:
        command.extend(["-H", args.docker_host])

    return command


def _send_status(status):
    """Write a PluggableStatus protobuf object to stdout."""

    sys.stdout.write(status.SerializeToString())
    sys.stdout.flush()


def launch(container, args):
    """Launch a new docker container, don't wait for the container to terminate."""

    # Read the TaskInfo from STDIN
    try:
        data = sys.stdin.read()
        if len(data) <= 0:
            print >> sys.stderr, "Expected protobuf over stdin. Received 0 bytes."
            return 1

        task = mesos_pb2.TaskInfo()
        task.ParseFromString(data)
    except google.protobuf.message.DecodeError:
        print >> sys.stderr, "Could not deserialise external container protobuf"
        return 1

    # Build the docker invocation
    command = []

    # If there's no executor command, wrap the docker invoke in our own
    if not task.executor.command.value:
        executor_path = os.path.join(
            os.path.dirname(
                os.path.realpath(__file__)
            ),
            "bin/docker-executor"
        )
        command.append(executor_path)

    command.extend(_docker_command(args))
    command.append("run")

    # Add any environment variables
    for env in task.command.environment.variables:
        command.extend([
            "-e",
            "%s=%s" % (env.name, env.value)
        ])

    # Set the container ID
    command.extend([
        "-name", container
    ])

    # Set the resource configuration
    for resource in task.resources:
        if resource.name == "cpus":
            command.extend(["-c", str(int(resource.scalar.value * 256))])
        if resource.name == "mem":
            command.extend(["-m", "%dm" % (int(resource.scalar.value))])
        # TODO: Handle port configurations

    # Figure out what command to execute in the container
    # TODO: Test with executors that are fetched from a remote
    if task.executor.command.value:
        container_command = task.executor.command.value
    else:
        container_command = task.command.value

    # Put together the rest of the invoke
    command.append(task.command.container.image)
    command.extend(["/bin/sh", "-c", container_command])

    print >> sys.stderr, "Launching docker process with command %r" % (command)

    # Write the stdout/stderr of the docker container to the sandbox
    sandbox_dir = os.environ["MESOS_DIRECTORY"]

    stdout_path = os.path.join(sandbox_dir, "stdout")
    stderr_path = os.path.join(sandbox_dir, "stderr")

    with open(stdout_path, "w") as stdout:
        with open(stderr_path, "w") as stderr:
            proc = subprocess.Popen(command, stdout=stdout, stderr=stderr)

            status = mesos_pb2.PluggableStatus()
            status.message = "launch/docker: ok"

            _send_status(status)
            os.close(1)  # Close stdout

            return_code = proc.wait()

    print >> sys.stderr, "Docker container %s exited with return code %d" % (container, return_code)
    return return_code


def usage(container, args):
    """Retrieve the resource usage of a given container."""

    # TODO
    return 0


def destroy(container, args):
    """Destroy a container."""

    # Build the docker invocation
    command = list(_docker_command(args))
    command.extend(["stop", "-t", args.docker_stop_timeout, container])

    print >> sys.stderr, "Destroying container with command %r" % (command)

    proc = subprocess.Popen(command)
    return_code = proc.wait()

    if return_code == 0:
        status = mesos_pb2.PluggableStatus()
        status.message = "destroy/docker: ok"
        return status

    return return_code


def wait(container, args):
    """Wait for the given container to come up."""

    timeout = 5.0
    interval = 0.1

    # Build the docker command
    command = list(_docker_command(args))
    command.extend(["inspect", container])

    # Wait for `timeout` until the container comes up
    while timeout > 0.0:

        print >> sys.stderr, "Checking status of docker container %s" % (container)

        # Write the container info out to the sandbox, for lols
        sandbox_dir = os.environ["MESOS_DIRECTORY"]
        with open(os.path.join(sandbox_dir, "container"), "w") as out:
            proc = subprocess.Popen(command, stdout=out, stderr=subprocess.PIPE)
            return_code = proc.wait()

        # If the container is up, wait for it to finish
        if return_code == 0:

            print >> sys.stderr, "Waiting for docker container %s" % (container)

            command = list(_docker_command(args))
            command.extend(["wait", container])

            # Wait for the container to finish
            proc = subprocess.Popen(command, stdout=subprocess.PIPE)
            proc.wait()

            container_exit_code = int(proc.stdout.read(1))

            status = mesos_pb2.PluggableTermination()
            status.status = container_exit_code
            status.killed = False
            status.message = "wait/docker: ok"

            print >> sys.stderr, "Container exited with exit code %d" % (container_exit_code)
            return status

        time.sleep(interval)
        timeout -= interval

    return 1


def main(args):

    # Simple default function for ignoring a command
    ignore = lambda c, a: 0

    commands = {
        "launch": launch,
        "destroy": destroy,
        "usage": usage,
        "wait": wait,

        "update": ignore,
        "recover": ignore,
    }

    if args.command not in commands:
        print >> sys.stderr, "Invalid command %s" % (args.command)
        exit(2)

    return commands[args.command](args.container, args)


if __name__ == "__main__":

    parser = argparse.ArgumentParser(prog="docker-containerizer")
    parser.add_argument("--mesos-executor", required=False,
                        help="Path to the built-in mesos executor")
    parser.add_argument("-H", "--docker-host", required=False,
                        help="Docker host for client to connect to")
    parser.add_argument("-T", "--docker-stop-timeout", default=2,
                        help="Number of seconds to wait when stopping a container")

    # Positional arguments
    parser.add_argument("command",
                        help="Containerizer command to run")
    parser.add_argument("container",
                        help="Container ID")

    output = main(parser.parse_args())

    # Pass protobuf responses through
    if not isinstance(output, int):
        _send_status(output)
        output = 0

    exit(output)
