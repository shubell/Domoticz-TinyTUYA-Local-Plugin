# Domoticz TinyTUYA Local Plugin
#
# Rewritten local-control version
#

"""
<plugin key="tinytuyalocal" name="TinyTUYA (Local Control)" author="Xenomes / rewritten" version="0.8" wikilink="" externallink="">
    <description>
        <h2>TinyTUYA Plugin Local Control</h2><br/>
        <br/>
        <h3>Features</h3>
        <ul style="list-style-type:square">
            <li>Local control via TinyTuya</li>
            <li>On/Off, selectors, dimmers, sensors</li>
            <li>Reads devices.json from plugin folder</li>
        </ul>
    </description>
    <params>
        <param field="Mode6" label="Debug" width="150px">
            <options>
                <option label="None" value="0" default="true" />
                <option label="Python Only" value="2"/>
                <option label="Basic Debugging" value="62"/>
                <option label="Basic + Messages" value="126"/>
                <option label="Queue" value="128"/>
                <option label="Connections Only" value="16"/>
                <option label="Connections + Queue" value="144"/>
                <option label="All" value="-1"/>
            </options>
        </param>
    </params>
</plugin>
"""

try:
    import DomoticzEx as Domoticz
except ImportError:
    import fakeDomoticz as Domoticz

import tinytuya
import os
import sys
import json
import time
import base64
import random


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_plugin = None
devs = []
testData = False
snapshot_data = {}
# Non-pollable device categories (skip status polling)
NON_POLLABLE_CATEGORIES = {"infrared", "infrared_ac", "wg2", "gateway"}

# DP codes that indicate an IR device
IR_DP_CODES = {"ir_send", "ir_study_code", "ir_learn"}

last_poll_times = {}
device_offline = {}          # True if last poll failed

# ---------------------------------------------------------------------------
# Main plugin
# ---------------------------------------------------------------------------

class BasePlugin:
    def __init__(self):
        self.debug = False

    def onStart(self):
        global testData, snapshot_data, devs

        Domoticz.Log("TinyTUYA local plugin started")
        Domoticz.Heartbeat(30)   
        Domoticz.Log("Heartbeat set to 30 seconds")

        if Parameters["Mode6"] != "0":
            self.debug = True
            Domoticz.Debugging(int(Parameters["Mode6"]))
            DumpConfigToLog()

        testData = os.path.isfile(os.path.join(Parameters["HomeFolder"], "testdata.on"))
        if testData:
            Domoticz.Error("!!! Warning: plugin is using local snapshot/test mode !!!")

        devs = load_devices_json()
        if not devs:
            Domoticz.Error("No devices loaded from devices.json")
            return

        if testData:
            snapshot_path = os.path.join(Parameters["HomeFolder"], "snapshot.json")
            if os.path.isfile(snapshot_path):
                try:
                    with open(snapshot_path, "r", encoding="utf-8") as f:
                        snapshot_data = json.load(f)
                except Exception as e:
                    Domoticz.Error("Failed loading snapshot.json: {} line {}".format(str(e), lineno()))

        build_devices()
        # Initialize poll timers to now, so the first heartbeat won't poll everything at once
        for dev in devs:
            # Spread initial cooldown over 0–300 seconds, first loop minmum cooldown is 30 + random int as we assume all are offline/unreachable
            last_poll_times[dev.get("id")] = time.time() + random.randint(0, 300) - 30 #normal cooldown is 30s so we deduct that so some can get updated in the first loop which we call after this
        # Do NOT call refresh_all_devices(startup=True) as it times out... takes too long
        refresh_all_devices(startup=False)

    def onStop(self):
        Domoticz.Log("TinyTUYA local plugin stopped")

    def onConnect(self, Connection, Status, Description):
        Domoticz.Debug("onConnect called")

    def onMessage(self, Connection, Data):
        Domoticz.Debug("onMessage called")

    def onDisconnect(self, Connection):
        Domoticz.Debug("onDisconnect called")

    def onNotification(self, Name, Subject, Text, Status, Priority, Sound, ImageFile):
        Domoticz.Log(
            "Notification: {}, {}, {}, {}, {}, {}, {}".format(
                Name, Subject, Text, Status, Priority, Sound, ImageFile
            )
        )

    def onHeartbeat(self):
        refresh_all_devices(startup=False)

    def onDeviceRemoved(self, DeviceID, Unit):
        Domoticz.Log("Device removed: {} / {}".format(DeviceID, Unit))

    def onCommand(self, DeviceID, Unit, Command, Level, Color):
        try:
            Domoticz.Debug(
                "onCommand DeviceID={} Unit={} Command={} Level={} Color={}".format(
                    DeviceID, Unit, Command, Level, Color
                )
            )

            if DeviceID not in Devices or Unit not in Devices[DeviceID].Units:
                Domoticz.Error("onCommand: unknown device/unit")
                return

            dev_unit = Devices[DeviceID].Units[Unit]
            category = getConfigItem(DeviceID, "category")

            if Command == "Set Level":
                if dev_unit.Type == 244 and dev_unit.SubType == 62 and dev_unit.SwitchType == 18:
                    level_names = dev_unit.Options.get("LevelNames", "").split("|")
                    idx = int(Level / 10)
                    if 0 <= idx < len(level_names):
                        value = level_names[idx]
                        if send_command(DeviceID, Unit, value, category):
                            UpdateDevice(DeviceID, Unit, Level, 1, 0)
                else:
                    if send_command(DeviceID, Unit, Level, category):
                        UpdateDevice(DeviceID, Unit, Level, 1, 0)

            elif Command == "Set Color":
                try:
                    color_dict = eval(Color)
                except Exception:
                    color_dict = {}
                if send_command(DeviceID, Unit, color_dict, category):
                    UpdateDevice(DeviceID, Unit, Color, 1, 0)

            else:
                if dev_unit.Type == 81 and dev_unit.SubType == 1:
                    # Command is "On" or "Off" – convert to integer
                    state = 1 if Command in ["On", "True", True] else 0
                    if send_command(DeviceID, Unit, Command, category):
                        UpdateDevice(DeviceID, Unit, 0, state, 0)
                else:
                    state = False if Command in ["Off", "Closed", False] else True
                    if send_command(DeviceID, Unit, state, category):
                        UpdateDevice(DeviceID, Unit, "On" if state else "Off", 1 if state else 0, 0)

        except Exception as e:
            Domoticz.Error("onCommand failed: {} line {}".format(str(e), lineno()))


# ---------------------------------------------------------------------------
# Domoticz entry points
# ---------------------------------------------------------------------------

def onStart():
    global _plugin
    _plugin = BasePlugin()
    _plugin.onStart()

def onStop():
    global _plugin
    _plugin.onStop()

def onConnect(Connection, Status, Description):
    global _plugin
    _plugin.onConnect(Connection, Status, Description)

def onMessage(Connection, Data):
    global _plugin
    _plugin.onMessage(Connection, Data)

def onCommand(DeviceID, Unit, Command, Level, Color):
    global _plugin
    _plugin.onCommand(DeviceID, Unit, Command, Level, Color)

def onNotification(Name, Subject, Text, Status, Priority, Sound, ImageFile):
    global _plugin
    _plugin.onNotification(Name, Subject, Text, Status, Priority, Sound, ImageFile)

def onDisconnect(Connection):
    global _plugin
    _plugin.onDisconnect(Connection)

def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def load_devices_json():
    path = os.path.join(Parameters["HomeFolder"], "devices.json")
    if not os.path.isfile(path):
        Domoticz.Error("devices.json is missing in plugin folder: {}".format(path))
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            Domoticz.Error("devices.json is not a list")
            return []
    except Exception as e:
        Domoticz.Error("Failed reading devices.json: {} line {}".format(str(e), lineno()))
        return []


def build_devices():
    for dev in devs:
        try:
            dev_id = str(dev.get("id", ""))
            dev_name = str(dev.get("name", dev_id))
            dev_category = DeviceType(dev.get("category", ""))
            mapping = dev.get("mapping", {})

            for dp, item in mapping.items():
                item["dp"] = str(dp)

            code_list = [v.get("code") for v in mapping.values()]

            if dev_category in ("light", "fanlight", "pirlight"):
                if createDevice(dev_id, 1):
                    create_light_master(dev, code_list)

            for item in mapping.values():
                try:
                    unit = int(item["dp"])
                except (ValueError, TypeError):
                    Domoticz.Debug("Skipping non-numeric DP: {} ({})".format(item["dp"], item.get("code", "")))
                    continue
                if createDevice(dev_id, unit):
                    create_mapped_unit(dev, item, code_list)

            setConfigItem(dev_id, {
                "category": dev_category,
                "key": dev.get("key", ""),
                "ip": dev.get("ip", ""),
                "version": dev.get("version", "3.3"),
                "last_update": 0
            })

        except Exception as e:
            Domoticz.Error("build_devices failed: {} line {}".format(str(e), lineno()))




def refresh_all_devices(startup=False):
    current_time = time.time()
    for dev in devs:
        try:
            dev_id = str(dev.get("id", ""))
            last_poll = last_poll_times.get(dev_id, 0)
            
             # ---------- Decide cooldown based on known state ----------
            if device_offline.get(dev_id, False):
                # Offline: 4–6 minutes (180–300s) + random jitter up to 60s
                cooldown = 240 + random.randint(0, 120)   # 180..300
            else:
                # Online: normal cooldown (30s)
                cooldown = 30
                
            # Skip if cooldown hasn't elapsed
            if not startup and current_time - last_poll < cooldown:
                Domoticz.Debug("Skipping {} (cooldown {:.0f}s)".format(dev_id, cooldown))
                continue

            # ---------- Skip non-pollable devices ----------
            dev_category = dev.get("category", "")
            if dev_category in NON_POLLABLE_CATEGORIES:
                Domoticz.Debug("Skipping non-pollable device: {} ({})".format(
                    dev.get("name", dev.get("id", "")), dev_category))
                continue

            mapping = dev.get("mapping", {})
            code_list = [v.get("code") for v in mapping.values()]
            if any(code in IR_DP_CODES for code in code_list):
                Domoticz.Debug("Skipping IR device (contains IR DPs): {}".format(
                    dev.get("name", dev.get("id", ""))))
                continue

            # ---------- Poll the device ----------
            success = refresh_device(dev, startup=startup)

            # ---------- Update state ----------
            last_poll_times[dev_id] = current_time
            # If refresh_device returned True (success), mark as online; else offline
            device_offline[dev_id] = not success

        except Exception as e:
            Domoticz.Error("refresh_all_devices: {} line {}".format(str(e), lineno()))

def refresh_device(dev, startup=False):
    dev_id = str(dev.get("id", ""))
    dev_category = DeviceType(dev.get("category", ""))
    mapping = dev.get("mapping", {})
    if testData:
        dps = get_snapshot_dps(dev_id)
        if dps is None:
            return False
        status = {"dps": dps}
    else:
        status = get_local_status(dev)
        Domoticz.Debug("tuyastatus for {}: {}".format(dev_id, status))

    if not isinstance(status, dict) or "dps" not in status:
        if dev_id in Devices:
            for unit in Devices[dev_id].Units:
                current = Devices[dev_id].Units[unit]
                # Keep existing sValue/nValue, just set TimedOut=1
                UpdateDevice(dev_id, unit, current.sValue, current.nValue, 1)
        return  False   # poll failed

    dps = status.get("dps", {})
    Domoticz.Debug("dps for {}: {}".format(dev_id, dps))

    if dev_category in ("light", "fanlight", "pirlight"):
        try:
            master = dps.get("1", None)
            if master is not None and dev_id in Devices and 1 in Devices[dev_id].Units:
                UpdateDevice(dev_id, 1, bool(master), 1 if bool(master) else 0, 0)
        except Exception:
            pass

    for item in mapping.values():
        try:
            # 1) Safely convert DP to int
            try:
                unit = int(item["dp"])
            except (ValueError, TypeError):
                Domoticz.Debug("Skipping non-numeric DP: {} ({})".format(item["dp"], item.get("code", "")))
                continue

            if dev_id not in Devices or unit not in Devices[dev_id].Units:
                continue

            raw_value = dps.get(str(unit), "Key not found")
            if raw_value == "Key not found":
                continue

            # ---- Debug: raw value and item values ----
            Domoticz.Debug("Raw Value for DP {}: {} (type {})".format(unit, raw_value, type(raw_value)))
            Domoticz.Debug("Item values: {}".format(item.get('values', {})))

            currentstatus = get_scale(raw_value, item)
            Domoticz.Debug("Scaled value: {}".format(currentstatus))
            dtype = Devices[dev_id].Units[unit]
            Domoticz.Debug("Unit {} Type/SubType/SwitchType: {}/{}/{}".format(unit, dtype.Type, dtype.SubType, dtype.SwitchType))
            if str(item["code"]) in ("switch", "switch_1", "switch_2"):
                Domoticz.Debug("Final value for {}: {}".format(item["code"], currentstatus))
                UpdateDevice(dev_id, unit, currentstatus, 0 if currentstatus is False else 1, 0)

            elif str(item["code"]) == "phase_a" and str(item.get("type")) == "Raw":
                decode_phase_a(dev_id, unit, currentstatus)

            elif dtype.Type == 244 and dtype.SubType == 62 and dtype.SwitchType == 18:
                mode = ["off"] + item.get("values", {}).get("range", [])
                if str(currentstatus) in mode:
                    Domoticz.Debug("Final value for {}: {}".format(item["code"], currentstatus))
                    UpdateDevice(dev_id, unit, int(mode.index(str(currentstatus)) * 10), 1, 0)

            elif dtype.Type == 81 and dtype.SubType == 1:
                # nValue must be integer; cast to int if numeric
                nVal = int(currentstatus) if isinstance(currentstatus, (int, float)) else 0
                Domoticz.Debug("Final value for {}: {}".format(item["code"], currentstatus))
                UpdateDevice(dev_id, unit, 0, nVal, 0)
                
            elif dtype.Type == 243 and dtype.SubType == 19 and dtype.SwitchType == 13:
                # Text device – convert Bitmap or Enum to human‑readable string
                mode = ["no fault"]
                if item.get("type") == "Bitmap":
                    # Bitmap: each bit represents a fault; we need to build a string
                    labels = item.get("values", {}).get("label", [])
                    fault_list = []
                    # currentstatus is an integer bitmask
                    for i, label in enumerate(labels):
                        if int(currentstatus) & (1 << i):
                            fault_list.append(label.replace("_", " ").capitalize())
                    currenttext = ", ".join(fault_list) if fault_list else "No fault"
                else:
                    # Enum or range – look up by index
                    range_list = item.get("values", {}).get("range", [])
                    if range_list:
                        # Some devices use "range" with strings; others use "label"
                        if isinstance(range_list, list):
                            try:
                                idx = int(currentstatus)
                                if 0 <= idx < len(range_list):
                                    currenttext = range_list[idx].replace("_", " ").capitalize()
                                else:
                                    currenttext = "Unknown"
                            except:
                                currenttext = str(currentstatus)
                        else:
                            currenttext = str(currentstatus)
                    else:
                        currenttext = str(currentstatus)
                Domoticz.Debug("Final value for {}: {}".format(item["code"], currentstatus))
                UpdateDevice(dev_id, unit, currenttext, 1, 0)

            elif (dtype.Type == 243 and dtype.SubType == 29) or (dtype.Type == 248 and dtype.SubType == 1):
                signe = ""
                if str(item["code"]) == "power_a":
                    signe_val = dps.get("102", "")
                    if signe_val == "FORWARD":
                        signe = " +"
                    elif signe_val == "REVERSE":
                        signe = " -"
                    power_value = float(currentstatus) / 10.0
                elif str(item["code"]) == "power_b":
                    signe_val = dps.get("104", "")
                    if signe_val == "FORWARD":
                        signe = " +"
                    elif signe_val == "REVERSE":
                        signe = " -"
                    power_value = float(currentstatus) / 10.0
                else:
                    power_value = float(currentstatus)

                # Build sValue with correct second part
                if dtype.Type == 248:
                    # Usage Electric expects "Usage;Return" – set Return = 0
                    svalue = signe + str(power_value) + ";0.0"
                else:
                    # General/kWh expects "Counter;Usage" – keep usage in both (KR behaviour)
                    svalue = signe + str(power_value) + ";" + signe + str(power_value)

                Domoticz.Debug("Final value for {}: {}".format(item["code"], power_value))
                UpdateDevice(dev_id, unit, svalue, 0, 0)

            else:
                Domoticz.Debug("Final value for {}: {}".format(item["code"], currentstatus))
                UpdateDevice(dev_id, unit, currentstatus, 0 if currentstatus is False else 1, 0)

            battery_device(dev_id, item["code"], currentstatus)

        except Exception as e:
            Domoticz.Error("refresh_device item failed: {} line {}".format(str(e), lineno()))
    return True        

def get_local_status(dev):
    try:
        device = tinytuya.Device(
            dev_id=str(dev.get("id")),
            address=str(dev.get("ip")),
            local_key=str(dev.get("key")),
            version=str(dev.get("version", "3.3")),
            connection_timeout=4,
            connection_retry_limit=1
        )
        try:
            device.set_version(float(dev.get("version", "3.3")))
        except Exception:
            pass

        try:
            device.detect_available_dps()
        except Exception:
            pass

        return device.status()
    except Exception as e:
        Domoticz.Error("status() failed for {}: {} line {}".format(dev.get("id"), str(e), lineno()))
        return "Device Unreachable"


def get_snapshot_dps(dev_id):
    try:
        devices = snapshot_data.get("devices", [])
        found = list(filter(lambda x: x.get("id") == dev_id, devices))
        if not found:
            return None
        return found[0].get("dps", {})
    except Exception:
        return None


def send_command(ID, Unit, Status, dev_type=""):
    try:
        Domoticz.Debug(
            "SendCommand ID={} Unit={} Type={} Status={} StatusType={} IP={} Version={}".format(
                ID,
                Unit,
                dev_type,
                Status,
                type(Status),
                getConfigItem(ID, "ip"),
                getConfigItem(ID, "version")
            )
        )

        selected_device = next((d for d in devs if str(d.get("id")) == str(ID)), None)
        if not selected_device:
            Domoticz.Error("SendCommand: device not found in devices.json for ID={}".format(ID))
            return False

        item = selected_device.get("mapping", {}).get(str(Unit))

        # If it is not a "master" light and no mapping exists for this Unit, abort.
        if not item and dev_type != "light":
            Domoticz.Error("SendCommand: mapping not found for ID={} Unit={}".format(ID, Unit))
            return False

        if item:
            Status = set_scale(Status, item)

        version_value = float(getConfigItem(ID, "version") or "3.3")

        # Special case: light
        if dev_type == "light":
            bulb = tinytuya.BulbDevice(
                dev_id=str(ID),
                address=str(getConfigItem(ID, "ip")),
                local_key=str(getConfigItem(ID, "key")),
                version=version_value,
                connection_timeout=3,
                connection_retry_limit=1
            )
            bulb.set_version(version_value)

            result = None

            if isinstance(Status, (int, float)):
                bulb.turn_on()
                result = bulb.set_brightness_percentage(int(Status))

            elif isinstance(Status, dict):
                if Status.get("m") == 2:
                    bulb.turn_on()
                    result = bulb.set_colourtemp(Status.get("cw"))
                elif Status.get("m") == 3:
                    bulb.turn_on()
                    result = bulb.set_colour(
                        int(Status.get("r", 0)),
                        int(Status.get("g", 0)),
                        int(Status.get("b", 0))
                    )
                else:
                    Domoticz.Error("Unsupported color payload: {}".format(Status))
                    return False

            elif Status is True:
                result = bulb.turn_on()

            elif Status is False:
                result = bulb.turn_off()

            else:
                Domoticz.Error("Unsupported light command payload: {}".format(Status))
                return False

            Domoticz.Debug("Light command result: {}".format(result))
            return result not in (None, False)

        # Standard device
        device = tinytuya.Device(
            dev_id=str(ID),
            address=str(getConfigItem(ID, "ip")),
            local_key=str(getConfigItem(ID, "key")),
            version=version_value,
            connection_timeout=3,
            connection_retry_limit=1
        )
        device.set_version(version_value)

        result = None

        # Booleano = switch
        if isinstance(Status, bool):
            result = device.set_status(Status, switch=int(Unit))

        # Other values ​​= Arbitrary DPS
        else:
            result = device.set_value(int(Unit), Status)

        Domoticz.Debug("SendCommand result: {}".format(result))

        # Some devices return a dictionary, others return None/False on error.
        if result is None or result is False:
            Domoticz.Error("Command may have failed for ID={} Unit={} Status={}".format(ID, Unit, Status))
            return False

        return True

    except Exception as e:
        Domoticz.Error("SendCommand failed ,High-level API: {} line {}".format(str(e), lineno()))
    # --- Fallback to the old payload method ---    
        try:
            Domoticz.Debug(
                "SendCommand ID={} Unit={} Type={} Status={} StatusType={} IP={} Version={}".format(
                    ID,
                    Unit,
                    dev_type,
                    Status,
                    type(Status),
                    getConfigItem(ID, "ip"),
                    getConfigItem(ID, "version")
                )
            )

            selected_device = next((d for d in devs if str(d.get("id")) == str(ID)), None)
            if not selected_device:
                Domoticz.Error("SendCommand: device not found in devices.json for ID={}".format(ID))
                return False

            item = selected_device.get("mapping", {}).get(str(Unit))
            if not item and dev_type != "light":
                Domoticz.Error("SendCommand: mapping not found for ID={} Unit={}".format(ID, Unit))
                return False

            if item:
                Status = set_scale(Status, item)

            if dev_type == "light":
                bulb = tinytuya.BulbDevice(
                    dev_id=str(ID),
                    address=str(getConfigItem(ID, "ip")),
                    local_key=str(getConfigItem(ID, "key")),
                    version=str(getConfigItem(ID, "version")),
                    connection_timeout=3,
                    connection_retry_limit=0
                )
                try:
                    bulb.set_version(float(getConfigItem(ID, "version")))
                except Exception:
                    pass

                if isinstance(Status, (int, float)):
                    bulb.turn_on()
                    bulb.set_brightness_percentage(int(Status))
                elif isinstance(Status, dict):
                    if Status.get("m") == 2:
                        bulb.turn_on()
                        bulb.set_colourtemp(Status.get("cw"))
                    elif Status.get("m") == 3:
                        bulb.turn_on()
                        bulb.set_colour(Status.get("r"), Status.get("g"), Status.get("b"))
                elif Status is True:
                    bulb.turn_on()
                elif Status is False:
                    bulb.turn_off()
                else:
                    Domoticz.Debug("Unsupported light command payload: {}".format(Status))

                return True

            device = tinytuya.Device(
                dev_id=str(ID),
                address=str(getConfigItem(ID, "ip")),
                local_key=str(getConfigItem(ID, "key")),
                version=str(getConfigItem(ID, "version")),
                connection_timeout=3,
                connection_retry_limit=0
            )
            try:
                device.set_version(float(getConfigItem(ID, "version")))
            except Exception:
                pass

            # ... fallback code, but with tinytuya.CONTROL_NEW ...
            payload = device.generate_payload(tinytuya.CONTROL_NEW, {str(Unit): Status})
            Domoticz.Debug("SendCommand payload: {}".format(payload))
            result = device.send(payload)
            Domoticz.Debug("SendCommand result: {}".format(result))
            return True

        except Exception as e:
            Domoticz.Error("SendCommand failed Fallback: {} line {}".format(str(e), lineno()))
            return False


# ---------------------------------------------------------------------------
# Device creation
# ---------------------------------------------------------------------------

def create_light_master(dev, code_list):
    dev_id = str(dev["id"])
    name = dev["name"]

    if (
        ("switch_led" in code_list or "led_switch" in code_list or "switch_led_1" in code_list or "switch_led_2" in code_list)
        and "work_mode" in code_list
        and ("colour_data" in code_list or "colour_data_v2" in code_list)
        and ("temp_value" in code_list or "temp_value_v2" in code_list)
        and ("bright_value" in code_list or "bright_value_v2" in code_list)
    ):
        Domoticz.Unit(Name=name, DeviceID=dev_id, Unit=1, Type=241, Subtype=4, Switchtype=7, Used=1).Create()
    elif (
        ("switch_led" in code_list or "led_switch" in code_list)
        and "work_mode" in code_list
        and ("colour_data" in code_list or "colour_data_v2" in code_list)
        and ("temp_value" in code_list or "temp_value_v2" in code_list)
    ):
        Domoticz.Unit(Name=name, DeviceID=dev_id, Unit=1, Type=241, Subtype=1, Switchtype=7, Used=1).Create()
    elif (
        ("switch_led" in code_list or "led_switch" in code_list)
        and ("bright_value" in code_list or "bright_value_v2" in code_list)
    ):
        Domoticz.Unit(Name=name, DeviceID=dev_id, Unit=1, Type=241, Subtype=3, Switchtype=7, Used=1).Create()
    else:
        Domoticz.Unit(Name=name, DeviceID=dev_id, Unit=1, Type=244, Subtype=73, Switchtype=0, Used=1).Create()


def create_mapped_unit(dev, item, code_list):
    dev_id = str(dev["id"])
    name = dev["name"]
    unit = int(item["dp"])
    code = item.get("code", "")
    values = item.get("values", {})

    switch_codes = (
        [f"switch{i}" for i in range(1, 9)] +
        [f"switch_{i}" for i in range(1, 9)] +
        ["switch", "fan_switch", "window_check", "child_lock", "muffling", "light",
         "colour_switch", "anion", "switch_charge", "laser_switch", "doorcontact",
         "doorcontact_state", "door_control_1", "door_state_1", "smartlock",
         "position", "switch_pir", "fan_speed", "MachineRainMode"]
    )

    selector_codes = (
        ["mode", "work_mode", "speed", "fan_direction", "Alarmtype", "AlarmPeriod",
         "alarm_state", "status", "alarm_volume", "alarm_lock", "cistern", "suction",
         "fan_speed_enum", "dehumidify_set_value", "device_mode", "pir_sensitivity",
         "manual_feed", "feed_state", "feed_report", "switch_mode", "laser_switch",
         "defrost_state", "compressor_state", "MachineControlCmd"] +
        [f"switch{i}_value" for i in range(1, 9)] +
        [f"switch_type_{i}" for i in range(1, 9)]
    )

    temperature_codes = [
        "temp_current", "intemp", "outtemp", "whjtemp", "cmptemp", "wttemp", "hqtemp",
        "va_temperature", "sub1_temp", "sub2_temp", "sub3_temp", "Temperature",
        "temp_indoor", "temperature", "temp_top", "temp_bottom"
    ]

    humidity_codes = ["va_humidity", "sub1_hum", "sub2_hum", "sub3_hum", "humidity_indoor"]
    percentage_codes = ["electricity_left", "filter"]
    custom_codes = ["co2_value", "pm25", "roll_brush", "edge_brush", "RH_value", "RH_threshold", "atmosphere", "pm10", "pm25_value", "voc_value", "ch2o_value", "water_flow", "dc_fan_speed", "cmp_act_frep"]
    text_codes = ["air_quality_index", "direction_a", "direction_b", "gateway", "status", "fault", "multifunctionalarm", "air_quality", "watersensor_state", "MachineStatus", "MachineWarning", "MachineError"]
    setpoint_codes = ["set_temp", "temp_set", "cook_temperature", "wth_stemp", "ach_stemp", "aircond_temp_diff", "wth_temp_diff", "acc_stemp"]
    current_codes = ["cur_current", "cmp_cur", "leakage_current", "Current"]
    voltage_codes = ["voltage_a", "cur_voltage"]
    watt_codes = ["cur_power", "total_power", "ActivePower", "ActivePowerA", "ActivePowerB", "ActivePowerC", "phase_a"]

    if code in switch_codes:
        if code in ["doorcontact", "doorcontact_state", "door_control_1", "door_state_1", "smartlock"]:
            Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=244, Subtype=73, Switchtype=11, Used=1).Create()
        elif code == "position":
            Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=244, Subtype=73, Switchtype=21, Used=1).Create()
        elif code == "switch_pir":
            Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=244, Subtype=73, Switchtype=8, Used=1).Create()
        elif code in ["laser_bright", "fan_speed"]:
            Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=244, Subtype=73, Switchtype=7, Used=1).Create()
        else:
            Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=244, Subtype=73, Switchtype=0, Used=1).Create()

    elif code in selector_codes:
        mode = ["off"] + values.get("range", [])
        options = {
            "LevelOffHidden": "true",
            "LevelActions": "",
            "LevelNames": "|".join(mode),
            "SelectorStyle": "0" if len(mode) < 5 else "1"
        }
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=244, Subtype=62, Switchtype=18, Options=options, Image=9, Used=1).Create()

    elif code == "ActivePowerA" and "ActivePowerB" in code_list and "ActivePowerC" in code_list:
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=89, Subtype=1, Used=1).Create()

    elif code in ["power_a", "power_b", "add_ele"]:
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=243, Subtype=29, Used=1).Create()

    elif code in current_codes:
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=243, Subtype=31, Options={"Custom": "1;mA"}, Used=1).Create()

    elif code in voltage_codes:
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=243, Subtype=8, Used=1).Create()

    elif code in watt_codes:
        if code == "phase_a" and item.get("type") == "Raw":
            Domoticz.Unit(Name="{} (A)".format(name), DeviceID=dev_id, Unit=100 + unit, Type=243, Subtype=23, Used=1).Create()
            Domoticz.Unit(Name="{} (W)".format(name), DeviceID=dev_id, Unit=101 + unit, Type=248, Subtype=1, Used=1).Create()
            Domoticz.Unit(Name="{} (V)".format(name), DeviceID=dev_id, Unit=102 + unit, Type=243, Subtype=8, Used=1).Create()
            Domoticz.Unit(Name="{} (kWh)".format(name), DeviceID=dev_id, Unit=103 + unit, Type=243, Subtype=29, Used=1).Create()
        else:
            # Use Type=243, Subtype=29 (General/kWh) – same as original/KR
            Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=243, Subtype=29, Used=1).Create()

    elif code in temperature_codes:
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=80, Subtype=5, Used=1).Create()

    elif code in humidity_codes:
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=81, Subtype=1, Used=1).Create()

    elif code in percentage_codes:
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=243, Subtype=6, Used=1).Create()

    elif code == "bright_value":
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=246, Subtype=1, Switchtype=11, Used=1).Create()

    elif code in custom_codes:
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=243, Subtype=31, Options={"Custom": values.get("unit", "")}, Used=1).Create()

    elif code in text_codes:
        Domoticz.Unit(Name="{} ({})".format(name, code), DeviceID=dev_id, Unit=unit, Type=243, Subtype=19, Image=13, Used=1).Create()

    elif code in setpoint_codes:
        Domoticz.Unit(
            Name="{} ({})".format(name, code),
            DeviceID=dev_id,
            Unit=unit,
            Type=242,
            Subtype=1,
            Options={
                "ValueStep": values.get("step"),
                "ValueMin": values.get("min"),
                "ValueMax": values.get("max"),
                "ValueUnit": values.get("unit")
            },
            Used=1
        ).Create()

    else:
        Domoticz.Debug("No mapping for {} / {}".format(name, code))


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def DeviceType(category):
    if category in {"dj", "dd", "dc", "fwl", "xdd", "fwd", "jsq", "tyndj"}:
        return "light"
    return "nolight"


def UpdateDevice(ID, Unit, sValue, nValue, TimedOut, AlwaysUpdate=0):
    try:
        if ID not in Devices or Unit not in Devices[ID].Units:
            Domoticz.Debug("UpdateDevice: Device {} Unit {} does not exist, skipping".format(ID, Unit))
            return

        current = Devices[ID].Units[Unit]
        # ---- DEBUG: log every call ----
        Domoticz.Debug(
            "UpdateDevice called: ID={} Unit={} sValue={} nValue={} TimedOut={} AlwaysUpdate={}".format(
                ID, Unit, sValue, nValue, TimedOut, AlwaysUpdate
            )
        )
        if (
            str(current.sValue) != str(sValue) or
            str(current.nValue) != str(nValue) or
            str(Devices[ID].TimedOut) != str(TimedOut) or
            AlwaysUpdate == 1
        ):
            if sValue is None:
                sValue = current.sValue

            # ---- FIX: convert float to int for LastLevel ----
            if isinstance(sValue, (int, float)):
                current.LastLevel = int(sValue)
            elif isinstance(sValue, dict):
                current.Color = json.dumps(sValue)

            # ---- Also ensure nValue is integer ----
            try:
                nValue = int(nValue)
            except (ValueError, TypeError):
                Domoticz.Error("UpdateDevice: nValue must be integer, got {}".format(nValue))
                nValue = 0

            current.sValue = str(sValue)
            current.nValue = nValue
            Devices[ID].TimedOut = TimedOut
            current.Update(Log=True)

            Domoticz.Debug(
                "Update device value: ID={} Unit={} sValue={} nValue={} TimedOut={}".format(
                    ID, Unit, sValue, nValue, TimedOut
                )
            )
    except Exception as e:
        Domoticz.Error("UpdateDevice failed: {} line {}".format(str(e), lineno()))
def createDevice(ID, Unit):
    if ID in Devices and Unit in Devices[ID].Units:
        return False
    return True


def decode_phase_a(ID, unit, currentstatus):
    try:
        decoded_data = base64.b64decode(currentstatus)
        currentvoltage = int.from_bytes(decoded_data[:2], byteorder="big") * 0.1
        currentcurrent = int.from_bytes(decoded_data[2:5], byteorder="big") * 0.001
        currentpower = int.from_bytes(decoded_data[5:8], byteorder="big")

        UpdateDevice(ID, 100 + unit, str(currentcurrent), 0, 0)
        UpdateDevice(ID, 101 + unit, str(currentpower), 0, 0)
        UpdateDevice(ID, 102 + unit, str(currentvoltage), 0, 0)

        lastupdate = int(time.time()) - int(time.mktime(time.strptime(
            Devices[ID].Units[103 + unit].LastUpdate, "%Y-%m-%d %H:%M:%S"
        )))
        lastvalue = Devices[ID].Units[103 + unit].sValue if Devices[ID].Units[103 + unit].sValue else "0;0"
        energy = float(lastvalue.split(";")[1]) + (currentpower * (lastupdate / 3600))
        UpdateDevice(ID, 103 + unit, str(currentpower) + ";" + str(energy), 0, 0, 1)
    except Exception as e:
        Domoticz.Error("decode_phase_a failed: {} line {}".format(str(e), lineno()))


def battery_device(ID, code, value):
    try:
        currentbattery = None

        if code == "battery_state":
            if value == "high":
                currentbattery = 100
            elif value == "middle":
                currentbattery = 50
            elif value == "low":
                currentbattery = 5
        elif code == "BatteryStatus":
            if int(value) == 1:
                currentbattery = 100
            elif int(value) == 2:
                currentbattery = 50
            elif int(value) == 3:
                currentbattery = 5
        elif code == "battery":
            currentbattery = value * 10
        elif code in ["va_battery", "battery_percentage", "residual_electricity"]:
            currentbattery = value

        if currentbattery is None:
            return

        for unit in Devices[ID].Units:
            if str(currentbattery) != str(Devices[ID].Units[unit].BatteryLevel):
                Devices[ID].Units[unit].BatteryLevel = currentbattery
                Devices[ID].Units[unit].Update()

    except Exception as e:
        Domoticz.Error("battery_device failed: {} line {}".format(str(e), lineno()))


def set_scale(raw, item):
    try:
        if isinstance(raw, (bool, dict)):
            return raw

        values = item.get("values", {}) if isinstance(item, dict) else {}
        scale = int(values.get("scale", 0) or 0)
        min_v = values.get("min", None)
        max_v = values.get("max", None)

        x = float(raw)
        result = int(round(x * (10 ** scale)))

        if max_v is not None and result > int(max_v):
            result = int(max_v)
        if min_v is not None and result < int(min_v):
            result = int(min_v)

        return result
    except Exception:
        return raw


def get_scale(raw, item):
    if isinstance(raw, bool):
        return raw

    try:
        values = item.get("values", {}) if isinstance(item, dict) else {}
        scale = int(values.get("scale", 0) or 0)
        unit = values.get("unit", None)

        if isinstance(raw, str):
            try:
                x = float(raw)
            except Exception:
                return raw
        else:
            x = raw

        if isinstance(x, (int, float)):
            x = x / (10 ** scale)
            #if unit == "W":
                # No extra conversion for "W"
                #x = x / 1000.0
            return x

        return raw
    except Exception:
        return raw


def getConfigItem(Key=None, Values=None, Default=None):
    try:
        Config = Domoticz.Configuration()
        if Key is not None:
            if Values is not None:
                return Config.get(Key, {}).get(Values, Default)
            return Config.get(Key, Default)
        return Config
    except Exception:
        return Default


def setConfigItem(Key=None, Value=None):
    try:
        Config = Domoticz.Configuration()
        if Key is not None:
            Config[Key] = Value
        else:
            Config = Value
        return Domoticz.Configuration(Config)
    except Exception as e:
        Domoticz.Error("Domoticz.Configuration failed: {} line {}".format(str(e), lineno()))
        return {}


def DumpConfigToLog():
    try:
        for x in Parameters:
            if Parameters[x] != "":
                Domoticz.Debug("'{}':'{}'".format(x, Parameters[x]))
        Domoticz.Debug("Device count: {}".format(len(Devices)))
        for DeviceName in Devices:
            device = Devices[DeviceName]
            Domoticz.Debug("Device ID: '{}'".format(device.DeviceID))
            Domoticz.Debug("--->Unit Count: '{}'".format(len(device.Units)))
            for UnitNo in device.Units:
                unit = device.Units[UnitNo]
                Domoticz.Debug("--->Unit: {}".format(UnitNo))
                Domoticz.Debug("--->Unit Name: '{}'".format(unit.Name))
                Domoticz.Debug("--->Unit nValue: {}".format(unit.nValue))
                Domoticz.Debug("--->Unit sValue: '{}'".format(unit.sValue))
                Domoticz.Debug("--->Unit LastLevel: {}".format(unit.LastLevel))
    except Exception as e:
        Domoticz.Error("DumpConfigToLog failed: {} line {}".format(str(e), lineno()))


def lineno():
    return format(sys.exc_info()[-1].tb_lineno)


def version(v):
    return tuple(map(int, v.split(".")))
