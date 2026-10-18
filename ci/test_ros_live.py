"""Optional live ROS Noetic test: source its setup.bash, then python3 ci/test_ros_live.py.

Uses a private ROS-master on a free port, real telemetry/subscribers and fake
TCP OpenOCD. No board; does not test the Windows/WSL bridge or UART.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import xmlrpc.client

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "ci"), str(ROOT / "stm32-ros-alt-channel/scripts")]
from fake_openocd import FakeOpenOcd
from process_tools import spawn_logged, stop_process


def main():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["ROS_MASTER_URI"] = "http://127.0.0.1:%d" % port
    os.environ["ROS_HOSTNAME"] = "127.0.0.1"
    work = ROOT / "build/ci/ros-live"
    work.mkdir(parents=True, exist_ok=True)
    master = spawn_logged(["roscore", "-p", str(port)], work / "master.log")
    node = rospy = None
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                xmlrpc.client.ServerProxy(os.environ["ROS_MASTER_URI"]).getPid("/test")
                break
            except OSError:
                if master.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("ROS-master did not start")
                time.sleep(0.2)
        import rospy
        from std_msgs.msg import Int32, UInt32
        rospy.init_node("shared_tools_test", anonymous=True, disable_signals=True)
        seen = {}
        subscribers = [
            rospy.Subscriber("/signed", Int32, lambda msg: seen.update(signed=msg.data)),
            rospy.Subscriber("/shift", UInt32, lambda msg: seen.update(shift=msg.data)),
        ]
        fields = [
            dict(topic="/signed", address=0x102, kind="int32", message="std_msgs/Int32", mask=65535, expr="test"),
            dict(topic="/shift", address=0x100, kind="uint32", message="std_msgs/UInt32", mask=0xFF00, shift=8, expr="test"),
        ]
        spec = work / "addresses.json"
        spec.write_text(json.dumps({"fields": fields}), encoding="utf-8")
        memory = {0x100: lambda n: 0xFFFF1234, 0x104: lambda n: 0x5678}
        with FakeOpenOcd(memory, noise={0: "before", 1: "midecho"}) as server:
            # Configure only endpoint/arguments; production telemetry main remains unchanged.
            code = ("import sys; sys.path.insert(0,%r); import telemetry_node as t; "
                    "link=t.OpenOcdLink; t.OpenOcdLink=lambda:link(port=%d); "
                    "sys.argv=['telemetry',%r]; sys.exit(t.main())") % (
                        str(ROOT / "stm32-ros-alt-channel/scripts"), server.port, str(spec))
            node = spawn_logged([sys.executable, "-c", code], work / "node.log")
            deadline = time.monotonic() + 15
            while len(seen) < 2 and time.monotonic() < deadline:
                if node.poll() is not None:
                    raise RuntimeError("telemetry_node exited")
                time.sleep(0.1)
            if seen != {"signed": -1, "shift": 18}:
                raise RuntimeError("unexpected ROS values: %s" % seen)
            import ros1_channel as channel
            channel.ROS_MASTER_URI = os.environ["ROS_MASTER_URI"]
            # Run the same command locally in WSL; only the Windows launcher is replaced.
            channel.wsl = lambda command, timeout=120: subprocess.run(
                ["bash", "-lc", command], capture_output=True, text=True, timeout=timeout)
            listing = channel.wsl("source /opt/ros/noetic/setup.bash && export ROS_MASTER_URI=%s && rostopic list -p" % channel.ROS_MASTER_URI)
            topic = channel.select_hz_topic(listing.stdout, "/signed")
            ok, detail = channel.measure_frequency(topic)
            if not ok:
                raise RuntimeError("real frequency check failed: " + detail)
            print("PASS real ROS frequency", topic, detail)
            print("PASS real ROS Noetic master + telemetry_node + TCP fake OpenOCD:", seen)
            for subscriber in subscribers:
                subscriber.unregister()
    finally:
        stop_process(node)
        if rospy is not None:
            rospy.signal_shutdown("test complete")
        stop_process(master)


if __name__ == "__main__":
    main()
