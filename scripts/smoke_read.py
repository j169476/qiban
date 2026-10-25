"""Optional live read-only check. Does not bind an account or send any Bilibili action."""

import asyncio
import json
from pathlib import Path

from bili_assistant.bilibili import Bilibili
from bili_assistant.errors import AssistantError
from bili_assistant.storage import Store


async def main():
    store = Store(Path(__file__).resolve().parents[1] / ".dev" / "read-smoke")
    store.initialize()
    adapter = Bilibili(store)
    try:
        result = await asyncio.wait_for(adapter.video_context("BV1xx411c7mD"), timeout=50)
        print(
            json.dumps(
                {
                    "bvid": result["bvid"],
                    "title": result["title"],
                    "material_status": result["material_status"],
                    "coverage": result["coverage"],
                    "segments": len(result["segments"]),
                },
                ensure_ascii=False,
            )
        )
    except AssistantError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        raise SystemExit(2) from None


if __name__ == "__main__":
    asyncio.run(main())
