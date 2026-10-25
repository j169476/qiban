"""Every supplied catalog excerpt is versioned evidence; unseen records are not."""
from copy import deepcopy

import pytest
from test_core_takeover import Model, incoming, make

from bili_assistant.cognition import packet, verify_versions
from companion_core.errors import AssistantError


def seeded(store, memory, count=8):
    settings = store.read("companion")
    settings["data"].update(context_record_limit=6, catalog_limit=10)
    store.write("companion", settings["data"], settings["version"], "test")
    for i in range(count):
        item_id = f"catalog-{i:02}"
        store.write("memories", {**memory, "id": item_id, "content": f"草图的尺寸要求 {i}"},
                    None, "test", item_id)


def catalog_only(memory):
    full = {(r["kind"], r["data"]["id"]) for r in memory["records"]}
    return next(item for item in memory["catalog"] if (item["kind"], item["id"]) not in full)


def change(store, item, action="edit"):
    if action == "delete":
        store.path(item["kind"], item["id"]).unlink()
        return
    current = store.read(item["kind"], item["id"])
    current["data"]["content"] += "；旧尺寸作废，必须重新询问。"
    store.write(item["kind"], current["data"], current["version"], "manual-edit", item["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["edit", "delete"])
async def test_core_catalog_only_change_invalidates_model_decision(store, memory, action):
    seeded(store, memory)

    class EditingModel(Model):
        async def generate(self, instructions, material, schema, images=(), **options):
            change(store, catalog_only(material["memory"]), action)
            return await super().generate(instructions, material, schema, images, **options)

    core, _, _, platform, _ = make(store, model=EditingModel())
    platform.incoming = [incoming()]
    core.start()
    event = (await core.poll())[0]
    with pytest.raises(AssistantError) as error:
        await core.handle(event)
    assert error.value.code == "context_changed"
    assert not platform.sent
    assert store.state("core-processed:" + event.id) is None


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_bilibili_packet_tracks_catalog_only_source(store, memory, action):
    seeded(store, memory)
    core, connector, _, _, _ = make(store)
    core.start()
    context, dependencies = packet(connector.engine, {"events": [incoming()], "video": None})
    item = catalog_only(context["memory"])
    assert (item["kind"], item["id"], item["version"]) in dependencies
    change(store, item, action)
    with pytest.raises(AssistantError) as error:
        verify_versions(store, dependencies)
    assert error.value.code == "context_changed"


@pytest.mark.asyncio
async def test_core_does_not_freeze_records_outside_supplied_context(store, memory):
    seeded(store, memory, 12)

    class EditingModel(Model):
        async def generate(self, instructions, material, schema, images=(), **options):
            visible = {r["id"] for r in material["memory"]["catalog"]}
            visible.update(r["data"]["id"] for r in material["memory"]["records"])
            item = next(r for r in store.records("memories")[0] if r["data"]["id"] not in visible)
            change(store, {"kind": "memories", "id": item["data"]["id"]})
            return await super().generate(instructions, material, schema, images, **options)

    core, _, _, platform, _ = make(store, model=EditingModel())
    platform.incoming = [incoming()]
    core.start()
    event = (await core.poll())[0]
    result = await core.handle(event)
    assert result["action"]["status"] == "succeeded"
    assert len(platform.sent) == 1


def test_bilibili_packet_does_not_freeze_unsupplied_records(store, memory):
    seeded(store, memory, 12)
    core, connector, _, _, _ = make(store)
    core.start()
    context, dependencies = packet(connector.engine, {"events": [incoming()], "video": None})
    visible = {r["id"] for r in context["memory"]["catalog"]}
    visible.update(r["data"]["id"] for r in context["memory"]["records"])
    item = next(r for r in store.records("memories")[0] if r["data"]["id"] not in visible)
    before = deepcopy(dependencies)
    change(store, {"kind": "memories", "id": item["data"]["id"]})
    verify_versions(store, dependencies)
    assert dependencies == before
