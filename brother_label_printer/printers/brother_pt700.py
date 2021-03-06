"""
Brother P-Touch P700 Driver
"""
import io
import struct
import threading
import time
from collections import namedtuple
from enum import Enum, IntEnum
from itertools import islice
from typing import Iterable, Sequence
from math import ceil
import logging

import packbits
from PIL import Image, ImageChops

from ..label import Label
from . import BasePrinter, BaseStatus, BaseErrorStatus

logger = logging.getLogger(__name__)


def batch_iter_bytes(b, size):
    i = iter(b)

    return iter(lambda: bytes(tuple(islice(i, size))), b"")


def create_copies(b, size, copies):
    result = list()
    for i in range(copies):
        result.append(batch_iter_bytes(b, size))

    return result


class INFO_OFFSETS(IntEnum):
    PRINTHEAD_MARK = 0
    MODEL_CODE = 4
    ERROR_1 = 8
    ERROR_2 = 9
    MEDIA_WIDTH = 10
    MEDIA_TYPE = 11
    MODE = 15
    MEDIA_LENGTH = 17
    STATUS_TYPE = 18
    PHASE_TYPE = 19
    PHASE_NUMBER_HI = 20
    PHASE_NUMBER_LO = 21
    NOTIFY_NO = 22
    HARDWARE_SETTINGS = 26


class ERRORS(Enum):
    NO_MEDIA = 0
    CUTTER_JAM = 2
    WEAK_BATTERY = 3
    HV_ADAPTER = 6
    REPLACE_MEDIA = 8
    COVER_OPEN = 12
    OVERHEATING = 13
    UNKNOWN = -1


class MEDIA_TYPE(Enum):
    NO_MEDIA = 0
    LAMINATED_TAPE = 1
    NON_LAMINATED_TAPE = 2
    HEAT_SHRINK = 3
    INCOMPATIBLE = 4


class STATUS_TYPE(Enum):
    STATUS_REPLY = 0
    PRINTING_DONE = 1
    ERROR_OCCURRED = 2
    TURNED_OFF = 3
    NOTIFICATION = 4
    PHASE_CHANGE = 5


STATUS_TYPE_MAP = {
    0x00: STATUS_TYPE.STATUS_REPLY,
    0x01: STATUS_TYPE.PRINTING_DONE,
    0x02: STATUS_TYPE.ERROR_OCCURRED,
    0x04: STATUS_TYPE.TURNED_OFF,
    0x05: STATUS_TYPE.NOTIFICATION,
    0x06: STATUS_TYPE.PHASE_CHANGE,
}

MEDIA_TYPE_MAP = {
    0x00: MEDIA_TYPE.NO_MEDIA,
    0x01: MEDIA_TYPE.LAMINATED_TAPE,
    0x02: MEDIA_TYPE.NON_LAMINATED_TAPE,
    0x11: MEDIA_TYPE.HEAT_SHRINK,
    0xFF: MEDIA_TYPE.INCOMPATIBLE,
}

TapeInfo = namedtuple("TapeInfo", ["lmargin", "printarea", "rmargin", "width"])

MEDIA_WIDTH_INFO = {
    # media ID to tape width in dots
    0: TapeInfo(None, None, None, None),
    4: TapeInfo(52, 24, 52, 3.5),
    6: TapeInfo(48, 32, 48, 6.0),
    9: TapeInfo(39, 50, 39, 9.0),
    12: TapeInfo(29, 70, 29, 12.0),
    18: TapeInfo(8, 112, 8, 18.0),
    24: TapeInfo(0, 128, 0, 24.0),
}

ERROR_MASK = {
    0: ERRORS.NO_MEDIA,
    2: ERRORS.CUTTER_JAM,
    3: ERRORS.WEAK_BATTERY,
    6: ERRORS.HV_ADAPTER,
    8: ERRORS.REPLACE_MEDIA,
    12: ERRORS.COVER_OPEN,
    13: ERRORS.OVERHEATING,
}


def encode_line(bitmap_line: bytes, tape_info: TapeInfo) -> bytes:
    # The number of bits we need to add left or right is not always a multiple
    # of 8, so we need to convert our line into an int, shift it over by the
    # left margin and convert it to back again, padding to 16 bytes.

    line_int = int.from_bytes(bitmap_line, byteorder='big')
    line_int <<= tape_info.rmargin
    padded = line_int.to_bytes(16, byteorder='big')

    # pad to 16 bytes
    compressed = packbits.encode(padded)
    logger.debug("original bitmap: %s", bitmap_line)
    logger.debug("padded bitmap %s", padded)
    logger.debug("packbits compressed %s", compressed)
    # <h: big endian short (2 bytes)
    prefix = struct.pack("<H", len(compressed))

    return prefix + compressed


class Errors(BaseErrorStatus):
    def __init__(self, byte1: int, byte2: int) -> None:
        value = byte1 | (byte2 << 8)
        self.data = {
            err.name.lower(): bool(value & 1 << offset)

            for offset, err in ERROR_MASK.items()
        }

    def any(self):
        return any(self.data.values())

    def __getattr__(self, attr):
        return self.data[attr]

    def __repr__(self):
        return "<Errors {}>".format(self.data)


class Status(BaseStatus):
    def __init__(self, msg: Sequence) -> None:
        self.data = {i.name.lower(): msg[i.value] for i in INFO_OFFSETS}

        self.errors = Errors(self.error_1, self.error_2)
        self.tape_info = MEDIA_WIDTH_INFO[self.media_width]

    def ready(self):
        return not self.errors.any()

    def __getattr__(self, attr):
        return self.data[attr]


class P700(BasePrinter):
    """Printer Class for the Brother P-Touch P700/PT-700 Printer"""
    DPI = (180, 180)

    def __init__(self, io_obj: io.BufferedIOBase):
        super().__init__(io_obj)
        self.status = self.get_status()
        self._check_print_status = False

    def connect(self) -> None:
        """Connect to Printer"""
        self.io.write(b'\x00' * 100)
        self.io.write(b'\x1b\x40')

        logger.info("connected")

    def get_status(self) -> Status:
        """get status of the printer as ``Status`` object"""
        with self.io.lock:
            self.io.write(b'\x1B\x69\x53')
            data = self.io.read(32)

            if not data:
                raise IOError("No Response from printer")

            if len(data) < 32:
                raise IOError("Invalid Response from printer")

            return Status(data)

    def _debug_status(self):
        data = self.io.read(32)

        if data:
            logger.debug(Status(data))

    def get_label_width(self):
        return self.get_status().tape_info.width

    def print_label(self, label: Label, copies=1) -> Status:
        self.status = self.get_status()
        if not self.status.ready():
            raise IOError("Printer is not ready")

        img = label.render(height=self.status.tape_info.printarea)
        logger.debug("printarea is %s dots", self.status.tape_info.printarea)
        if not img.mode == "1":
            raise ValueError("render output has invalid mode '1'")
        img = img.transpose(Image.ROTATE_270).transpose(
            Image.FLIP_TOP_BOTTOM)
        img = ImageChops.invert(img)

        logger.info("label output size: %s", img.size)
        logger.info("tape info: %s", self.status.tape_info)

        img_bytes = img.tobytes()

        with self.io.lock:
            self._raw_print(
                self.status, create_copies(img_bytes, ceil(img.size[0] / 8), copies))

        return self.get_status()

    def _dummy_print(self, status: Status, document: Iterable[bytes]) -> None:
        for line in document:
            print(b'G' + encode_line(line, status.tape_info))
        print('------')
        for line in document:
            print(b'G' + encode_line(line, status.tape_info))

    def _print_status_check(self):
        while self._check_print_status:
            data = self.io.read(32)
            if len(data) == 32:
                self.status = Status(data)
            time.sleep(0.1)

    def _raw_print(self, status: Status, documents: list[Iterable[bytes]]) -> None:
        logger.info("starting print")

        self.connect()
        self._check_print_status = True

        status_thread = threading.Thread(target=self._print_status_check)
        status_thread.start()

        try:
            self.set_raster_mode()
            self.set_various_mode()
            self.set_advanced_mode()
            self.set_margin(14)
            self.set_compression_mode()

            for i in range(len(documents)):
                for line in documents[i]:
                    self.io.write(b'G' + encode_line(line, status.tape_info))

                self.print_empty_row()

                if i+1 < len(documents):
                    self.next_page()
                    end = time.time() + 20
                    while True:
                        if self.status.data.get('status_type') == 6 and self.status.data.get('phase_type') == 0:
                            break
                        time.sleep(0.1)
                        if time.time() > end:
                            raise TimeoutError("The printer did not reach receiving state for the next page")

            # end page
            self.last_page_end()
            logger.info("end of page")

            end = time.time() + 20
            while True:
                if self.status.data.get('status_type') == 1:
                    break
                time.sleep(0.1)
                if time.time() > end:
                    raise TimeoutError("The printer did not reach printing complete state")

        finally:
            self._check_print_status = False
            status_thread.join()

    def last_page_end(self):
        self.io.write(b'\x1A')

    def next_page(self):
        self.io.write(b'\x0C')

    def print_empty_row(self):
        self.io.write(b'\x5A')

    def set_compression_mode(self, tiff=True):
        data = b'\x4D' + self.build_byte({1: tiff})
        self.io.write(data)

    def set_margin(self, margin: int):
        data = b'\x1B\x69\x64' + margin.to_bytes(2, 'little')
        self.io.write(data)

    def set_advanced_mode(self, no_chain_printing=True, special_tape=False, no_buffer_clearing=False):
        """
        No chain printing
        When printing multiple copies, the labels are fed after the last one is printed.
        1:No chain printing(Feeding and cutting are performed after the last one is printed.)
        0:Chain printing(Feeding and cutting are not performed after the last one is printed.)
        Special tape (no cutting)
        Labels are not cut when special tape is installed.
        1.Special tape (no cutting) ON 0:Special tape (no cutting) OFF
        No buffer clearing when printing
        The expansion buffer of the machine is not cleared with the “no buffer clearing when printing”
        """
        data = b'\x1B\x69\x4B' + self.build_byte({3: no_chain_printing, 4: special_tape, 7: no_buffer_clearing})
        self.io.write(data)

    def set_various_mode(self, cut=True, mirror=False):
        """
        Autocut 1.Automatically cuts 0.Does not automatically cut
        Mirror printing 1. Mirror printing 0. No mirror printing
        """
        data = b'\x1B\x69\x4D' + self.build_byte({6: cut, 7: mirror})
        self.io.write(data)

    def set_raster_mode(self):
        self.io.write(b'\x1B\x69\x61\x01')

    @staticmethod
    def build_byte(bits: dict):
        return bytes([int(''.join([str(int(bits.get(i, 0))) for i in reversed(range(8))]), 2)])
