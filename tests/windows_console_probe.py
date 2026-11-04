"""Run the actual Windows driver in a hidden console; no model calls or paid API use."""

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from textual.events import Resize
from textual.geometry import Size

from ehv_agent.config import atomic_json
from ehv_agent.engine import Engine
from ehv_agent.events import Events
from ehv_agent.terminal import Coord, ScreenBufferInfo, terminal_session
from ehv_agent.ui import AgentApp


def probe(home, output):
    with terminal_session() as console:
        if console is None or console.size() is None:
            atomic_json(output, {"unavailable": True})
            return
        events = Events(home)
        engine = Engine(home, events)
        app = AgentApp(home, engine, events, terminal=console)
        result = {}

        async def check(pilot):
            await pilot.pause(0.5)
            # Simulate a buffer-size notification that does not describe the viewport.
            app.post_message(Resize(Size(240, 120), Size(240, 120)))
            await pilot.pause(0.5)
            app.action_redraw()
            await pilot.pause(0.5)
            info = ScreenBufferInfo()
            console.api.GetConsoleScreenBufferInfo(console.handle, ctypes.byref(info))
            api = console.api.ReadConsoleOutputCharacterW
            api.argtypes = [
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                Coord,
                ctypes.POINTER(wintypes.DWORD),
            ]
            api.restype = wintypes.BOOL
            count = info.size.x * info.size.y
            text = ctypes.create_unicode_buffer(count + 1)
            read = wintypes.DWORD()
            if not api(console.handle, text, count, Coord(0, 0), ctypes.byref(read)):
                raise OSError("Could not inspect native console buffer")
            result.update(
                size=list(app.size),
                viewport=list(console.size()),
                prompt_count=text.value.count("Describe a task"),
                footer_count=text.value.count("Quit"),
                brand_count=text.value.count("EHV AGENT"),
            )
            app.action_request_quit()
            await pilot.pause(1.1)

        try:
            app.run(size=console.size(), auto_pilot=check)
        finally:
            engine.shutdown()
            if engine.thread:
                engine.thread.join(timeout=3)
        atomic_json(output, result)


if __name__ == "__main__":
    probe(Path(sys.argv[1]), Path(sys.argv[2]))
