# SPDX-FileCopyrightText: (c) 2017 Blender Foundation
# SPDX-FileCopyrightText: (c) TagStudio Contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Extract an embedded thumbnail from a Blender file."""

import gzip
import os
import struct
import zstandard
from io import BufferedReader, BytesIO
from typing import BinaryIO
from pathlib import Path

from PIL import Image, ImageOps

import logging

def blend_extract_thumb(path: Path | str) -> tuple[bytes | None, int, int]:
    REND: bytes = b"REND"
    TEST: bytes = b"TEST"
    ENDB: bytes = b"ENDB"

    # Zstandard frame magic.
    ZSTD_MAGIC: bytes = b"\x28\xb5\x2f\xfd"

    blendfile: BinaryIO | None = None
    raw_file: BinaryIO | None = None

    try:
        # --------------------------------------------------------------
        # Open file and detect compression.
        # --------------------------------------------------------------
        raw_file: BufferedReader = open(path, "rb")

        magic: bytes = raw_file.read(4)

        # --------------------------------------------------------------
        # Zstandard-compressed Blender file.
        #
        # Blender's "Compress" option uses Zstandard compression.
        #
        # Decompress the entire file into BytesIO so that the rest of
        # the parser has normal seekable-file behaviour.
        # --------------------------------------------------------------
        if magic == ZSTD_MAGIC:
            logging.info("Zstandard-compressed blend file")

            raw_file.seek(0)

            dctx = zstandard.ZstdDecompressor()

            with dctx.stream_reader(raw_file) as reader:
                decompressed: bytes = reader.read()

            blendfile = BytesIO(decompressed)

            raw_file.close()
            raw_file = None

        # --------------------------------------------------------------
        # GZIP-compressed blend file.
        # --------------------------------------------------------------
        elif magic[:2] == b"\x1f\x8b":
            logging.info("GZIP-compressed blend file")

            raw_file.seek(0)

            with gzip.GzipFile(fileobj=raw_file, mode="rb") as reader:
                decompressed = reader.read()

            blendfile = BytesIO(decompressed)

            raw_file.close()
            raw_file = None

        # --------------------------------------------------------------
        # Normal uncompressed blend file.
        # --------------------------------------------------------------
        else:
            logging.info("Uncompressed blend file")

            raw_file.seek(0)
            blendfile = raw_file

        # --------------------------------------------------------------
        # Read Blender file header.
        #
        # Legacy header = 12 bytes
        # Blender 5+   = 17 bytes
        # --------------------------------------------------------------
        head: bytes = blendfile.read(17)

        logging.info("Head: %r", head)

        if not head.startswith(b"BLENDER"):
            logging.info("Header doesn't start with BLENDER")
            return None, 0, 0

        if len(head) < 12:
            logging.info("Header is too short")
            return None, 0, 0

        # --------------------------------------------------------------
        # Blender 5.0+ header
        #
        #   BLENDER17-01v0501
        #   01234567890123456
        #
        #   0-6   = BLENDER
        #   7-8   = header size
        #   9     = '-'
        #   10-11 = header format
        #   12    = 'v'
        #   13-16 = Blender version
        # --------------------------------------------------------------
        is_blender_5: bool = (
            len(head) >= 17
            and head[7:9].isdigit()
            and head[9:13] == b"-01v"
        )

        if is_blender_5:
            try:
                header_size: int = int(head[7:9])
                version: int = int(head[13:17])
            except ValueError:
                logging.info("Invalid Blender 5 header")
                return None, 0, 0

            logging.info(
                "Blender 5+ header: size=%d version=%d",
                header_size,
                version,
            )

            if header_size < 17:
                logging.info("Invalid Blender 5 header size")
                return None, 0, 0

            # We have already consumed 17 bytes.
            if header_size > 17:
                blendfile.seek(header_size - 17, os.SEEK_CUR)

            # ----------------------------------------------------------
            # Blender 5+ BHead
            #
            # 0-3    code
            # 4-7    SDNA index (uint32)
            # 8-15   old pointer (uint64)
            # 16-23  block size (uint64)
            # 24-31  count (uint64)
            #
            # Total = 32 bytes.
            # ----------------------------------------------------------
            sizeof_bhead: int = 32
            large_bhead: bool = True

            # Blender 5+ is little endian.
            int_endian_pair: str = "<ii"

        # --------------------------------------------------------------
        # Legacy Blender header
        #
        #   BLENDER-v400
        #
        #   7     pointer size
        #         '-' = 64-bit
        #         '_' = 32-bit
        #
        #   8     endian
        #         'v' = little endian
        #         'V' = big endian
        #
        #   9-11  Blender version
        # --------------------------------------------------------------
        else:
            is_64_bit: bool = head[7] == ord("-")
            is_big_endian: bool = head[8] == ord("V")

            try:
                version: int = int(head[9:12])
            except ValueError:
                logging.info("Invalid legacy Blender version")
                return None, 0, 0

            logging.info(
                "Legacy Blender header: version=%d 64bit=%s big_endian=%s",
                version,
                is_64_bit,
                is_big_endian,
            )

            # Blender pre-2.5 had no thumbnails.
            if version < 250:
                logging.info("Blender version has no thumbnails")
                return None, 0, 0

            sizeof_bhead: int = 24 if is_64_bit else 20
            large_bhead = False

            int_endian: str = ">" if is_big_endian else "<"
            int_endian_pair = int_endian + "ii"

            # We read 17 bytes above, but the old header is only 12.
            blendfile.seek(12, os.SEEK_SET)

        # --------------------------------------------------------------
        # Walk the BHeads until we find TEST.
        # --------------------------------------------------------------
        while True:
            block_offset: int = blendfile.tell()

            bhead: bytes = blendfile.read(sizeof_bhead)

            logging.debug(
                "BHead at offset %d: read %d/%d bytes: %r",
                block_offset,
                len(bhead),
                sizeof_bhead,
                bhead[:4],
            )

            # ENDB is a special partial BHead.
            if len(bhead) >= 4 and bhead[:4] == ENDB:
                logging.info("Reached ENDB before TEST")
                return None, 0, 0

            if len(bhead) < sizeof_bhead:
                logging.info(
                    "Truncated BHead at offset %d: got %d bytes, expected %d",
                    block_offset,
                    len(bhead),
                    sizeof_bhead,
                )
                return None, 0, 0

            code: bytes = bhead[:4]

            # ----------------------------------------------------------
            # Blender 5+
            #
            # The block size is at offset 16 and is uint64.
            # ----------------------------------------------------------
            if large_bhead:
                length: int = struct.unpack_from(
                    "<Q",
                    bhead,
                    16,
                )[0]

                sdna: int = struct.unpack_from(
                    "<I",
                    bhead,
                    4,
                )[0]

                count: int = struct.unpack_from(
                    "<Q",
                    bhead,
                    24,
                )[0]

                logging.debug(
                    "Blender 5 BHead: offset=%d code=%r size=%d sdna=%d count=%d",
                    block_offset,
                    code,
                    length,
                    sdna,
                    count,
                )

            # ----------------------------------------------------------
            # Legacy Blender
            #
            # code   = 0-3
            # length = 4-7
            # old    = 8-11/15
            # SDNA   = ...
            # count  = ...
            # ----------------------------------------------------------
            else:
                length = struct.unpack_from(
                    int_endian + "i",
                    bhead,
                    4,
                )[0]

                logging.debug(
                    "Legacy BHead: offset=%d code=%r size=%d",
                    block_offset,
                    code,
                    length,
                )

            # ----------------------------------------------------------
            # REND contains render information before TEST.
            # Skip its payload.
            # ----------------------------------------------------------
            if code == REND:
                if length < 0:
                    logging.info("Invalid REND length: %d", length)
                    return None, 0, 0

                logging.debug(
                    "Skipping REND payload: %d bytes",
                    length,
                )

                blendfile.seek(length, os.SEEK_CUR)
                continue

            # First non-REND block.
            break

        # --------------------------------------------------------------
        # We need the TEST block.
        # --------------------------------------------------------------
        if code != TEST:
            logging.info(
                "Expected TEST block, found %r at offset %d",
                code,
                block_offset,
            )
            return None, 0, 0

        # --------------------------------------------------------------
        # TEST payload:
        #
        #   int32 width
        #   int32 height
        #   RGBA pixel data
        # --------------------------------------------------------------
        dimensions: bytes = blendfile.read(8)

        if len(dimensions) != 8:
            logging.info("TEST block is missing dimensions")
            return None, 0, 0

        try:
            x: int
            y: int
            x, y = struct.unpack(
                int_endian_pair,
                dimensions,
            )
        except struct.error:
            logging.info("Unable to unpack thumbnail dimensions")
            return None, 0, 0

        logging.info(
            "Thumbnail dimensions: %dx%d",
            x,
            y,
        )

        # The TEST block length includes the two 32-bit dimensions.
        image_length: int = length - 8

        if x <= 0 or y <= 0:
            logging.info(
                "Invalid thumbnail dimensions: %dx%d",
                x,
                y,
            )
            return None, 0, 0

        expected_length: int = x * y * 4

        if image_length != expected_length:
            logging.info(
                "Thumbnail size mismatch: block=%d expected=%d",
                image_length,
                expected_length,
            )
            return None, 0, 0

        # --------------------------------------------------------------
        # Read RGBA thumbnail.
        # --------------------------------------------------------------
        image_buffer: bytes = blendfile.read(image_length)

        if len(image_buffer) != image_length:
            logging.info(
                "Thumbnail data truncated: got %d expected %d",
                len(image_buffer),
                image_length,
            )
            return None, 0, 0

        return image_buffer, x, y

    except (OSError, zstandard.ZstdError) as exc:
        logging.exception(
            "Unable to read/decompress blend file: %s",
            exc,
        )
        return None, 0, 0

    finally:
        if blendfile is not None:
            blendfile.close()

        if raw_file is not None and raw_file is not blendfile:
            raw_file.close()


def blend_thumb(file_in: Path | str) -> Image.Image | None:
    buf, width, height = blend_extract_thumb(file_in)
    if buf is None:
        return None
    image = Image.frombuffer(
        "RGBA",
        (width, height),
        buf,
    )
    image = ImageOps.flip(image)
    # Upscale Image so it looks better at higher resolutions.
    width, height = image.size
    ratio = height / width
    image = image.resize((512, round(512 * ratio)), Image.BICUBIC)
    return image
