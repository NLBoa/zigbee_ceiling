#!/usr/bin/env python3
"""Small local dashboard for Zigbee2MQTT: pair new switches, name them, turn them on/off.

Run:   ~/zigbee2mqtt-test/dashboard.py
Open:  http://localhost:8090

Options: --mqtt-host/--mqtt-port/--base-topic (where Zigbee2MQTT publishes),
         --bind/--port (where this page is served; localhost only by default, there is no login).
"""
import argparse
import ipaddress
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

MAX_EVENTS = 20


class Bridge:
    """Keeps a live picture of Zigbee2MQTT (devices, states, pairing window) from MQTT."""

    def __init__(self, host, port, base_topic):
        self.base = base_topic
        self.lock = threading.Lock()
        self.connected = False
        self.devices = {}          # friendly_name -> device info
        self.states = {}           # friendly_name -> last state payload
        self.last_seen = {}        # friendly_name -> epoch seconds
        self.state_requested = set()
        self.permit_join_end = None  # epoch ms while pairing is open, else None
        self.events = []
        self.pending = {}          # transaction id -> [threading.Event, response]

        self.client = mqtt.Client(CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()  # also reconnects automatically

    # ---- MQTT callbacks -------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print(f"MQTT connect failed: {reason_code}", file=sys.stderr)
            return
        self.connected = True
        b = self.base
        client.subscribe([
            (f"{b}/bridge/devices", 0),
            (f"{b}/bridge/info", 0),
            (f"{b}/bridge/event", 0),
            (f"{b}/bridge/response/#", 0),
            (f"{b}/+", 0),  # device state reports: <base>/<friendly_name>
        ])

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        topic = msg.topic
        b = self.base
        if topic == f"{b}/bridge/devices":
            self._update_devices(data)
        elif topic == f"{b}/bridge/info":
            # Only the pairing fields are kept; bridge/info also carries the full config.
            with self.lock:
                self.permit_join_end = data.get("permit_join_end") if data.get("permit_join") else None
        elif topic == f"{b}/bridge/event":
            self._add_event(data)
        elif topic.startswith(f"{b}/bridge/response/"):
            waiter = self.pending.get(data.get("transaction"))
            if waiter:
                waiter[1] = data
                waiter[0].set()
        elif topic.count("/") == b.count("/") + 1 and isinstance(data, dict):
            name = topic[len(b) + 1:]
            if name != "bridge":
                with self.lock:
                    self.states[name] = data
                    self.last_seen[name] = time.time()

    def _update_devices(self, device_list):
        devices = {}
        for d in device_list:
            if d.get("type") == "Coordinator":
                continue
            definition = d.get("definition") or {}
            is_switch = any(
                e.get("type") in ("switch", "light")
                and any(f.get("property") == "state" for f in e.get("features", []))
                for e in definition.get("exposes", [])
            )
            devices[d["friendly_name"]] = {
                "friendly_name": d["friendly_name"],
                "ieee_address": d["ieee_address"],
                "model": definition.get("model") or d.get("model_id"),
                "vendor": definition.get("vendor") or d.get("manufacturer"),
                "description": definition.get("description"),
                "power_source": d.get("power_source"),
                "interview_completed": d.get("interview_completed", False),
                "interview_state": d.get("interview_state"),
                "supported": d.get("supported", False),
                "is_switch": is_switch,
            }
        with self.lock:
            self.devices = devices
            need_state = [n for n, d in devices.items()
                          if d["is_switch"] and d["interview_completed"]
                          and n not in self.states and n not in self.state_requested]
            self.state_requested.update(need_state)
        # Zigbee2MQTT does not retain device state, so ask each switch once for its current state.
        for name in need_state:
            self.client.publish(f"{self.base}/{name}/get", json.dumps({"state": ""}))

    def _add_event(self, event):
        data = event.get("data") or {}
        entry = {
            "time": time.time(),
            "type": event.get("type"),
            "friendly_name": data.get("friendly_name"),
            "ieee_address": data.get("ieee_address"),
            "status": data.get("status"),
            "model": (data.get("definition") or {}).get("model"),
        }
        with self.lock:
            self.events.append(entry)
            del self.events[:-MAX_EVENTS]

    # ---- Actions --------------------------------------------------------

    def _request(self, name, payload, timeout=10):
        """Send a bridge request and wait for Zigbee2MQTT's response (matched by transaction id)."""
        tx = uuid.uuid4().hex[:12]
        waiter = [threading.Event(), None]
        self.pending[tx] = waiter
        try:
            self.client.publish(f"{self.base}/bridge/request/{name}", json.dumps({**payload, "transaction": tx}))
            if not waiter[0].wait(timeout):
                return {"status": "error", "error": "No response from Zigbee2MQTT. Is it running?"}
            return waiter[1]
        finally:
            self.pending.pop(tx, None)

    def set_state(self, name, state):
        state = str(state).upper()
        if state not in ("ON", "OFF", "TOGGLE"):
            return {"status": "error", "error": "state must be ON, OFF or TOGGLE"}
        with self.lock:
            device = self.devices.get(name)
        if not device or not device["is_switch"]:
            return {"status": "error", "error": f"Unknown switch: {name}"}
        self.client.publish(f"{self.base}/{name}/set", json.dumps({"state": state}))
        return {"status": "ok"}

    def permit_join(self, seconds):
        seconds = max(0, min(254, int(seconds)))
        return self._request("permit_join", {"time": seconds})

    def rename(self, old, new):
        new = str(new).strip()
        with self.lock:
            known = old in self.devices
        if not known:
            return {"status": "error", "error": f"Unknown device: {old}"}
        if not new:
            return {"status": "error", "error": "Name cannot be empty"}
        resp = self._request("device/rename", {"from": old, "to": new})
        if resp.get("status") == "ok":
            with self.lock:
                if old in self.states:
                    self.states[new] = self.states.pop(old)
                if old in self.last_seen:
                    self.last_seen[new] = self.last_seen.pop(old)
        return resp

    def snapshot(self):
        with self.lock:
            devices = []
            for name, d in sorted(self.devices.items()):
                devices.append({
                    **d,
                    "state": self.states.get(name, {}),
                    "last_seen": self.last_seen.get(name),
                })
            return {
                "connected": self.connected,
                "now_ms": int(time.time() * 1000),
                "permit_join_end": self.permit_join_end,
                "devices": devices,
                "events": list(reversed(self.events)),
            }


def make_handler(bridge, allowed_hosts):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console quiet (the page polls every second)
            pass

        def _host_ok(self):
            # Blocks DNS-rebinding tricks when serving on localhost only.
            if allowed_hosts is None:
                return True
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in allowed_hosts

        def _send(self, code, body, ctype="application/json"):
            raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"status": "error", "error": "Forbidden host"})
            if self.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, bridge.snapshot())
            self._send(404, {"status": "error", "error": "Not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"status": "error", "error": "Forbidden host"})
            # Requiring JSON forces a CORS preflight, so other websites can't drive this API.
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._send(415, {"status": "error", "error": "Expected application/json"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                return self._send(413, {"status": "error", "error": "Request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/set":
                    result = bridge.set_state(body["device"], body["state"])
                elif self.path == "/api/permit_join":
                    result = bridge.permit_join(body.get("time", 254))
                elif self.path == "/api/rename":
                    result = bridge.rename(body["from"], body["to"])
                else:
                    return self._send(404, {"status": "error", "error": "Not found"})
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"status": "error", "error": f"Bad request: {e}"})
            self._send(200 if result.get("status") == "ok" else 400, result)

    return Handler


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zigbee Switches</title>
<style>
  :root {
    --bg: #f5f5f4; --card: #ffffff; --text: #1c1917; --muted: #78716c; --line: #e7e5e4;
    --on: #16a34a; --on-text: #ffffff; --off: #e7e5e4; --accent: #2563eb; --warn: #b45309; --err: #dc2626;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0c0a09; --card: #1c1917; --text: #f5f5f4; --muted: #a8a29e; --line: #292524;
      --on: #22c55e; --on-text: #052e16; --off: #292524; --accent: #60a5fa; --warn: #fbbf24; --err: #f87171;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 16px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  main { max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; }
  header { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  h1 { font-size: 24px; margin: 0; }
  h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 28px 0 10px; }
  #conn { font-size: 14px; color: var(--muted); }
  #conn.bad { color: var(--err); }
  .panel, .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }
  .panel p { margin: 6px 0 0; color: var(--muted); }
  .panel.pairing { border-color: var(--accent); }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  button { font: inherit; border-radius: 8px; border: 1px solid var(--line); background: var(--card);
           color: var(--text); padding: 8px 14px; cursor: pointer; }
  button:hover { border-color: var(--muted); }
  button:disabled { opacity: .5; cursor: default; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.link { border: none; background: none; color: var(--accent); padding: 4px 0; }
  .countdown { font-variant-numeric: tabular-nums; font-weight: 600; }
  #devices { display: grid; gap: 12px; }
  .card.new { border-color: var(--warn); }
  .name { font-size: 18px; font-weight: 600; word-break: break-all; }
  .meta { font-size: 13px; color: var(--muted); margin-top: 2px; }
  .badge { font-size: 12px; font-weight: 600; color: var(--warn); }
  .toggle { display: inline-flex; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  .toggle button { border: none; border-radius: 0; min-width: 64px; padding: 10px 16px; font-weight: 600; }
  .toggle button.active.on { background: var(--on); color: var(--on-text); }
  .toggle button.active.off { background: var(--off); }
  .empty { color: var(--muted); }
  #events { list-style: none; margin: 0; padding: 0; font-size: 14px; }
  #events li { padding: 6px 0; border-bottom: 1px solid var(--line); display: flex; gap: 10px; }
  #events time { color: var(--muted); font-variant-numeric: tabular-nums; flex: none; }
  #toast { position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); max-width: calc(100% - 32px);
           background: var(--text); color: var(--bg); padding: 10px 16px; border-radius: 8px; display: none; }
  #toast.err { background: var(--err); color: #fff; }
</style>
</head>
<body>
<main>
  <header>
    <h1>Zigbee Switches</h1>
    <span id="conn">Connecting…</span>
  </header>

  <h2>Pair a switch</h2>
  <div id="pair" class="panel"></div>

  <h2>Switches</h2>
  <div id="devices"><p class="empty">Loading…</p></div>

  <h2>Recent activity</h2>
  <ul id="events"></ul>
</main>
<div id="toast" role="status"></div>

<script>
const $ = (s) => document.querySelector(s);
let snap = null, clockOffset = 0, toastTimer = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = isErr ? 'err' : '';
  t.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.style.display = 'none'), 4000);
}

async function api(path, body) {
  const opts = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({ status: 'error', error: 'Bad response from dashboard' }));
  if (!r.ok || j.status === 'error') throw new Error(j.error || r.statusText);
  return j;
}

async function refresh() {
  try {
    snap = await api('/api/state');
    clockOffset = snap.now_ms - Date.now();
  } catch (e) {
    snap = null;
  }
  render();
}

function ago(epochSec) {
  if (!epochSec) return 'no report yet';
  const s = Math.max(0, Math.round(Date.now() / 1000 + clockOffset / 1000 - epochSec));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function render() {
  const conn = $('#conn');
  if (!snap) { conn.textContent = 'Dashboard server not reachable'; conn.className = 'bad'; return; }
  conn.textContent = snap.connected ? 'Connected to MQTT' : 'Waiting for MQTT broker…';
  conn.className = snap.connected ? '' : 'bad';
  renderPairing();
  renderDevices();
  renderEvents();
}

function renderPairing() {
  const p = $('#pair');
  const left = snap.permit_join_end
    ? Math.max(0, Math.round((snap.permit_join_end - (Date.now() + clockOffset)) / 1000)) : 0;
  p.replaceChildren();
  const row = el('div', 'row');
  if (left > 0) {
    p.className = 'panel pairing';
    const txt = el('div');
    txt.append('Pairing is open for ', el('span', 'countdown', `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`));
    const stop = el('button', '', 'Stop pairing');
    stop.onclick = () => permitJoin(0, stop);
    row.append(txt, stop);
    p.append(row, el('p', '', 'Hold the button on the switch for about 30 seconds until the LED blinks fast (2 blinks per second), then let go. It appears below within ~20 seconds. Pair one switch at a time.'));
  } else {
    p.className = 'panel';
    const start = el('button', 'primary', 'Pair a new switch');
    start.onclick = () => permitJoin(254, start);
    row.append(el('div', '', 'Pairing is closed.'), start);
    p.append(row, el('p', '', 'Click "Pair a new switch" to let a new switch join for about 4 minutes.'));
  }
}

function renderDevices() {
  const list = $('#devices');
  if (!snap.devices.length) {
    list.replaceChildren(el('p', 'empty', 'No switches paired yet.'));
    return;
  }
  list.replaceChildren(...snap.devices.map(card));
}

function card(d) {
  const isNew = d.friendly_name === d.ieee_address;
  const c = el('div', 'card' + (isNew ? ' new' : ''));
  const row = el('div', 'row');
  const info = el('div');
  info.append(el('div', 'name', d.friendly_name));
  if (isNew) info.append(el('div', 'badge', 'New: give it a name'));
  const bits = [ [d.vendor, d.model].filter(Boolean).join(' ') ];
  if (d.state.battery != null) bits.push(`battery ${Math.round(d.state.battery)}%`);
  if (d.state.linkquality != null) bits.push(`signal ${d.state.linkquality}`);
  bits.push(ago(d.last_seen));
  info.append(el('div', 'meta', bits.filter(Boolean).join(' · ')));
  const rn = el('button', 'link', isNew ? 'Name this switch' : 'Rename');
  rn.onclick = () => rename(d.friendly_name, isNew);
  info.append(rn);
  row.append(info);

  if (!d.interview_completed) {
    row.append(el('div', 'badge', d.interview_state === 'FAILED' ? 'Setup failed: pair it again' : 'Setting up…'));
  } else if (!d.is_switch) {
    row.append(el('div', 'meta', 'Not an on/off switch'));
  } else {
    const st = d.state.state;
    const t = el('div', 'toggle');
    for (const s of ['ON', 'OFF']) {
      const b = el('button', s.toLowerCase() + (st === s ? ' active' : ''), s === 'ON' ? 'On' : 'Off');
      b.onclick = () => setState(d.friendly_name, s, t);
      t.append(b);
    }
    row.append(t);
  }
  c.append(row);
  return c;
}

const EVENT_TEXT = {
  device_joined: (e) => `${e.friendly_name} joined`,
  device_leave: (e) => `${e.friendly_name} left the network`,
  device_interview: (e) =>
    e.status === 'started' ? `Setting up ${e.friendly_name}…`
    : e.status === 'successful' ? `${e.friendly_name} is ready${e.model ? ` (${e.model})` : ''}`
    : `Setup failed for ${e.friendly_name}. Put it in pairing mode again`,
};

function renderEvents() {
  const ul = $('#events');
  const items = snap.events.filter((e) => EVENT_TEXT[e.type]);
  if (!items.length) { ul.replaceChildren(el('li', 'empty', 'Nothing yet.')); return; }
  ul.replaceChildren(...items.map((e) => {
    const li = el('li');
    li.append(el('time', '', new Date(e.time * 1000).toLocaleTimeString()), el('span', '', EVENT_TEXT[e.type](e)));
    return li;
  }));
}

async function setState(name, state, group) {
  group.querySelectorAll('button').forEach((b) => (b.disabled = true));
  try { await api('/api/set', { device: name, state }); }
  catch (e) { toast(e.message, true); }
  setTimeout(refresh, 400);
}

async function permitJoin(seconds, btn) {
  btn.disabled = true;
  try { await api('/api/permit_join', { time: seconds }); toast(seconds ? 'Pairing open' : 'Pairing closed'); }
  catch (e) { toast(e.message, true); btn.disabled = false; }
  refresh();
}

async function rename(current, isNew) {
  const name = prompt(isNew ? 'Name for this switch (e.g. kitchen_light):' : `New name for ${current}:`, isNew ? '' : current);
  if (!name || name.trim() === current) return;
  try { await api('/api/rename', { from: current, to: name.trim() }); toast(`Renamed to ${name.trim()}`); }
  catch (e) { toast(e.message, true); }
  refresh();
}

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mqtt-host", default="localhost")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--base-topic", default="zigbee2mqtt")
    p.add_argument("--bind", default="127.0.0.1", help="address to serve on (0.0.0.0 = whole LAN, no login!)")
    p.add_argument("--port", type=int, default=8090)
    args = p.parse_args()

    try:
        loopback = ipaddress.ip_address(args.bind).is_loopback
    except ValueError:
        loopback = args.bind == "localhost"
    allowed_hosts = {"localhost", "127.0.0.1", "::1"} if loopback else None

    bridge = Bridge(args.mqtt_host, args.mqtt_port, args.base_topic)
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(bridge, allowed_hosts))
    print(f"Dashboard on http://{'localhost' if loopback else args.bind}:{args.port}  "
          f"(MQTT {args.mqtt_host}:{args.mqtt_port}, topic {args.base_topic})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        bridge.client.loop_stop()
        bridge.client.disconnect()


if __name__ == "__main__":
    main()
