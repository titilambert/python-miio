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
import logging
import time
from collections import defaultdict
from datetime import timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import click

from PIL import Image, ImageDraw

from .click_common import EnumType, command, format_output
from .device import Device
from .exceptions import ViomiVacuumException
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


class ViomiPositionPoint:
    def __init__(self, pos_x, pos_y, phi, update, plan_multiplicator=1):
        self._pos_x = pos_x
        self._pos_y = pos_y
        self.phi = phi
        self.update = update
        self._plan_multiplicator = plan_multiplicator

    @property
    def pos_x(self):
        """X coordonate with multiplicator."""
        return self._pos_x * self._plan_multiplicator

    @property
    def pos_y(self):
        """Y coordonate with multiplicator."""
        return self._pos_y * self._plan_multiplicator

    def image_pos_x(self, offset, img_center):
        """X coordonate on an image."""
        return self.pos_x - offset + img_center

    def image_pos_y(self, offset, img_center):
        """Y coordonate on an image."""
        return self.pos_y - offset + img_center

    def __repr__(self) -> str:
        return "<ViomiPositionPoint x: {}, y: {}, phi: {}, update {}".format(
            self.pos_x, self.pos_y, self.phi, self.update
        )

    def __eq__(self, value) -> bool:
        return (
            self.pos_x == value.pos_x
            and self.pos_y == value.pos_y
            and self.phi == value.phi
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
    On = 5


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
    def is_on(self) -> bool:
        """True if cleaning."""
        cleaning_states = [
            ViomiVacuumState.Cleaning,
            ViomiVacuumState.VacuumingAndMopping,
        ]
        return self.state in cleaning_states

    @property
    def edge_state(self) -> ViomiEdgeState:
        """Vaccum along the edges

        The settings is valid once
        0: disabled
        2: enabled
        5: unknown
        """
        return ViomiEdgeState(self.data["mode"])

    @property
    def mop_installed(self) -> bool:
        """Mop installed status

        True if the mop is installed
        False if the mop is NOT installed
        """
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
        """Battery is charging or not.

        is_charge is 1 when the battery is not charging
        is_charge is 0 when the device is charging
        Note: When the battery is at 100% is_charge is 1

        Return:
        - True if the battery is charging
        - False if the battery is NOT charging
        """
        return self.data["is_charge"]

    @property
    def working(self) -> bool:
        """Device is working or not.

        is_work is 1 when the device is not working
        is_work is 0 when the device is working

        Return:
        - True if the device is working
        - False if the device is NOT working
        """
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
        """Voice state."""
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

    @command(
        default_output=format_output(
            "\n",
            "General\n"
            "=======\n\n"
            "Hardware version: {result.hw_info}\n"
            "State: {result.state}\n"
            "Working: {result.working}\n"
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
        """Set fanspeed.

        [silent, standard, medium, turbo]
        """
        self.send("set_suction", [speed.value])

    @command(click.argument("watergrade", type=EnumType(ViomiWaterGrade)))
    def set_water_grade(self, watergrade: ViomiWaterGrade):
        """Set water grade.

        [low, medium, high]
        """
        self.send("set_suction", [watergrade.value])

    def get_positions(self, plan_multiplicator=1) -> [ViomiPositionPoint]:
        """Return the current positions

        returns: [x, y, phi, update, x, y, phi, update, x, y, phi, update, ...]
        """
        results = self.send("get_curpos", [])
        positions = []
        # Group result 4 by 4
        for result in [i for i in zip(*(results[i::4] for i in range(4)))]:
            positions.append(
                ViomiPositionPoint(*result, plan_multiplicator=plan_multiplicator)
            )
        return positions

    @command()
    def get_current_position(self) -> ViomiPositionPoint:
        """Return the current position"""
        positions = self.get_positions()
        if positions:
            return positions[-1]
        return None

    @command(click.argument("output", type=click.Path(dir_okay=False, writable=True)))
    def follow_position(self, output):
        """Draw a map of the track made by the Vacuum."""
        no_pos_found = 0
        wait_time = 1

        image_size = 10000
        image_margin = 20
        plan_multiplicator = image_size / 10
        image = Image.new("RGB", (image_size, image_size), "white")
        draw = ImageDraw.Draw(image)

        img_center = image_size / 2

        position_history = []
        # ScaleUp the position to have more precision
        positions = self.get_positions(plan_multiplicator=plan_multiplicator)
        if not positions:
            return

        while no_pos_found < 5:
            for position in positions:
                if position in position_history:
                    # Avoid position duplication
                    continue
                elif not position_history:
                    # Handle first point
                    # Set the middle of the image as starting point
                    offset_x = position.pos_x
                    offset_y = position.pos_y
                    position_history.append(position)
                elif position and position != position_history[-1]:
                    # Handle other points
                    start_point = (
                        position_history[-1].image_pos_x(offset_x, img_center),
                        position_history[-1].image_pos_y(offset_y, img_center),
                    )
                    end_point = (
                        position.image_pos_x(offset_x, img_center),
                        position.image_pos_y(offset_y, img_center),
                    )
                    draw.line([start_point, end_point], "black", 1, joint=None)
                    position_history.append(position)

                    # Get box corner for cropping
                    min_x = (
                        min(
                            [
                                p.image_pos_x(offset_x, img_center)
                                for p in position_history
                            ]
                        )
                        - image_margin
                    )
                    max_x = (
                        max(
                            [
                                p.image_pos_x(offset_x, img_center)
                                for p in position_history
                            ]
                        )
                        + image_margin
                    )
                    min_y = (
                        min(
                            [
                                p.image_pos_y(offset_y, img_center)
                                for p in position_history
                            ]
                        )
                        - image_margin
                    )
                    max_y = (
                        max(
                            [
                                p.image_pos_y(offset_y, img_center)
                                for p in position_history
                            ]
                        )
                        + image_margin
                    )
                    # Crop image
                    image_output = image.crop((min_x, min_y, max_x, max_y))
                    # Flip the image to get it correctly
                    image_output = image_output.transpose(Image.FLIP_TOP_BOTTOM)
                    image_output.save(output, "PNG")

            try:
                # Get new positions
                positions = self.get_positions(plan_multiplicator=plan_multiplicator)
            except Exception as exp:
                # Sometimes we get a token error
                # We just have to wait before new query
                time.sleep(wait_time * 4)
                _LOGGER.warning("Warning, got error requesting vacuum: %s", exp)

                positions = []
                continue

            if not positions:
                # Sometimes there is no new position
                # We just have to wait to get new ones
                no_pos_found += 1
                _LOGGER.warning("No new position found")
                time.sleep(wait_time)
            else:
                no_pos_found = 0
            # we need to wait a bit to not overload the vacuum
            time.sleep(wait_time)

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
        return self.send("set_voice", [state.value])

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
