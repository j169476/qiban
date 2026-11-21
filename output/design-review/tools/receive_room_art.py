"""Publish the room backgrounds and static character poses used by the desktop."""
import argparse
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageStat

ROOT = Path(__file__).resolve().parents[3]
INBOX = ROOT / "output/design-art/room-static-v1"
DESKTOP = ROOT / "scripts/desktop_probe/Assets"
POSES = ("replying", "learning", "watching", "sleeping", "reading", "window", "tail")
SEASONS = {"spring": (3, 4, 5), "summer": (6, 7, 8), "autumn": (9, 10, 11), "winter": (12, 1, 2)}
# Local wall-clock minutes. The desktop and the preview read the same generated catalog.
PERIODS = (
    ("midnight", "深夜", 0), ("predawn", "黎明", 300), ("morning", "清晨", 420),
    ("forenoon", "上午", 540), ("noon", "正午", 720), ("afternoon", "午后", 840),
    ("dusk", "黄昏", 1020), ("night", "入夜", 1170),
)


def read_still(path, size):
    with Image.open(path) as image:
        if image.format != "WEBP" or image.n_frames != 1 or image.size != size:
            raise ValueError(f"{path}: expected one {size} WebP frame")
        return image.convert("RGBA")


def publish(inbox, desktop):
    with Image.open(inbox / "backgrounds/noon.webp") as noon:
        size = noon.size
    if size[0]*9 != size[1]*16:
        raise ValueError(f"Expected a 16:9 room, got {size}")
    reference_file = inbox/"lighting-reference.json"
    references = json.loads(reference_file.read_text(encoding="utf-8-sig")) if reference_file.exists() else {}
    catalog = {"size":size, "rendering":"pixel", "periods":[], "seasons":[], "characters":{"hoodie":{}}}
    destination = desktop / "room-stills"
    destination.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=desktop) as temp:
        stage = Path(temp)
        rooms = {}
        for period, label, start in PERIODS:
            filename = period+".webp"
            original = inbox/"backgrounds"/filename
            image = read_still(original,size)
            if image.getchannel("A").getextrema() != (255,255):
                raise ValueError(f"{original}: room needs an opaque background")
            rooms[period] = image.convert("RGB")
            shutil.copyfile(original,stage/filename)
            catalog["periods"].append({"id":period,"label":label,"start":start,"src":"room-stills/"+filename})
        for season, months in SEASONS.items():
            original = inbox/"seasons"/(season+".webp")
            sheet = read_still(original,(size[0]*len(PERIODS),size[1]))
            for i, (period, _, _) in enumerate(PERIODS):
                alpha = sheet.getchannel("A").crop((size[0]*i,0,size[0]*(i+1),size[1]))
                if alpha.getextrema() != (0,255):
                    raise ValueError(f"{original}: expected a visible window on transparency for {period}")
            filename = season+".webp"
            shutil.copyfile(original,stage/filename)
            catalog["seasons"].append({"id":season,"months":months,"src":"room-stills/"+filename})
        for pose in POSES:
            original = inbox/"characters/hoodie"/(pose+".webp")
            alpha = read_still(original,size).getchannel("A")
            if alpha.getextrema() != (0,255):
                raise ValueError(f"{original}: character needs a visible silhouette on transparency")
            # Sample the room only where this portrait sits, so a bedside lamp and window can differ.
            mask = alpha.point(lambda a: 255 if a >= 128 else 0)
            means = {period:ImageStat.Stat(room,mask).mean for period,room in rooms.items()}
            reference = means[references.get(pose,"noon")]
            lighting = {period:[round(max(.59,min(1.2,.20+.80*v/max(1,ref))),3)
                                for v,ref in zip(mean,reference)] for period,mean in means.items()}
            filename = pose+"-still.webp"
            shutil.copyfile(original,stage/filename)
            catalog["characters"]["hoodie"][pose] = {"still":"room-stills/"+filename,"lighting":lighting}
        for item in stage.glob("*.webp"):
            item.replace(destination/item.name)
        manifest = stage/"room-assets.js"
        manifest.write_text("window.qibanRoomScenes = "+json.dumps(catalog,ensure_ascii=False,indent=2)+";\n",encoding="utf-8")
        manifest.replace(desktop/manifest.name)
    print(f"Published {len(PERIODS)} periods, {len(SEASONS)} window sheets and {len(POSES)} poses to {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox",type=Path,default=INBOX)
    parser.add_argument("--desktop",type=Path,default=DESKTOP)
    args=parser.parse_args()
    publish(args.inbox,args.desktop)
