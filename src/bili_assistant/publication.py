"""Reviewed local image assets and guarded, single-attempt illustrated dynamics."""

import hashlib
import io
from pathlib import Path

from PIL import Image

from .errors import AssistantError
from .media import MAX_IMAGE_BYTES, image_suffix, image_url
from .storage import atomic_write


def folder(store):
    result = (store.root / "assets" / "publication").resolve()
    if not result.is_relative_to(store.root):
        raise AssistantError("invalid_path", "发布素材目录超出本地数据目录")
    result.mkdir(parents=True, exist_ok=True)
    return result


def import_image(engine, path, origin, description):
    engine.mode("role")
    source = Path(path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= MAX_IMAGE_BYTES:
        raise AssistantError("publication_image", "需要明确的本地图片绝对路径，大小上限 5 MB")
    raw = source.read_bytes()
    suffix = image_suffix(raw)
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            width, height = picture.size
            if not (1 <= width <= 10000 and 1 <= height <= 10000 and width * height <= 25_000_000):
                raise ValueError()
            picture.verify()
    except (ValueError, OSError, Image.DecompressionBombError):
        raise AssistantError("publication_image", "图片无法解码或尺寸超限") from None
    digest = hashlib.sha256(raw).hexdigest()
    account = engine.store.read("manifest")["data"]["account_uid"]
    item_id = "image-" + hashlib.sha256((account + digest).encode()).hexdigest()[:24]
    filename = digest + suffix
    target = folder(engine.store) / filename
    if target.is_symlink():
        raise AssistantError("invalid_path", "素材文件不能是符号链接")
    try:
        old = engine.store.read("publication_images", item_id)
    except AssistantError as exc:
        if exc.code != "not_found":
            raise
        old = None
    atomic_write(target, raw)
    if old:
        return {"duplicate": True, **old, "path": str(target)}
    record = engine.store.write("publication_images", {
        "schema_version": 1, "id": item_id, "account_uid": account, "sha256": digest, "filename": filename,
        "width": width, "height": height, "origin": origin, "description": description,
        "source": {"type": "conversation", "recorded_at": engine.clock().isoformat(), "reference": engine.identity()},
        "locked": False,
    }, None, engine.identity(), item_id)
    return {**record, "path": str(target)}


def resolve(store, image_ids):
    account = store.read("manifest")["data"]["account_uid"]
    assets = []
    for reference in image_ids:
        item_id = reference["id"]
        record = store.read("publication_images", item_id)
        if record["version"] != reference["version"]:
            raise AssistantError("publication_image_changed", "图片记录已变化，请重新审阅最新版本")
        doc = record["data"]
        base = folder(store)
        path = base / doc["filename"]
        if (doc["account_uid"] != account or path.is_symlink() or path.resolve().parent != base
                or not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES):
            raise AssistantError("publication_image", "图片不属于当前账号或已不再是有效缓存")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != doc["sha256"]:
            raise AssistantError("publication_image_changed", "发布素材已改变，请重新导入并审阅")
        suffix = image_suffix(raw)
        assets.append({"id": item_id, "version": record["version"], "path": str(path), "sha256": doc["sha256"],
                       "width": doc["width"], "height": doc["height"], "suffix": suffix})
    return assets


async def post_once(api, before_send):
    from bilibili_api.utils.network import get_client

    request = await api._prepare_request()
    before_send()
    response = await get_client().request(**request)
    return api._process_response(response)


async def send_images(store, content, image_ids, credential, before_send):
    from bilibili_api.utils.network import Api, BiliAPIFile
    from bilibili_api.utils.utils import get_api

    assets = resolve(store, image_ids)
    api_specs = get_api("dynamic")["send"]
    pictures = []
    uploaded = False
    try:
        for asset in assets:
            # Match the pinned SDK's upload transport, without its shared temp/test.png or POST retry.
            api = Api(**api_specs["upload_img"], credential=credential)
            api.update_data(biz="new_dyn", category="daily")
            api.update_files(file_up=BiliAPIFile(path=asset["path"], mime_type={
                ".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[asset["suffix"]]))

            def guard_upload():
                before_send()
                if resolve(store, image_ids) != assets:
                    raise AssistantError("before_send_cancelled", "上传前素材版本已改变")

            result = await post_once(api, guard_upload)
            uploaded = True
            pictures.append({"img_src": image_url(result["image_url"]),
                             "img_width": result["image_width"], "img_height": result["image_height"]})
        contents = await content.get_contents(credential=credential)
        api = Api(**api_specs["instant"], credential=credential, json_body=True)
        api.update_data(dyn_req={"content": {"contents": contents}, "scene": 2, "pics": pictures,
                                "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
                                "attach_card": None})
        api.update_params(csrf=credential.bili_jct)

        def guard_publish():
            before_send()
            if resolve(store, image_ids) != assets:
                raise AssistantError("before_send_cancelled", "发布前素材版本已改变")

        return await post_once(api, guard_publish)
    except Exception:
        if uploaded:
            # A partial upload is a real side effect. Never free the reservation for an automatic retry.
            raise AssistantError("publication_partial", "部分图片可能已上传，动态发布未获确认；不自动重发") from None
        raise
