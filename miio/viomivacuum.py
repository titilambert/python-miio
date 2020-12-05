"""Viomi Vacuum.

# https://github.com/rytilahti/python-miio/issues/550#issuecomment-552780952
# https://github.com/homebridge-xiaomi-roborock-vacuum/homebridge-xiaomi-roborock-vacuum/blob/ee10cbb3e98dba75d9c97791a6e1fcafc1281591/miio/lib/devices/vacuum.js
# https://github.com/homebridge-xiaomi-roborock-vacuum/homebridge-xiaomi-roborock-vacuum/blob/ee10cbb3e98dba75d9c97791a6e1fcafc1281591/miio/lib/devices/viomivacuum.js

Features:

Main:
- Area/Duration - Missing (get_clean_summary/get_clean_record
- Battery - battery_life
- Dock - set_charge
- Start/Pause - set_mode_withroom
- Modes (Vacuum/Vacuum&Mop/Mop) - set_mop/id_mop
- Fan Speed (Silent/Standard/Medium/Turbo) - set_suction/suction_grade
- Water Level (Low/Medium/High) - set_suction/water_grade

Settings:
- Cleaning history - MISSING (cleanRecord)
- Scheduled cleanup - get_ordertime
- Vacuum along the edges - get_mode/set_mode
- Secondary cleanup - set_repeat/repeat_state
- Mop or vacuum & mod mode - set_moproute/mop_route
- DND(DoNotDisturb) - set_notdisturb/get_notdisturb
- Voice On/Off - set_voice/voice_state
- Remember Map - voice_state
- Virtual wall/restricted area - MISSING
- Map list - get_maps/rename_map/delete_map/set_map
- Area editor - MISSING
- Reset map - MISSING
- Device leveling - MISSING
- Looking for the vacuum-mop - MISSING (find_me)
- Consumables statistics - get_properties
- Remote Control - MISSING

Misc:
- Get Properties
- Language - set_language
- Led - set_light
- Rooms - get_ordertime (hack)
- Clean History Path - MISSING (historyPath)
- Map plan - MISSING (map_plan)
"""
import itertools
import json
import logging
import os
import pathlib
import time
from collections import defaultdict
from datetime import timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import click
from appdirs import user_cache_dir

from .click_common import (
    DeviceGroup,
    EnumType,
    GlobalContextObject,
    command,
    format_output,
)
from .device import Device
from .exceptions import DeviceException
from .utils import pretty_seconds
from .vacuumcontainers import ConsumableStatus, DNDStatus

_LOGGER = logging.getLogger(__name__)


ERROR_CODES = {
    0: "Sleeping and not charging",
    500: "Radar timed out",
    501: "Wheels stuck",
    502: "Low battery",
    503: "Dust bin missing",
    508: "Uneven ground",
    509: "Cliff sensor error",
    510: "Collision sensor error",
    511: "Could not return to dock",
    512: "Could not return to dock",
    513: "Could not navigate",
    514: "Vacuum stuck",
    515: "Charging error",
    516: "Mop temperature error",
    521: "Water tank is not installed",
    522: "Mop is not installed",
    525: "Insufficient water in water tank",
    527: "Remove mop",
    528: "Dust bin missing",
    529: "Mop and water tank missing",
    530: "Mop and water tank missing",
    531: "Water tank is not installed",
    2101: "Unsufficient battery, continuing cleaning after recharge",
    2103: "Charging",
    2105: "Fully charged",
}


class ViomiVacuumException(DeviceException):
    """Exception raised by Viomi Vacuum."""


class ViomiConsumableStatus(ConsumableStatus):
    def __init__(self, data: List[int]) -> None:
        # [17, 17, 17, 17]
        self.data = [d * 60 * 60 for d in data]
        self.side_brush_total = timedelta(hours=180)
        self.main_brush_total = timedelta(hours=360)
        self.filter_total = timedelta(hours=180)
        self.mop_total = timedelta(hours=180)

    @property
    def main_brush(self) -> timedelta:
        """Main brush usage time."""
        return pretty_seconds(self.data[0])

    @property
    def main_brush_left(self) -> timedelta:
        """How long until the main brush should be changed."""
        return self.main_brush_total - self.main_brush

    @property
    def side_brush(self) -> timedelta:
        """Side brush usage time."""
        return pretty_seconds(self.data[1])

    @property
    def side_brush_left(self) -> timedelta:
        """How long until the side brush should be changed."""
        return self.side_brush_total - self.side_brush

    @property
    def filter(self) -> timedelta:
        """Filter usage time."""
        return pretty_seconds(self.data[2])

    @property
    def filter_left(self) -> timedelta:
        """How long until the filter should be changed."""
        return self.filter_total - self.filter

    @property
    def mop(self) -> timedelta:
        """Return ``sensor_dirty_time``"""
        return pretty_seconds(self.data[3])

    @property
    def mop_left(self) -> timedelta:
        """How long until the mop should be changed."""
        return self.sensor_dirty_total - self.sensor_dirty

    def __repr__(self) -> str:
        return (
            "<ConsumableStatus main: %s, side: %s, filter: %s, mop: %s>"
            % (  # noqa: E501
                self.main_brush,
                self.side_brush,
                self.filter,
                self.mop,
            )
        )


class ViomiVacuumSpeed(Enum):
    Silent = 0
    Standard = 1
    Medium = 2
    Turbo = 3


class ViomiVacuumState(Enum):
    Unknown = -1
    IdleNotDocked = 0
    Idle = 1
    Idle2 = 2
    Cleaning = 3
    Returning = 4
    Docked = 5
    VacuumingAndMopping = 6


class ViomiMode(Enum):
    Vacuum = 0  # No Mop, Vacuum only
    VacuumAndMop = 1
    Mop = 2
    CleanZone = 3
    CleanSpot = 4


class ViomiLanguage(Enum):
    CN = 1  # Chinese (default)
    EN = 2  # English


class ViomiLedState(Enum):
    Off = 0
    On = 1


class ViomiCarpetTurbo(Enum):
    Off = 0
    Medium = 1
    Turbo = 2


class ViomiMovementDirection(Enum):
    Forward = 1
    Left = 2  # Rotate
    Right = 3  # Rotate
    Backward = 4
    Stop = 5
    Unknown = 10


class ViomiBinType(Enum):
    Vacuum = 1
    Water = 2
    VacuumAndWater = 3
    NoBin = 0


class ViomiWaterGrade(Enum):
    Low = 11
    Medium = 12
    High = 13


class ViomiRoutePattern(Enum):
    """Mopping pattern."""

    S = 0
    Y = 1


class ViomiVoiceState(Enum):
    Off = 0
    Level_10 = 1
    Level_20 = 2
    Level_30 = 3
    Level_40 = 4
    Level_50 = 5
    Level_60 = 6
    Level_70 = 7
    Level_80 = 8
    Level_90 = 9
    Level_100 = 10


class ViomiEdgeState(Enum):
    Off = 0
    Unknown = 1
    On = 2


class ViomiVacuumStatus:
    def __init__(self, data):
        # ["run_state","mode","err_state","battary_life","box_type","mop_type","s_time","s_area",
        # "suction_grade","water_grade","remember_map","has_map","is_mop","has_newmap"]'
        # 1,               11,           1,            1,         1,       0          ]
        self.data = data

    @property
    def state(self):
        """State of the vacuum."""
        try:
            return ViomiVacuumState(self.data["run_state"])
        except ValueError:
            _LOGGER.warning("Unknown vacuum state: %s", self.data["run_state"])
            return ViomiVacuumState.Unknown

    @property
    def edge_state(self) -> ViomiEdgeState:
        """Vaccum along the edges

        The settings is valid once
        0: Off
        1: Unknown
        2: On
        """
        return ViomiEdgeState(self.data["mode"])

    @property
    def mop_installed(self) -> bool:
        """True if the mop is installed."""
        return bool(self.data["mop_type"])

    @property
    def error_code(self) -> int:
        """Error code from vacuum."""
        return self.data["err_state"]

    @property
    def error(self) -> Optional[str]:
        """String presentation for the error code."""
        if self.error_code is None:
            return None

        return ERROR_CODES.get(self.error_code, f"Unknown error {self.error_code}")

    @property
    def battery(self) -> int:
        """Battery in percentage."""
        return self.data["battary_life"]

    @property
    def bin_type(self) -> ViomiBinType:
        """Type of the inserted bin."""
        return ViomiBinType(self.data["box_type"])

    @property
    def clean_time(self) -> timedelta:
        """Cleaning time."""
        return pretty_seconds(self.data["s_time"])

    @property
    def clean_area(self) -> float:
        """Cleaned area in square meters."""
        return self.data["s_area"]

    @property
    def fanspeed(self) -> ViomiVacuumSpeed:
        """Current fan speed."""
        return ViomiVacuumSpeed(self.data["suction_grade"])

    @command()
    def fan_speed_presets(self) -> Dict[str, int]:
        """Return dictionary containing supported fanspeeds."""
        return {x.name: x.value for x in list(ViomiVacuumSpeed)}

    @property
    def water_grade(self) -> ViomiWaterGrade:
        """Water grade."""
        return ViomiWaterGrade(self.data["water_grade"])

    @property
    def remember_map(self) -> bool:
        """True to remember the map."""
        return bool(self.data["remember_map"])

    @property
    def has_map(self) -> bool:
        """True if device has map?"""
        return bool(self.data["has_map"])

    @property
    def has_new_map(self) -> bool:
        """True if the device has scanned a new map (like a new floor)."""
        return bool(self.data["has_newmap"])

    @property
    def mop_mode(self) -> ViomiMode:
        """Whether mopping is enabled and if so which mode"""
        return ViomiMode(self.data["is_mop"])

    @property
    def current_map_id(self) -> float:
        """Current map id."""
        return self.data["cur_mapid"]

    @property
    def hw_info(self) -> str:
        """Hardware info."""
        return self.data["hw_info"]

    @property
    def charging(self) -> bool:
        """True if battery is charging

        Note: When the battery is at 100%, device reports that it is not charging.
        """
        return not bool(self.data["is_charge"])

    @property
    def is_on(self) -> bool:
        """True if device is working."""
        return not bool(self.data["is_work"])

    @property
    def light_state(self) -> bool:
        """Led state.

        This seems doing nothing on STYJ02YM
        """
        return bool(self.data["light_state"])

    @property
    def map_number(self) -> int:
        """Number of saved maps."""
        return self.data["map_num"]

    @property
    def mop_route(self) -> ViomiRoutePattern:
        """Pattern mode."""
        return ViomiRoutePattern(self.data["mop_route"])

    @property
    def order_time(self) -> int:
        """FIXME: ??? int or bool."""
        return self.data["order_time"]

    @property
    def repeat_state(self) -> bool:
        """Secondary clean up state."""
        return self.data["repeat_state"]

    @property
    def start_time(self) -> int:
        """FIXME: ??? int or bool."""
        return self.data["start_time"]

    @property
    def voice_state(self) -> ViomiVoiceState:
        """Voice volume level (from 0 to 100%, 0 means Off)."""
        return ViomiVoiceState(self.data["v_state"])

    @property
    def water_percent(self) -> int:
        """FIXME: ??? int or bool."""
        return self.data["water_percent"]

    @property
    def zone_data(self) -> int:
        """FIXME: ??? int or bool."""
        return self.data["zone_data"]


class ViomiVacuum(Device):
    """Interface for Viomi vacuums (viomi.vacuum.v7)."""

    _cache = {"edge_state": None, "rooms": {}}
    timeout = 0.5
    retry_count = 20

    def __init__(
        self, ip: str, token: str = None, start_id: int = 0, debug: int = 0
    ) -> None:
        super().__init__(ip, token, start_id, debug)
        self.manual_seqnum = -1
        # self.model = None
        # self._fanspeeds = FanspeedV1

    @command(
        default_output=format_output(
            "\n",
            "General\n"
            "=======\n\n"
            "Hardware version: {result.hw_info}\n"
            "State: {result.state}\n"
            "Working: {result.is_on}\n"
            "Battery status: {result.error}\n"
            "Battery: {result.battery}\n"
            "Charging: {result.charging}\n"
            "Box type: {result.bin_type}\n"
            "Fan speed: {result.fanspeed}\n"
            "Water grade: {result.water_grade}\n"
            "Mop mode: {result.mop_mode}\n"
            "Mop installed: {result.mop_installed}\n"
            "Vacuum along the edges: {result.edge_state}\n"
            "Mop route pattern: {result.mop_route}\n"
            "Secondary Cleanup: {result.repeat_state}\n"
            "Voice state: {result.voice_state}\n"
            "Clean time: {result.clean_time}\n"
            "Clean area: {result.clean_area} m²\n"
            "\n"
            "Map\n"
            "===\n\n"
            "Current map ID: {result.current_map_id}\n"
            "Remember map: {result.remember_map}\n"
            "Has map: {result.has_map}\n"
            "Has new map: {result.has_new_map}\n"
            "Number of maps: {result.map_number}\n"
            "\n"
            "Unknown properties\n"
            "=================\n\n"
            "Light state: {result.light_state}\n"
            "Order time: {result.order_time}\n"
            "Start time: {result.start_time}\n"
            "water_percent: {result.water_percent}\n"
            "zone_data: {result.zone_data}\n",
        )
    )
    def status(self) -> ViomiVacuumStatus:
        """Retrieve properties."""
        properties = [
            "battary_life",
            "box_type",
            "cur_mapid",
            "err_state",
            "has_map",
            "has_newmap",
            "hw_info",
            "is_charge",
            "is_mop",
            "is_work",
            "light_state",
            "map_num",
            "mode",
            "mop_route",
            "mop_type",
            "order_time",
            "remember_map",
            "repeat_state",
            "run_state",
            "s_area",
            "s_time",
            "start_time",
            "suction_grade",
            "v_state",
            "water_grade",
            "water_percent",
            "zone_data",
            # The following list of properties existing but
            # there are not used in the code
            # "sw_info",
            # "main_brush_hours",
            # "main_brush_life",
            # "side_brush_hours",
            # "side_brush_life",
            # "mop_hours",
            # "mop_life",
            # "hypa_hours",
            # "hypa_life",
        ]

        values = self.get_properties(properties)

        return ViomiVacuumStatus(defaultdict(lambda: None, zip(properties, values)))

    @command()
    def home(self):
        """Return to home."""
        self.send("set_charge", [1])

    @command()
    def start(self):
        """Start cleaning."""
        # params: [edge, 1, roomIds.length, *list_of_room_ids]
        # - edge: see ViomiEdgeState
        # - 1: start cleaning (2 pause, 0 stop)
        # - roomIds.length
        # - *room_id_list
        # 3rd param of set_mode_withroom is room_array_len and next are
        # room ids ([0, 1, 3, 11, 12, 13] = start cleaning rooms 11-13).
        # room ids are encoded in map and it's part of cloud api so best way
        # to get it is log between device <> mi home app
        # (before map format is supported).
        self._cache["edge_state"] = self.get_properties(["mode"])
        self.send("set_mode_withroom", self._cache["edge_state"] + [1, 0])

    @command(
        click.option(
            "--rooms",
            "-r",
            multiple=True,
            help="Rooms name or room id. Can be used multiple times",
        )
    )
    def start_with_room(self, rooms):
        """Start cleaning specific rooms."""
        if not self._cache["rooms"]:
            self.get_rooms()
        reverse_rooms = {v: k for k, v in self._cache["rooms"].items()}
        room_ids = []
        for room in rooms:
            if room in self._cache["rooms"]:
                room_ids.append(room)
            elif room in reverse_rooms:
                room_ids.append(reverse_rooms[room])
            else:
                return "Rooms {} is unknown, it should be in '{}' " "or in '{}'".format(
                    room,
                    ", ".join(self._cache["rooms"].keys()),
                    ", ".join(self._cache["rooms"].values()),
                )
        self._cache["edge_state"] = self.get_properties(["mode"])
        self.send(
            "set_mode_withroom",
            self._cache["edge_state"] + [1, 0, len(room_ids)] + room_ids,
        )

    @command()
    def pause(self):
        """Pause cleaning."""
        # params: [edge_state, 0]
        # - edge: see ViomiEdgeState
        # - 2: pause cleaning
        if not self._cache["edge_state"]:
            self._cache["edge_state"] = self.get_properties(["mode"])
        self.send("set_mode", self._cache["edge_state"] + [2])

    @command()
    def stop(self):
        """Validate that Stop cleaning."""
        # params: [edge_state, 0]
        # - edge: see ViomiEdgeState
        # - 0: stop cleaning
        if not self._cache["edge_state"]:
            self._cache["edge_state"] = self.get_properties(["mode"])
        self.send("set_mode", self._cache["edge_state"] + [0])

    @command(click.argument("mode", type=EnumType(ViomiMode)))
    def clean_mode(self, mode: ViomiMode):
        """Set the cleaning mode.

        [vacuum, vacuumAndMop, mop, cleanzone, cleanspot]
        """
        self.send("set_mop", [mode.value])

    @command(click.argument("speed", type=EnumType(ViomiVacuumSpeed)))
    def set_fan_speed(self, speed: ViomiVacuumSpeed):
        """Set fanspeed [silent, standard, medium, turbo]."""
        self.send("set_suction", [speed.value])

    @command(click.argument("watergrade", type=EnumType(ViomiWaterGrade)))
    def set_water_grade(self, watergrade: ViomiWaterGrade):
        """Set water grade.

        [low, medium, high]
        """
        self.send("set_suction", [watergrade.value])

    # MISSING cleaning history

    @command()
    def get_scheduled_cleanup(self):
        """Not implemented yet."""
        # Needs to reads and understand the return of:
        # self.send("get_ordertime", [])
        # [id, enabled, repeatdays, hour, minute, ?, ? , ?, ?, ?, ?, nb_of_rooms, room_id, room_name, room_id, room_name, ...]
        raise NotImplementedError()

    @command()
    def set_scheduled_cleanup(self):
        """Not implemented yet."""
        # Needs to reads and understand:
        # self.send("set_ordertime", [????])
        raise NotImplementedError()

    @command()
    def del_scheduled_cleanup(self):
        """Not implemented yet."""
        # Needs to reads and understand:
        # self.send("det_ordertime", [shedule_id])
        raise NotImplementedError()

    @command(click.argument("state", type=EnumType(ViomiEdgeState)))
    def set_edge(self, state: ViomiEdgeState):
        """Set or Unset edge mode.

        Vacuum along the edges
        The settings is valid once
        """
        return self.send("set_mode", [state.value])

    @command(click.argument("state", type=bool))
    def set_repeat(self, state: bool):
        """Set or Unset repeat mode."""
        return self.send("set_repeat", [int(state)])

    @command(click.argument("mop_mode", type=EnumType(ViomiRoutePattern)))
    def set_route_pattern(self, mop_mode: ViomiRoutePattern):
        """Set the mop route pattern."""
        self.send("set_moproute", [mop_mode.value])

    @command()
    def dnd_status(self):
        """Returns do-not-disturb status."""
        status = self.send("get_notdisturb")
        return DNDStatus(
            dict(
                enabled=status[0],
                start_hour=status[1],
                start_minute=status[2],
                end_hour=status[3],
                end_minute=status[4],
            )
        )

    @command(
        click.option("--disable", is_flag=True),
        click.argument("start_hr", type=int),
        click.argument("start_min", type=int),
        click.argument("end_hr", type=int),
        click.argument("end_min", type=int),
    )
    def set_dnd(
        self, disable: bool, start_hr: int, start_min: int, end_hr: int, end_min: int
    ):
        """Set do-not-disturb.

        :param int start_hr: Start hour
        :param int start_min: Start minute
        :param int end_hr: End hour
        :param int end_min: End minute"""
        return self.send(
            "set_notdisturb",
            [0 if disable else 1, start_hr, start_min, end_hr, end_min],
        )

    @command(click.argument("state", type=EnumType(ViomiVoiceState)))
    def set_voice(self, state: ViomiVoiceState):
        """Switch the voice on or off."""
        return self.send("set_voice", [1, state.value])

    @command(click.argument("state", type=bool))
    def set_remember(self, state: bool):
        """Set remenber map state."""
        return self.send("set_remember", [int(state)])

    # MISSING: Virtual wall/restricted area

    @command()
    def get_maps(self) -> List[Dict[str, Any]]:
        """Return map list.

        [{'name': 'MapName1', 'id': 1598622255, 'cur': False},
         {'name': 'MapName2', 'id': 1599508355, 'cur': True},
          ...]
        """
        return self.send("get_map")

    @command(click.argument("map_id", type=int))
    def set_map(self, map_id: int):
        """Change current map."""
        maps = self.get_maps()
        if map_id not in [m["id"] for m in maps]:
            raise ViomiVacuumException("Map id {} doesn't exists".format(map_id))
        return self.send("set_map", [map_id])

    @command(click.argument("map_id", type=int))
    def delete_map(self, map_id: int):
        """Delete map."""
        maps = self.get_maps()
        if map_id not in [m["id"] for m in maps]:
            raise ViomiVacuumException("Map id {} doesn't exists".format(map_id))
        return self.send("del_map", [map_id])

    @command(
        click.argument("map_id", type=int),
        click.argument("map_name", type=str),
    )
    def rename_map(self, map_id: int, map_name: str):
        """Rename map."""
        maps = self.get_maps()
        if map_id not in [m["id"] for m in maps]:
            raise ViomiVacuumException("Map id {} doesn't exists".format(map_id))
        return self.send("rename_map", {"mapID": map_id, "name": map_name})

    @command(
        click.option("--map-id", type=int, default=None),
        click.option("--map-name", type=str, default=None),
        click.option("--refresh", type=bool, default=False),
    )
    def get_rooms(
        self, map_id: int = None, map_name: str = None, refresh: bool = False
    ):
        """Return room ids and names."""
        if self._cache["rooms"] and not refresh:
            return self._cache["rooms"]
        if map_name:
            map_id = None
            maps = self.get_maps()
            for map_ in maps:
                if map_["name"] == map_name:
                    map_id = map_["id"]
            if map_id is None:
                raise ViomiVacuumException(
                    "Error: Bad map name, should be in {}".format(
                        ", ".join([m["name"] for m in maps])
                    )
                )
        elif map_id:
            maps = self.get_maps()
            if map_id not in [m["id"] for m in maps]:
                raise ViomiVacuumException(
                    "Error: Bad map id, should be in {}".format(
                        ", ".join([str(m["id"]) for m in maps])
                    )
                )
        # https://github.com/homebridge-xiaomi-roborock-vacuum/homebridge-xiaomi-roborock-vacuum/blob/d73925c0106984a995d290e91a5ba4fcfe0b6444/index.js#L969
        # https://github.com/homebridge-xiaomi-roborock-vacuum/homebridge-xiaomi-roborock-vacuum#semi-automatic
        schedules = self.send("get_ordertime", [])
        # ['1', '1', '32', '0', '0', '0', '1', '1', '11', '0', '1594139992', '2', '11', 'ami', '13', 'cuisine']
        # [id, enabled, repeatdays, hour, minute, ?, ? , ?, ?, ?, ?, nb_of_rooms, room_id, room_name, room_id, room_name, ...]
        # Find ALL specific scheduled cleanupS containing the room ids
        rooms = {}
        scheduled_found = False
        for raw_schedule in schedules:
            schedule = raw_schedule.split("_")
            # Scheduled cleanup needs to be scheduled for 00:00 and inactive
            if schedule[1] == "0" and schedule[3] == "0" and schedule[4] == "0":
                scheduled_found = True
                raw_rooms = schedule[12:]
                rooms_iter = iter(raw_rooms)
                rooms.update(
                    dict(itertools.zip_longest(rooms_iter, rooms_iter, fillvalue=None))
                )

        if not scheduled_found:
            msg = (
                "Fake schedule not found. "
                "Please create a scheduled cleanup with the "
                "following properties:\n"
                "* Hour: 00\n"
                "* Minute: 00\n"
                "* Select all (minus one) the rooms one by one\n"
                "* Set as inactive scheduled cleanup\n"
                "Then create a scheduled cleanup with the room missed at "
                "previous step with the following properties:\n"
                "* Hour: 00\n"
                "* Minute: 00\n"
                "* Select only the missed room\n"
                "* Set as inactive scheduled cleanup\n"
            )
            raise ViomiVacuumException(msg)

        self._cache["rooms"] = rooms
        return rooms

    # MISSING Area editor

    # MISSING Reset map

    # MISSING Device leveling

    # MISSING Looking for the vacuum-mop

    @command()
    def consumable_status(self) -> ViomiConsumableStatus:
        """Return information about consumables."""
        return ViomiConsumableStatus(self.send("get_consumables"))

    @command(
        click.argument("direction", type=EnumType(ViomiMovementDirection)),
        click.option(
            "--duration",
            type=float,
            default=0.5,
            help="number of seconds to perform this movement",
        ),
    )
    def move(self, direction: ViomiMovementDirection, duration=0.5):
        """Manual movement."""
        start = time.time()
        while time.time() - start < duration:
            self.send("set_direction", [direction.value])
            time.sleep(0.1)
        self.send("set_direction", [ViomiMovementDirection.Stop.value])

    @command(click.argument("language", type=EnumType(ViomiLanguage)))
    def set_language(self, language: ViomiLanguage):
        """Set the device's audio language.

        This seems doing nothing on STYJ02YM
        """
        return self.send("set_language", [language.value])

    @command(click.argument("state", type=EnumType(ViomiLedState)))
    def led(self, state: ViomiLedState):
        """Switch the button leds on or off.

        This seems doing nothing on STYJ02YM
        """
        return self.send("set_light", [state.value])

    @command(click.argument("mode", type=EnumType(ViomiCarpetTurbo)))
    def carpet_mode(self, mode: ViomiCarpetTurbo):
        """Set the carpet mode.

        This seems doing nothing on STYJ02YM
        """
        return self.send("set_carpetturbo", [mode.value])

    @classmethod
    def get_device_group(cls):
        @click.pass_context
        def callback(ctx, *args, id_file, **kwargs):
            gco = ctx.find_object(GlobalContextObject)
            if gco:
                kwargs["debug"] = gco.debug

            start_id = manual_seq = 0
            try:
                with open(id_file, "r") as f:
                    x = json.load(f)
                    start_id = x.get("seq", 0)
                    manual_seq = x.get("manual_seq", 0)
                    _LOGGER.debug("Read stored sequence ids: %s", x)
            except (FileNotFoundError, TypeError, ValueError):
                pass

            ctx.obj = cls(*args, start_id=start_id, **kwargs)
            ctx.obj.manual_seqnum = manual_seq

        dg = DeviceGroup(
            cls,
            params=DeviceGroup.DEFAULT_PARAMS
            + [
                click.Option(
                    ["--id-file"],
                    type=click.Path(dir_okay=False, writable=True),
                    default=os.path.join(
                        user_cache_dir("python-miio"), "python-mirobo.seq"
                    ),
                )
            ],
            callback=callback,
        )

        @dg.resultcallback()
        @dg.device_pass
        def cleanup(vac: ViomiVacuum, *args, **kwargs):
            if vac.ip is None:  # dummy Device for discovery, skip teardown
                return
            id_file = kwargs["id_file"]
            seqs = {"seq": vac._protocol.raw_id, "manual_seq": vac.manual_seqnum}
            _LOGGER.debug("Writing %s to %s", seqs, id_file)
            path_obj = pathlib.Path(id_file)
            cache_dir = path_obj.parents[0]
            cache_dir.mkdir(parents=True, exist_ok=True)
            with open(id_file, "w") as f:
                json.dump(seqs, f)

        return dg
