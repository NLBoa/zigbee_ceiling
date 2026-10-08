#!/usr/bin/env python3
"""Set a Zigbee2MQTT switch ON/OFF/TOGGLE and print the state reports it sends back.

Usage:
    toggle.py TOGGLE                 # send, print reports, exit after --timeout seconds
    toggle.py ON --device bedroom_light
    toggle.py --watch                # just print state updates until Ctrl-C
    toggle.py OFF --watch            # send, then keep printing updates
"""
import argparse
import json
import sys
import time

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("state", nargs="?", type=str.upper, choices=["ON", "OFF", "TOGGLE"],
                   help="state to send (omit to only watch)")
    p.add_argument("--device", default="bedroom_light", help="Zigbee2MQTT friendly name")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--base-topic", default="zigbee2mqtt")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="seconds to wait for reports after sending (ignored with --watch)")
    p.add_argument("--watch", action="store_true", help="keep printing updates until Ctrl-C")
    args = p.parse_args()

    if args.state is None and not args.watch:
        p.error("give a state (ON/OFF/TOGGLE) and/or --watch")

    state_topic = f"{args.base_topic}/{args.device}"
    set_topic = f"{state_topic}/set"
    received = 0

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print(f"MQTT connect failed: {reason_code}", file=sys.stderr)
            client.disconnect()
            return
        # Subscribe first; the command is sent from on_subscribe so no report is missed.
        client.subscribe(state_topic)

    def on_subscribe(client, userdata, mid, reason_code_list, properties):
        if reason_code_list and reason_code_list[0].is_failure:
            print(f"Subscribe failed: {reason_code_list[0]}", file=sys.stderr)
            client.disconnect()
            return
        print(f"Subscribed to {state_topic}")
        if args.state:
            client.publish(set_topic, json.dumps({"state": args.state}))
            print(f"Sent {{\"state\": \"{args.state}\"}} to {set_topic}")

    def on_message(client, userdata, msg):
        nonlocal received
        if msg.retain:  # skip stale retained state, only show live reports
            return
        received += 1
        try:
            data = json.loads(msg.payload)
        except ValueError:
            print(f"{time.strftime('%H:%M:%S')}  (non-JSON) {msg.payload!r}")
            return
        extras = ", ".join(f"{k}={v}" for k, v in data.items() if k != "state")
        print(f"{time.strftime('%H:%M:%S')}  state={data.get('state', '?')}  {extras}")

    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        client.connect(args.host, args.port, keepalive=30)
    except OSError as e:
        print(f"Cannot reach MQTT broker at {args.host}:{args.port}: {e}", file=sys.stderr)
        return 1

    try:
        if args.watch:
            client.loop_forever()
        else:
            client.loop_start()
            time.sleep(args.timeout)
            client.loop_stop()
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()

    if args.state and not args.watch and received == 0:
        print(f"No state report from '{args.device}' within {args.timeout}s "
              "(check the friendly name, or the device may be asleep/out of range)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
