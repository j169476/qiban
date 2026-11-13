"""Check a single previously enrolled private test target with the packaged reader."""
import argparse
import asyncio
import json
from pathlib import Path

from assemble_application import validate

from companion_wechat.module import ReconnectingTransport
from companion_wechat.receiver_transport import SubprocessTransport, WorkerError


async def check(args):
    runtime=args.runtime.resolve(strict=True)
    validate(runtime, "Qiban.WeChatReader.exe")
    saved=json.loads(args.enrollment.read_text(encoding="utf8"))
    private=[item for item in saved["enrollment"]["targets"].values() if item["kind"]=="private"]
    if len(private)!=1:
        raise ValueError("Expected one previously enrolled private test target")
    worker=SubprocessTransport(executable=runtime/"Qiban.WeChatReader.exe")
    reader=ReconnectingTransport(None,worker=worker)
    result={"verified":False}
    try:
        await reader.request("open",config=saved["config"],enrollment=saved["enrollment"],checkpoint=saved["checkpoint"])
        current=await reader.request("resolve_alias",hint=args.hint)
        result["verified"]=current["username"]==private[0]["username"]
        result["status"]="verified" if result["verified"] else "target_changed_or_unavailable"
    except WorkerError as error:
        result["status"]=error.code
    finally:
        await reader.close()
    args.evidence.write_text(json.dumps(result),encoding="utf8")
    print(json.dumps(result))
    return 0 if result["verified"] else 1


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("runtime","enrollment","evidence"):
        parser.add_argument("--"+name,required=True,type=Path)
    parser.add_argument("--hint",required=True)
    raise SystemExit(asyncio.run(check(parser.parse_args())))
