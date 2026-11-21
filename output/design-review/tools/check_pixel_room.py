"""Verify the static desktop catalog against the separate original art delivery."""
import argparse
import json
from pathlib import Path

from PIL import Image
from receive_room_art import DESKTOP, INBOX, PERIODS, POSES, SEASONS


def verify(inbox, desktop):
    raw = (desktop/"room-assets.js").read_text(encoding="utf-8")
    catalog = json.loads(raw.removeprefix("window.qibanRoomScenes = ").strip().removesuffix(";"))
    periods = catalog["periods"]
    assert [(p["id"],p["label"],p["start"]) for p in periods] == list(PERIODS)
    assert catalog["rendering"] == "pixel"
    pairs = [(inbox/"backgrounds"/(p["id"]+".webp"),desktop/p["src"]) for p in periods]
    seasons = catalog["seasons"]
    assert {s["id"]:tuple(s["months"]) for s in seasons} == SEASONS
    sheets = {desktop/s["src"] for s in seasons}
    pairs.extend((inbox/"seasons"/(s["id"]+".webp"),desktop/s["src"]) for s in seasons)
    poses = catalog["characters"]["hoodie"]
    assert set(poses) == set(POSES)
    for pose,layer in poses.items():
        assert set(layer) == {"still","lighting"}
        assert set(layer["lighting"]) == {p["id"] for p in periods}
        pairs.append((inbox/"characters/hoodie"/(pose+".webp"), desktop/layer["still"]))
    assert {path for _,path in pairs} == set((desktop/"room-stills").glob("*.webp"))
    width, height = catalog["size"]
    windows_by_period = [set() for _ in periods]
    for original,published in pairs:
        assert original.read_bytes() == published.read_bytes(), published
        with Image.open(published) as im:
            expected = (width*len(periods) if published in sheets else width, height)
            assert im.size == expected and im.n_frames == 1, published
            if published in sheets:
                frames = [im.crop((i*width,0,(i+1)*width,height)).tobytes() for i in range(len(periods))]
                assert len(set(frames)) == len(periods), published
                for variants,frame in zip(windows_by_period,frames):
                    variants.add(frame)
    assert all(len(variants) == len(seasons) for variants in windows_by_period)
    print(f"{len(periods)} backgrounds, {len(seasons)} seasonal sheets and {len(poses)} poses match their originals")


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox",type=Path,default=INBOX)
    parser.add_argument("--desktop",type=Path,default=DESKTOP)
    args=parser.parse_args()
    verify(args.inbox,args.desktop)
