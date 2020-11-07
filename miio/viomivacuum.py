import itertools
import logging
import time
from collections import defaultdict
from datetime import timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import click

from .click_common import EnumType, command, format_output
from .device import Device
from .utils import pretty_seconds
from .vacuumcontainers import ConsumableStatus, DNDStatus

_LOGGER = logging.getLogger(__name__)


ERROR_CODES = {
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

    def __json__(self):
        return self.data


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


class ViomiMopMode(Enum):
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
        """
        return ViomiEdgeState(self.data["mode"])

    @property
    def mop_type(self):
        """Unknown mop_type values."""
        return self.data["mop_type"]

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
        """TODO: unknown"""
        return bool(self.data["has_newmap"])

    @property
    def mop_mode(self) -> ViomiMode:
        """Whether mopping is enabled and if so which mode

        TODO: is this really the same as mode?
        """
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
    def sw_info(self) -> str:
        """SoftWare info."""
        return self.data["sw_info"]

    @property
    def hepa_hours_left(self) -> timedelta:
        """HYPA left hours."""
        return timedelta(self.data["hypa_hours"])

    @property
    def hepa_life_left(self) -> int:
        """HYPA left life percent."""
        return self.data["hypa_life"]

    @property
    def charging(self) -> bool:
        """FIXME: True if device is charging?"""
        return bool(self.data["is_charge"])

    @property
    def working(self) -> bool:
        """FIXME: True if device is working?"""
        return bool(self.data["is_work"])

    @property
    def light_state(self) -> bool:
        """FIXME: True if device ?"""
        return bool(self.data["light_state"])

    @property
    def main_brush_hours_left(self) -> timedelta:
        """Main brush left hours."""
        return timedelta(self.data["main_brush_hours"])

    @property
    def main_brush_life_left(self) -> int:
        """Main brush left life percent."""
        return self.data["main_brush_life"]

    @property
    def map_number(self) -> int:
        """Number of saved maps."""
        return self.data["map_num"]

    @property
    def mop_hours_left(self) -> timedelta:
        """Mop left hours."""
        return timedelta(self.data["mop_hours"])

    @property
    def mop_life_left(self) -> int:
        """Mop left life percent."""
        return self.data["mop_life"]

    @property
    def mop_route(self) -> ViomiMopMode:
        """Pattern mode."""
        return ViomiMopMode(self.data["mop_route"])

    @property
    def order_time(self) -> int:
        """FIXME: ??? int or bool."""
        return self.data["order_time"]

    @property
    def repeat_state(self) -> bool:
        """Secondary clean up state."""
        return self.data["repeat_state"]

    @property
    def side_brush_hours_left(self) -> timedelta:
        """Side brush left hours."""
        return timedelta(self.data["side_brush_hours"])

    @property
    def side_brush_life_left(self) -> int:
        """Side brush left life percent."""
        return self.data["side_brush_life"]

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

    def __init__(
        self,
        ip: str = None,
        token: str = None,
        start_id: int = 0,
        debug: int = 0,
        lazy_discover: bool = True,
        timeout: int = 5,
    ) -> None:
        super().__init__(ip, token, start_id, debug, lazy_discover, timeout=0.5)

    def send(
        self,
        command: str,
        parameters: Any = None,
        retry_count=20,
        *,
        extra_parameters=None,
    ) -> Any:
        # For retry_count to 20
        return super().send(
            command, parameters, retry_count=20, extra_parameters=extra_parameters
        )

    @command(
        default_output=format_output(
            "\n",
            "General\n"
            "=======\n\n"
            "State: {result.state}\n"
            "Battery status: {result.error}\n"
            "Battery: {result.battery}\n"
            "Box type: {result.bin_type}\n"
            "Fan speed: {result.fanspeed}\n"
            "Water grade: {result.water_grade}\n"
            "Mop mode: {result.mop_mode}\n"
            "Vacuum along the edges: {result.edge_state}\n"
            "Mop route pattern: {result.mop_route}\n"
            "Secondary Cleanup: {result.repeat_state}\n"
            "Voice state: {result.voice_state}\n"
            "Clean time: {result.clean_time}\n"
            "Clean area: {result.clean_area}\n"
            "\n"
            "Consumables\n"
            "===========\n\n"
            "* Left hours:\n"
            "  - HYPA filter: {result.hepa_hours_left}\n"
            "  - Main brush: {result.main_brush_hours_left}\n"
            "  - Mop: {result.mop_hours_left}\n"
            "  - Side brush: {result.side_brush_hours_left}\n"
            "* Life left:\n"
            "  - HEPA filter: {result.hepa_life_left} %\n"
            "  - Main brush: {result.main_brush_life_left} %\n"
            "  - Mop: {result.mop_life_left} %\n"
            "  - Side brush: {result.side_brush_life_left} %\n"
            "\n"
            "Map\n"
            "===\n\n"
            "Current map ID: {result.current_map_id}\n"
            "Remember map: {result.remember_map}\n"
            "Has map: {result.has_map}\n"
            "Has new map: {result.has_new_map}\n"
            "Number of maps: {result.map_number}\n"
            "\n"
            "Misc\n"
            "====\n\n"
            "Hardware version: {result.hw_info}\n"
            "Software version: {result.sw_info}\n"
            "\n"
            "Unknown properties\n"
            "=================\n\n"
            "Light state: {result.light_state}\n"
            "Working: {result.working}\n"
            "Charging: {result.charging}\n"
            "Mop type: {result.mop_type}\n"
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
            "hypa_hours",
            "hypa_life",
            "is_charge",
            "is_mop",
            "is_work",
            "light_state",
            "main_brush_hours",
            "main_brush_life",
            "map_num",
            "mode",
            "mop_hours",
            "mop_life",
            "mop_route",
            "mop_type",
            "order_time",
            "remember_map",
            "repeat_state",
            "run_state",
            "s_area",
            "s_time",
            "side_brush_hours",
            "side_brush_life",
            "start_time",
            "suction_grade",
            "sw_info",
            "v_state",
            "water_grade",
            "water_percent",
            "zone_data",
        ]

        values = self.get_properties(properties)

        return ViomiVacuumStatus(defaultdict(lambda: None, zip(properties, values)))

    @command()
    def start(self):
        """Start cleaning."""
        # TODO figure out the parameters
        # [actionMode, 1, roomIds.length, *list_of_room_ids]
        # action == ?
        # 1 = start, 3 = pause
        # 3rd param of set_mode_withroom is room_array_len and next are
        # room ids ([0, 1, 3, 11, 12, 13] = start cleaning rooms 11-13).
        # room ids are encoded in map and it's part of cloud api so best way
        # to get it is log between device <> mi home app
        # (before map format is supported).
        # "mode0": "Vacuum all",
        # "mode1": "Vacuum all",
        # "mode2": "Vacuum along the edges",
        # "mode3": "Vacuum area",
        # "mode4": "Vacuum spot",
        # "mode10": "Vacuum & mop all",
        # "mode11": "Vacuum & mop all",
        # "mode12": "Vacuum & mop along the edges",
        # "mode13": "Vacuum & mop area",
        # "mode14": "Vacuum & mop spot",
        # "mode20": "Mop all",
        # "mode21": "Mop all",
        # "mode22": "Mop along the edges",
        # "mode23": "Mop area",
        # "mode24": "Mop spot",
        self.send("set_mode_withroom", [0, 1, 0])

    @command()
    def stop(self):
        """FIXME: validate that Stop cleaning."""
        self.send("set_mode", [0])

    @command(click.argument("state", type=EnumType(ViomiEdgeState)))
    def set_edge(self, state: ViomiEdgeState):
        """Set or Unset edge mode."""
        return self.send("set_mode", [state.value])

    @command()
    def pause(self):
        """Pause cleaning."""
        self.send("set_mode_withroom", [0, 2, 0])

    @command(click.argument("speed", type=EnumType(ViomiVacuumSpeed)))
    def set_fan_speed(self, speed: ViomiVacuumSpeed):
        """Set fanspeed [silent, standard, medium, turbo]."""
        self.send("set_suction", [speed.value])

    @command(click.argument("watergrade", type=EnumType(ViomiWaterGrade)))
    def set_water_grade(self, watergrade: ViomiWaterGrade):
        """Set water grade [low, medium, high]."""
        self.send("set_suction", [watergrade.value])

    @command()
    def home(self):
        """Return to home."""
        self.send("set_charge", [1])

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

    @command(click.argument("mode", type=EnumType(ViomiMode)))
    def clean_mode(self, mode: ViomiMode):
        """Set the cleaning mode."""
        self.send("set_mop", [mode.value])

    @command(click.argument("mop_mode", type=EnumType(ViomiMopMode)))
    def set_route_pattern(self, mop_mode: ViomiMopMode):
        """Set the mop route pattern."""
        self.send("set_moproute", [mop_mode.value])

    @command()
    def consumable_status(self) -> ViomiConsumableStatus:
        """Return information about consumables."""
        return ViomiConsumableStatus(self.send("get_consumables"))

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

    @command(click.argument("language", type=EnumType(ViomiLanguage)))
    def set_language(self, language: ViomiLanguage):
        """Set the device's audio language."""
        return self.send("set_language", [language.value])

    @command(click.argument("state", type=EnumType(ViomiLedState)))
    def led(self, state: ViomiLedState):
        """Switch the button leds on or off."""
        return self.send("set_light", [state.value])

    @command(click.argument("state", type=EnumType(ViomiVoiceState)))
    def set_voice(self, state: ViomiVoiceState):
        """Switch the voice on or off."""
        return self.send("set_voice", [state.value])

    @command(click.argument("mode", type=EnumType(ViomiCarpetTurbo)))
    def carpet_mode(self, mode: ViomiCarpetTurbo):
        """Set the carpet mode."""
        return self.send("set_carpetturbo", [mode.value])

    @command()
    def fan_speed_presets(self) -> Dict[str, int]:
        """Return dictionary containing supported fanspeeds."""
        return {x.name: x.value for x in list(ViomiVacuumSpeed)}

    @command(click.argument("state", type=bool))
    def set_repeat(self, state: bool):
        """Set or Unset repeat mode."""
        return self.send("set_repeat", [int(state)])

    @command()
    def get_maps(self) -> List[Dict[str, Any]]:
        """Return map list."""
        return self.send("get_map")

    @command(click.argument("state", type=bool))
    def set_remember(self, state: bool):
        """Set remenber map state."""
        return self.send("set_remember", [int(state)])

    @command(click.argument("map_id", type=int))
    def set_map(self, map_id: int):
        """Change current map."""
        maps = self.get_maps()
        if map_id not in [m["id"] for m in maps]:
            return "Map id {} doesn't exists".format(map_id)
        return self.send("set_map", [map_id])

    @command(click.argument("map_id", type=int))
    def delete_map(self, map_id: int):
        """Delete map."""
        maps = self.get_maps()
        if map_id not in [m["id"] for m in maps]:
            return "Map id {} doesn't exists".format(map_id)
        return self.send("del_map", [map_id])

    @command(
        click.argument("map_id", type=int),
        click.argument("map_name", type=str),
    )
    def rename_map(self, map_id: int, map_name: str):
        """Rename map."""
        maps = self.get_maps()
        if map_id not in [m["id"] for m in maps]:
            return "Map id {} doesn't exists".format(map_id)
        return self.send("rename_map", {"mapID": map_id, "name": map_name})

    @command(
        click.option("--map-id", type=int, default=None),
        click.option("--map-name", type=str, default=None),
    )
    def list_rooms(self, map_id: int, map_name: str):
        # TODO set map_id default to None and use the current map_id
        # if map_id is set, use set_map
        if map_name:
            map_id = None
            maps = self.get_maps()
            for map_ in maps:
                if map_["name"] == map_name:
                    map_id = map_["id"]
            if map_id is None:
                return "Error: Bad map name, should be in {}".format(
                    ", ".join([m["name"] for m in maps])
                )
        elif map_id:
            maps = self.get_maps()
            if map_id not in [m["id"] for m in maps]:
                return "Error: Bad map id, should be in {}".format(
                    ", ".join([str(m["id"]) for m in maps])
                )
        # https://github.com/homebridge-xiaomi-roborock-vacuum/homebridge-xiaomi-roborock-vacuum/blob/d73925c0106984a995d290e91a5ba4fcfe0b6444/index.js#L969
        # https://github.com/homebridge-xiaomi-roborock-vacuum/homebridge-xiaomi-roborock-vacuum#semi-automatic
        ret = self.send("get_ordertime", [])
        # ['1', '1', '32', '0', '0', '0', '1', '1', '11', '0', '1594139992', '2', '11', 'ami', '13', 'cuisine']
        raw_rooms = ret[0].split("_")[12:]
        rooms_iter = iter(raw_rooms)
        rooms = dict(itertools.zip_longest(rooms_iter, rooms_iter, fillvalue=None))
        return rooms
