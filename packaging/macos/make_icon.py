"""Render the app's four audio bars into an ICNS using only the standard library."""
from pathlib import Path
import struct
import zlib


def chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)


def png(size):
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            u, v = (x + .5) / size, (y + .5) / size
            cx, cy = min(max(u, .21), .79), min(max(v, .21), .79)
            inside = (u - cx) ** 2 + (v - cy) ** 2 <= .17 ** 2
            rgba = (75, 112, 97, 255) if inside else (0, 0, 0, 0)
            for center, height in ((.29, .22), (.43, .40), (.57, .56), (.71, .31)):
                bx, by = min(max(u, center - .027), center + .027), min(max(v, .5-height/2+.024), .5+height/2-.024)
                if (u - bx) ** 2 + (v - by) ** 2 <= .024 ** 2:
                    rgba = (248, 250, 249, 255)
            rows.extend(rgba)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def generate(destination):
    content = bytearray()
    for kind, size in ((b"ic07", 128), (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024)):
        data = png(size)
        content.extend(kind + struct.pack(">I", len(data) + 8) + data)
    Path(destination).write_bytes(b"icns" + struct.pack(">I", len(content) + 8) + content)


if __name__ == "__main__":
    generate(Path(__file__).with_name("app.icns"))
