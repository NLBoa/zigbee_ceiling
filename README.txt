ZIGBEE CEILING: smart switch control from a Mac
================================================

Controls Third Reality Smart Switch Gen3 (3RSS009Z) Zigbee switches from a Mac, as a test
run before moving the setup to a Raspberry Pi.


HOW IT WORKS
------------

  Switches  <-- Zigbee radio -->  USB dongle  <-- serial -->  Zigbee2MQTT  <-->  Mosquitto  <-->  dashboard.py / toggle.py

  - USB dongle: Sonoff ZBDongle-P (model ZBDongle-P). It is the Zigbee coordinator and stores
    the network itself, so switches stay paired across restarts.
  - Zigbee2MQTT: talks to the dongle and turns Zigbee messages into MQTT messages. It runs
    natively with Node, not Docker, because Docker on macOS can't pass USB serial devices
    through. It also has its own advanced web UI on http://localhost:8080.
  - Mosquitto: the MQTT message broker that everything connects to (localhost:1883).
  - dashboard.py: a simple web page (http://localhost:8090) to pair new switches, name them,
    and turn them on and off.
  - toggle.py: a command-line tool that sets a switch ON, OFF or TOGGLE and prints the state
    the switch reports back.

Each switch has an MQTT topic named after it, e.g. zigbee2mqtt/bedroom_light. Publishing
{"state":"ON"} to zigbee2mqtt/bedroom_light/set turns it on, and the switch's state reports
appear on zigbee2mqtt/bedroom_light.


FILES IN THIS REPO
------------------

  README.txt                          this file
  dashboard.py                        web dashboard (pair, name, on/off)
  toggle.py                           command-line on/off/toggle
  requirements.txt                    Python dependency (paho-mqtt)
  config/configuration.example.yaml   Zigbee2MQTT config template
  config/mosquitto.conf               Mosquitto config (localhost only, no password)

Not in the repo, on purpose: ~/zigbee2mqtt/data/ (configuration.yaml, database.db,
coordinator_backup.json). These hold the Zigbee network key and pairing data. Never commit
them; back them up separately.


ONE-TIME SETUP (macOS)
----------------------

Needs Homebrew and Node 22.2+ or 24.

1. Install Mosquitto and pnpm, then install the Mosquitto config:

     brew install mosquitto pnpm
     cp config/mosquitto.conf /opt/homebrew/etc/mosquitto/mosquitto.conf

   Homebrew does not create this config file. Without it Mosquitto quits at launch even
   though "brew services" says it started.

2. Install Zigbee2MQTT:

     git clone --depth 1 https://github.com/Koenkk/zigbee2mqtt.git ~/zigbee2mqtt
     cd ~/zigbee2mqtt && pnpm install --frozen-lockfile

3. Plug in the dongle and find its port (use the cu. one, not tty.):

     ls /dev/cu.*          # e.g. /dev/cu.usbserial-10

4. Create the Zigbee2MQTT config, then edit serial.port if your port differs:

     cp config/configuration.example.yaml ~/zigbee2mqtt/data/configuration.yaml

5. Set up Python for the scripts (from this repo folder):

     python3 -m venv .venv
     .venv/bin/pip install -r requirements.txt


RUNNING IT
----------

Start these in order, each in its own terminal tab.

  1. Mosquitto (a background service that also starts at login):
       brew services start mosquitto

  2. Zigbee2MQTT. Wait for "Zigbee2MQTT started!":
       cd ~/zigbee2mqtt && pnpm start

  3. Dashboard (from this repo folder):
       .venv/bin/python dashboard.py
     Then open http://localhost:8090

To stop: press Ctrl-C in the Zigbee2MQTT and dashboard tabs, and run
"brew services stop mosquitto" if you want Mosquitto off too.


PAIRING A SWITCH
----------------

1. On the dashboard, click "Pair a new switch". Pairing stays open for about 4 minutes.
2. Hold the switch's button for about 30 seconds, until the LED blinks fast (2 blinks per
   second). Then let go: holding it longer makes the switch leave and rejoin.
3. Within about 20 seconds the switch appears with an orange border. Wait for
   "... is ready" under Recent activity.
4. Click "Name this switch" and give it a name like kitchen_light (letters, numbers and _;
   + and # are not allowed).
5. Pair one switch at a time. Click "Stop pairing" when you're done.


CONTROLLING SWITCHES
--------------------

  Dashboard:   On / Off buttons on each switch's card.

  Command line:
    .venv/bin/python toggle.py ON                         # bedroom_light by default
    .venv/bin/python toggle.py TOGGLE --device kitchen_light
    .venv/bin/python toggle.py --watch                    # print state updates until Ctrl-C

  Raw MQTT:
    mosquitto_pub -h localhost -t zigbee2mqtt/bedroom_light/set -m '{"state":"TOGGLE"}'
    mosquitto_sub -h localhost -t zigbee2mqtt/bedroom_light -v


UNPLUGGING THE DONGLE
---------------------

Stop Zigbee2MQTT first (Ctrl-C), wait for "Stopped Zigbee2MQTT", then unplug. Pairing is
kept on the dongle. Plug it back into the same USB socket: a different socket can change
the /dev/cu.* name, and then serial.port in the config must be updated.


TROUBLESHOOTING
---------------

  "MQTT failed to connect"
      Mosquitto isn't running. Check with "brew services info mosquitto" and make sure
      /opt/homebrew/etc/mosquitto/mosquitto.conf exists.

  Serial port errors on start
      Wrong port (re-check "ls /dev/cu.*") or another program has it open
      (check with "lsof /dev/cu.usbserial-10").

  Dashboard says "Waiting for MQTT broker"
      Start Mosquitto.

  Dashboard shows switches but nothing happens
      Start Zigbee2MQTT.

Logs: ~/zigbee2mqtt/data/log/


MOVING TO THE RASPBERRY PI (DOCKER)
-----------------------------------

1. Stop Zigbee2MQTT on the Mac and move the dongle to the Pi.
2. Copy configuration.yaml, database.db and coordinator_backup.json from
   ~/zigbee2mqtt/data/ into the Pi's Zigbee2MQTT data folder.
3. In the copied configuration.yaml, change:
     mqtt.server  ->  mqtt://mosquitto:1883
     serial.port  ->  /dev/zigbee
   Never change network_key, pan_id or ext_pan_id. Changing them means re-pairing every switch.
4. Map the dongle into the container as /dev/zigbee, using its
   /dev/serial/by-id/usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_...-port0 path.
5. Use Zigbee2MQTT image version 2.14.2 or newer.
6. Mosquitto on the Pi also needs "listener 1883" and auth (or allow_anonymous true) in its
   config, or Zigbee2MQTT can't connect.
7. To reach the dashboard from other devices:
     python dashboard.py --mqtt-host <broker> --bind 0.0.0.0
   It has no login, so only do this on a trusted home network.
