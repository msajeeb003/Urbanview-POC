"""A local SMTP sink for machines without Docker (compose runs Mailpit instead): accepts every
message on ``127.0.0.1:1025`` and writes it as ``NNN.eml`` into a folder, so the order and login
e-mails can be read without a mail provider. No TLS, no authentication, development only.

    python -m core.mail.devsink [--port 1025] [--dir <folder>]

Point the API at it with ``SMTP_HOST=127.0.0.1``, ``SMTP_PORT=1025``, ``SMTP_USE_TLS=false`` (and a
worker, or ``CELERY_TASK_ALWAYS_EAGER=true`` to send inside the request).
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import tempfile
from pathlib import Path


def make_handler(out: Path):
    counter = itertools.count(len(list(out.glob("*.eml"))) + 1)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def send(line: str) -> None:
            writer.write((line + "\r\n").encode())

        send("220 localhost UrbanView dev sink")
        await writer.drain()
        while raw := await reader.readline():
            verb = raw.decode(errors="replace").strip().split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                send("250-localhost")
                send("250 8BITMIME")
            elif verb == "DATA":
                send("354 End data with <CR><LF>.<CR><LF>")
                await writer.drain()
                lines: list[bytes] = []
                while (line := await reader.readline()) not in (b".\r\n", b".\n", b""):
                    lines.append(line[1:] if line.startswith(b"..") else line)
                n = next(counter)
                (out / f"{n:03d}.eml").write_bytes(b"".join(lines))
                send(f"250 OK queued as devsink-{n:03d}")
            elif verb == "QUIT":
                send("221 Bye")
                await writer.drain()
                break
            else:  # MAIL, RCPT, RSET, NOOP …
                send("250 OK")
            await writer.drain()
        writer.close()

    return handle


async def serve(port: int, out: Path) -> None:
    server = await asyncio.start_server(make_handler(out), "127.0.0.1", port)
    print(f"dev SMTP sink on 127.0.0.1:{port}, messages in {out}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local SMTP sink for development.")
    parser.add_argument("--port", type=int, default=1025)
    parser.add_argument("--dir", type=Path, default=Path(tempfile.gettempdir()) / "urbanview-mail")
    args = parser.parse_args()
    args.dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(serve(args.port, args.dir))
