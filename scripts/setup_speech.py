"""Install a local recognition model once; normal browsing never downloads weights."""

import argparse
import os
from pathlib import Path


def main():
    # Ordinary HTTPS is more predictable than the optional Xet transport on Windows proxies.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
    from huggingface_hub import snapshot_download

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parents[1] / "bili-assistant-data")
    args = parser.parse_args()
    target = args.data.resolve() / "models" / "faster-whisper-base"
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download("Systran/faster-whisper-base", local_dir=target, max_workers=2,
                      allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*"])
    print("Local speech model ready:", target)


if __name__ == "__main__":
    main()
