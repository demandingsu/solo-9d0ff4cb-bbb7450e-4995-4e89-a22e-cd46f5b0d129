"""只读调查 API:跳过误发批次,回放后续批次,找出会失败的批次与下发规范变化。"""
from fastapi.testclient import TestClient

from app import services
from app.db import SessionLocal
from app.main import app
from app.models import Batch

# ---------------------------------------------------------------- 工具


def make_batch(client, ops, title=""):
    r = client.post("/batches", json={"title": title, "ops": ops})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def verify(client, bid):
    r = client.post(f"/batches/{bid}/verify")
    assert r.status_code == 200, r.text
    return r.json()


def publish(client, bid):
    return client.post(f"/batches/{bid}/publish")


def publish_ok(client, bid):
    r = publish(client, bid)
    assert r.status_code == 200, r.text
    return r.json()


def investigate(client, bid):
    return client.get(f"/batches/{bid}/investigate")


def investigate_ok(client, bid):
    r = investigate(client, bid)
    assert r.status_code == 200, r.text
    return r.json()


def upsert_item(code, nominal, upper, lower, name="检验项目"):
    return {
        "op": "upsert_item",
        "item_code": code,
        "name": name,
        "nominal_um": nominal,
        "upper_um": upper,
        "lower_um": lower,
    }


def upsert_ws(code, gen, name="工位"):
    return {"op": "upsert_workstation", "workstation_code": code, "name": name, "terminal_generation": gen}


def claim(ws, item):
    return {"op": "claim_item", "workstation_code": ws, "item_code": item}


def unclaim(ws, item):
    return {"op": "unclaim_item", "workstation_code": ws, "item_code": item}


ITEM_SYM = upsert_item("ITEM-SYM", 10000, 10005, 9995, "对称项目")      # 公差 ±5
ITEM_ASYM = upsert_item("ITEM-ASYM", 20000, 20008, 19996, "非对称项目")  # +8/-4
WS_NEW = upsert_ws("WS-NEW", "new", "新终端工位")
WS_OLD = upsert_ws("WS-OLD", "old", "旧终端工位")


def setup_base(client):
    """B1:两个项目(对称/非对称)+ 新旧两个工位;旧工位领用对称项目。"""
    bid = make_batch(
        client,
        [ITEM_SYM, ITEM_ASYM, WS_NEW, WS_OLD,
         claim("WS-NEW", "ITEM-SYM"), claim("WS-NEW", "ITEM-ASYM"), claim("WS-OLD", "ITEM-SYM")],
        title="B1-基础数据",
    )
    verify(client, bid)
    publish_ok(client, bid)
    return bid


def spec_items(snapshot, item_code):
    return next((i for i in snapshot["items"] if i["item_code"] == item_code), None)


# ---------------------------------------------------------------- 基本调查与下发规范对比


def test_investigate_basic_spec_diff(client):
    """跳过误发批重放后续批:列出下发规范有变化的工位及两边内容。"""
    setup_base(client)
    # B2(误发):ITEM-SYM 改为 ±8,并新建工位 WS-EXTRA 领用它
    b2 = make_batch(
        client,
        [upsert_item("ITEM-SYM", 10000, 10008, 9992, "对称项目"),
         upsert_ws("WS-EXTRA", "new", "新增工位"),
         claim("WS-EXTRA", "ITEM-SYM")],
        "B2-误发",
    )
    verify(client, b2)
    publish_ok(client, b2)
    # B3(后续):ITEM-ASYM 改为 +9/-5
    b3 = make_batch(client, [upsert_item("ITEM-ASYM", 20000, 20009, 19995, "非对称项目")], "B3-后续")
    verify(client, b3)
    publish_ok(client, b3)

    out = investigate_ok(client, b2)
    assert out["batch_id"] == b2
    assert out["replayed_batches"] == [b3]
    assert out["failed_batches"] == []

    diffs = {d["workstation_code"]: d for d in out["spec_diffs"]}
    assert set(diffs) == {"WS-NEW", "WS-OLD", "WS-EXTRA"}

    # WS-NEW:两边都有 B3 的 ITEM-ASYM 新值;ITEM-SYM 一边 ±8、一边 ±5
    new_actual, new_without = diffs["WS-NEW"]["actual"], diffs["WS-NEW"]["without_batch"]
    assert spec_items(new_actual, "ITEM-ASYM")["upper_um"] == 20009
    assert spec_items(new_without, "ITEM-ASYM")["upper_um"] == 20009
    assert spec_items(new_actual, "ITEM-SYM")["upper_um"] == 10008
    assert spec_items(new_without, "ITEM-SYM")["upper_um"] == 10005

    # WS-OLD(旧终端):按对称公差格式对比
    assert spec_items(diffs["WS-OLD"]["actual"], "ITEM-SYM")["tolerance_um"] == 8
    assert spec_items(diffs["WS-OLD"]["without_batch"], "ITEM-SYM")["tolerance_um"] == 5

    # WS-EXTRA 只在实际侧存在(它由被跳过的 B2 创建)
    assert diffs["WS-EXTRA"]["actual"] is not None
    assert diffs["WS-EXTRA"]["without_batch"] is None
    assert spec_items(diffs["WS-EXTRA"]["actual"], "ITEM-SYM")["upper_um"] == 10008

    # 对比快照不含生成时间;内容未变的工位不出现
    for d in out["spec_diffs"]:
        for side in (d["actual"], d["without_batch"]):
            if side is not None:
                assert "generated_at" not in side
                assert "revision" not in side


def test_investigate_last_batch_shows_own_effect(client):
    """被调查批之后没有后续批次:回放为空,差异即该批自身的影响。"""
    setup_base(client)
    b2 = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10008, 9992, "对称项目")], "B2-误发")
    verify(client, b2)
    publish_ok(client, b2)

    out = investigate_ok(client, b2)
    assert out["replayed_batches"] == []
    assert out["failed_batches"] == []
    diffs = {d["workstation_code"]: d for d in out["spec_diffs"]}
    assert set(diffs) == {"WS-NEW", "WS-OLD"}
    assert spec_items(diffs["WS-OLD"]["actual"], "ITEM-SYM")["tolerance_um"] == 8
    assert spec_items(diffs["WS-OLD"]["without_batch"], "ITEM-SYM")["tolerance_um"] == 5


# ---------------------------------------------------------------- 回放失败:丢弃整批再继续


def test_investigate_skips_violating_batch_and_continues(client):
    """回放中违反旧终端公差规则的批次被整体丢弃,后续批次继续重放。"""
    # B1:旧工位 WS-X 领用对称项目;ITEM-ASYM 为非对称
    b1 = make_batch(
        client,
        [ITEM_SYM, ITEM_ASYM, upsert_ws("WS-X", "old", "试验工位"), claim("WS-X", "ITEM-SYM")],
        "B1",
    )
    verify(client, b1)
    publish_ok(client, b1)
    # B2(误发):把 WS-X 升级为新终端(合法:只领用了对称项目)
    b2 = make_batch(client, [upsert_ws("WS-X", "new", "试验工位")], "B2-误发")
    verify(client, b2)
    publish_ok(client, b2)
    # B3(后续):WS-X 领用非对称项目 —— 实际已是新终端,合法发布
    b3 = make_batch(client, [claim("WS-X", "ITEM-ASYM")], "B3-领用非对称")
    verify(client, b3)
    publish_ok(client, b3)
    # B4(后续):ITEM-SYM 改 ±6
    b4 = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")], "B4-改规范")
    verify(client, b4)
    publish_ok(client, b4)

    out = investigate_ok(client, b2)
    assert out["replayed_batches"] == [b3, b4]
    # B3 回放时 WS-X 仍是旧终端 → 违反旧终端公差规则 → 整批丢弃
    assert len(out["failed_batches"]) == 1
    fail = out["failed_batches"][0]
    assert fail["batch_id"] == b3
    assert fail["reason"] == "asymmetric_tolerance_on_old_terminal"
    assert [(v["workstation_code"], v["item_code"]) for v in fail["violations"]] == [
        ("WS-X", "ITEM-ASYM")
    ]
    # B4 不受影响继续重放:无 B2 的一侧 WS-X 仍是旧终端,ITEM-SYM 已是 ±6,且无 ITEM-ASYM
    diffs = {d["workstation_code"]: d for d in out["spec_diffs"]}
    actual, without = diffs["WS-X"]["actual"], diffs["WS-X"]["without_batch"]
    assert actual["terminal_generation"] == "new"
    assert spec_items(actual, "ITEM-ASYM") is not None
    assert without["terminal_generation"] == "old"
    assert spec_items(without, "ITEM-ASYM") is None
    assert spec_items(without, "ITEM-SYM")["tolerance_um"] == 6


def test_investigate_skips_op_error_batch(client):
    """回放中引用不存在项目/工位的批次被丢弃并给出原因,后续继续。"""
    setup_base(client)
    # B2(误发):新建对称项目 ITEM-NEW 并让 WS-NEW 领用
    b2 = make_batch(
        client,
        [upsert_item("ITEM-NEW", 5000, 5003, 4997, "新增项目"), claim("WS-NEW", "ITEM-NEW")],
        "B2-误发",
    )
    verify(client, b2)
    publish_ok(client, b2)
    # B3(后续):旧工位也领用 ITEM-NEW(对称,合法)
    b3 = make_batch(client, [claim("WS-OLD", "ITEM-NEW")], "B3-旧工位领用新项目")
    verify(client, b3)
    publish_ok(client, b3)
    # B4(后续):ITEM-SYM 改 ±7
    b4 = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10007, 9993, "对称项目")], "B4")
    verify(client, b4)
    publish_ok(client, b4)

    out = investigate_ok(client, b2)
    assert out["replayed_batches"] == [b3, b4]
    # 少了 B2 → ITEM-NEW 不存在 → B3 引用失败被丢弃
    assert len(out["failed_batches"]) == 1
    fail = out["failed_batches"][0]
    assert fail["batch_id"] == b3
    assert fail["reason"] == "op_error"
    assert "ITEM-NEW" in fail["message"]
    # B4 继续重放:两边 ITEM-SYM 都是 ±7;ITEM-NEW 只在实际侧
    diffs = {d["workstation_code"]: d for d in out["spec_diffs"]}
    assert set(diffs) == {"WS-NEW", "WS-OLD"}
    assert spec_items(diffs["WS-NEW"]["actual"], "ITEM-NEW") is not None
    assert spec_items(diffs["WS-NEW"]["without_batch"], "ITEM-NEW") is None
    assert spec_items(diffs["WS-NEW"]["without_batch"], "ITEM-SYM")["upper_um"] == 10007
    assert spec_items(diffs["WS-OLD"]["actual"], "ITEM-NEW")["tolerance_um"] == 3
    assert spec_items(diffs["WS-OLD"]["without_batch"], "ITEM-NEW") is None


# ---------------------------------------------------------------- 回放依据


def test_publish_saves_replay_basis(client):
    """发布时同事务保存回放依据(发布前生效状态快照)。"""
    b1 = setup_base(client)
    b2 = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10008, 9992, "对称项目")], "B2")
    verify(client, b2)
    publish_ok(client, b2)

    assert client.get(f"/batches/{b1}").json()["replay_basis_available"] is True
    assert client.get(f"/batches/{b2}").json()["replay_basis_available"] is True

    session = SessionLocal()
    try:
        basis1 = session.get(Batch, b1).replay_basis
        assert basis1["version"] == services.REPLAY_BASIS_VERSION
        assert basis1["state"]["items"] == []  # B1 发布前还没有任何生效数据
        basis2 = session.get(Batch, b2).replay_basis["state"]
        items = {i["code"]: i for i in basis2["items"]}
        assert items["ITEM-SYM"]["upper_um"] == 10005  # B2 发布前的旧值
        assert {c["item_code"] for c in basis2["claims"]} == {"ITEM-SYM", "ITEM-ASYM"}
    finally:
        session.close()


def test_publish_rolled_back_when_basis_save_fails(client, monkeypatch):
    """回放依据保存失败 → 发布整体回滚、不生效。"""
    setup_base(client)
    b2 = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10008, 9992, "对称项目")], "B2")
    verify(client, b2)

    def boom(state):
        raise RuntimeError("模拟回放依据保存失败")

    monkeypatch.setattr(services, "build_replay_basis", boom)
    no_raise = TestClient(app, raise_server_exceptions=False)
    r = no_raise.post(f"/batches/{b2}/publish")
    assert r.status_code == 500

    # 发布未生效:批次仍是 verified,生效数据未变
    assert client.get(f"/batches/{b2}").json()["status"] == "verified"
    item = client.get("/items/ITEM-SYM").json()
    assert (item["upper_um"], item["version"]) == (10005, 1)

    monkeypatch.undo()
    publish_ok(client, b2)  # 恢复后可正常发布
    assert client.get("/items/ITEM-SYM").json()["upper_um"] == 10008


def test_investigate_old_batch_without_basis(client):
    """早期批次缺少回放依据 → 明确提示无法调查。"""
    setup_base(client)
    b2 = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10008, 9992, "对称项目")], "B2")
    verify(client, b2)
    publish_ok(client, b2)

    # 模拟升级前的旧数据:没有回放依据
    session = SessionLocal()
    try:
        session.get(Batch, b2).replay_basis = None
        session.commit()
    finally:
        session.close()

    assert client.get(f"/batches/{b2}").json()["replay_basis_available"] is False
    r = investigate(client, b2)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "replay_basis_missing"


def test_investigate_requires_published_batch(client):
    """仅已发布批次可调查;不存在的批次 404。"""
    bid = make_batch(client, [ITEM_SYM], "草稿")
    r = investigate(client, bid)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "batch_not_published"

    verify(client, bid)  # verified 但未发布,同样不可调查
    assert investigate(client, bid).status_code == 409

    assert investigate(client, 99999).status_code == 404


# ---------------------------------------------------------------- 交错发布与一致性


def test_investigate_interleaved_publish_order(client):
    """回放按实际发布顺序(而非创建顺序);交错发布场景下结果正确。"""
    b1 = setup_base(client)
    # A、B 都改 ITEM-SYM(相关实体,交错才会使核对失效);B 顺带改 ITEM-ASYM
    bid_a = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10006, 9994, "对称项目")], "A")
    bid_b = make_batch(
        client,
        [upsert_item("ITEM-SYM", 10000, 10007, 9993, "对称项目"),
         upsert_item("ITEM-ASYM", 20000, 20009, 19993, "非对称项目")],
        "B",
    )
    verify(client, bid_a)
    verify(client, bid_b)
    publish_ok(client, bid_b)
    r = publish(client, bid_a)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "verification_stale"
    verify(client, bid_a)
    publish_ok(client, bid_a)
    # 实际发布顺序:B1, B, A(与创建顺序 B1, A, B 不同)

    out = investigate_ok(client, b1)
    assert out["replayed_batches"] == [bid_b, bid_a]  # 按发布顺序回放
    assert out["failed_batches"] == []

    # 调查 B:只有 A 在它之后发布;少了 B → ITEM-ASYM 保持旧值,ITEM-SYM 两边一致
    out_b = investigate_ok(client, bid_b)
    assert out_b["replayed_batches"] == [bid_a]
    diffs = {d["workstation_code"]: d for d in out_b["spec_diffs"]}
    assert set(diffs) == {"WS-NEW"}  # ITEM-ASYM 只有 WS-NEW 领用
    actual, without = diffs["WS-NEW"]["actual"], diffs["WS-NEW"]["without_batch"]
    assert spec_items(actual, "ITEM-ASYM")["upper_um"] == 20009
    assert spec_items(without, "ITEM-ASYM")["upper_um"] == 20008
    assert spec_items(actual, "ITEM-SYM")["upper_um"] == 10006  # A 在两边都已重放
    assert spec_items(without, "ITEM-SYM")["upper_um"] == 10006


def test_investigate_is_readonly_and_publish_continues(client):
    """调查是只读的;调查前后可正常发布,再次调查取到新时点。"""
    setup_base(client)
    b2 = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10008, 9992, "对称项目")], "B2-误发")
    verify(client, b2)
    publish_ok(client, b2)
    b3 = make_batch(client, [upsert_item("ITEM-ASYM", 20000, 20009, 19995, "非对称项目")], "B3")
    verify(client, b3)
    publish_ok(client, b3)

    before_items = client.get("/items").json()
    before_batches = client.get("/batches").json()
    out1 = investigate_ok(client, b2)
    assert out1["replayed_batches"] == [b3]
    # 只读:调查不改变任何数据
    assert client.get("/items").json() == before_items
    assert client.get("/batches").json() == before_batches

    # 回放期间(前后)可正常发布;新批次进入下一次调查的回放范围
    b4 = make_batch(client, [unclaim("WS-NEW", "ITEM-ASYM")], "B4")
    verify(client, b4)
    publish_ok(client, b4)
    out2 = investigate_ok(client, b2)
    assert out2["replayed_batches"] == [b3, b4]
    diffs = {d["workstation_code"]: d for d in out2["spec_diffs"]}
    # B4 两边都重放 → ITEM-ASYM 两边都不在 WS-NEW 的下发里
    assert spec_items(diffs["WS-NEW"]["actual"], "ITEM-ASYM") is None
    assert spec_items(diffs["WS-NEW"]["without_batch"], "ITEM-ASYM") is None


def test_investigate_same_committed_point(client):
    """回放范围与对照规范取自同一已提交时点:两次调查结果一致(内容不变则 diff 不变)。"""
    setup_base(client)
    b2 = make_batch(client, [upsert_item("ITEM-SYM", 10000, 10008, 9992, "对称项目")], "B2")
    verify(client, b2)
    publish_ok(client, b2)
    b3 = make_batch(client, [upsert_item("ITEM-ASYM", 20000, 20009, 19995, "非对称项目")], "B3")
    verify(client, b3)
    publish_ok(client, b3)

    out1 = investigate_ok(client, b2)
    out2 = investigate_ok(client, b2)
    assert out1["spec_diffs"] == out2["spec_diffs"]
    assert out1["replayed_batches"] == out2["replayed_batches"] == [b3]
