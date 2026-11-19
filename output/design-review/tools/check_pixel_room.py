"""Verify the static desktop catalog against the separate original art delivery."""
import argparse
import json
from pathlib import Path

from PIL import Image
from receive_room_art import DESKTOP, INBOX, PERIODS, POSES


def verify(inbox, desktop):
    raw = (desktop/"room-assets.js").read_text(encoding="utf-8")
    catalog = json.loads(raw.removeprefix("window.qibanRoomScenes = ").strip().removesuffix(";"))
    periods = catalog["periods"]
    assert [(p["id"],p["label"],p["start"]) for p in periods] == list(PERIODS)
    assert catalog["rendering"] == "pixel"
    pairs = [(inbox/"backgrounds"/(p["id"]+".webp"),desktop/p["src"]) for p in periods]
    poses = catalog["characters"]["hoodie"]
    assert set(poses) == set(POSES)
    for pose,layer in poses.items():
        assert set(layer) == {"still","lighting"}
        assert set(layer["lighting"]) == {p["id"] for p in periods}
        pairs.append((inbox/"characters/hoodie"/(pose+".webp"), desktop/layer["still"]))
    assert {path for _,path in pairs} == set((desktop/"room-stills").glob("*.webp"))
    for original,published in pairs:
        assert original.read_bytes() == published.read_bytes(), published
        with Image.open(published) as im:
            assert list(im.size) == catalog["size"] and im.n_frames == 1, published
    print(f"{len(periods)} backgrounds, {len(poses)} portraits: all static files match the originals byte for byte")


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox",type=Path,default=INBOX)
    parser.add_argument("--desktop",type=Path,default=DESKTOP)
    args=parser.parse_args()
    verify(args.inbox,args.desktop)
