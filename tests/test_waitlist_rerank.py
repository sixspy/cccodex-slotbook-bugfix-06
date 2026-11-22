"""候补取消重排名次（BUGFIX-06）：先落失败测见红，再修写口。

正确性口径只能二选一，本卡封印为甲路径 same_tx：
持档锁后在同一事务内改「已取消」并重算后续名次写入名次册。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
from db import init_db  # noqa: E402
import db as dbmod  # noqa: E402

SLOT = "S-EVE-03"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c: TestClient, user: str = "admin") -> dict:
    r = c.post("/api/login", json={"username": user, "password": "123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _items(c: TestClient, h: dict) -> list[dict]:
    r = c.get("/api/waitlist", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _by_party(c: TestClient, h: dict) -> dict[str, dict]:
    return {i["party_key"]: i for i in _items(c, h) if i["slot_code"] == SLOT}


@pytest.fixture
def false_green_queue():
    """假绿夹具：取消回执成功、状态也改了，但名次不重排的现网现场。

    S-EVE-03 容量 6，先占满并确认，再让 P-Q1/P-Q2/P-Q3 溢出进候补。
    旧实现只保证取消回执成功——只验回执就是假绿，必须验名次递补。
    """
    with TestClient(app) as c:
        h = _login(c)
        for i in range(6):
            r = c.post(
                "/api/holds",
                headers=h,
                json={"slot_code": SLOT, "party_key": f"P-FILL{i}", "seats": 1},
            )
            assert r.status_code == 200, r.text
            assert r.json()["disposition"] == "占位"
            hid = r.json()["hold_id"]
            assert c.post(
                "/api/bookings/confirm", headers=h, json={"hold_id": hid}
            ).status_code == 200

        for p in ("P-Q1", "P-Q2", "P-Q3"):
            r = c.post(
                "/api/holds",
                headers=h,
                json={"slot_code": SLOT, "party_key": p, "seats": 1},
            )
            assert r.status_code == 200, r.text
            assert r.json()["disposition"] == "候补"

        q = _by_party(c, h)
        ids = {p: q[p]["id"] for p in ("P-Q1", "P-Q2", "P-Q3")}
        assert ids["P-Q1"] < ids["P-Q2"] < ids["P-Q3"]
        yield c, h, ids


def test_cancel_success_must_rerank_following_waiters(false_green_queue):
    """失败测①：取消回执成功，但后面的人必须升上去。"""
    c, h, ids = false_green_queue
    r = c.post(
        "/api/waitlist/cancel", headers=h, json={"wait_id": ids["P-Q1"]}
    )
    assert r.status_code == 200, r.text  # 假绿：回执成功
    q = _by_party(c, h)
    assert q["P-Q1"]["status"] == "已取消"  # 假绿：状态也改了
    # 真红点：禁止只改状态不重排，后到者必须递补
    assert q["P-Q2"]["queue_rank"] == 1, q
    assert q["P-Q3"]["queue_rank"] == 2, q
    # 可晋升头随名次同态
    assert q["P-Q2"]["promotable"] is True, q
    assert q["P-Q3"]["promotable"] is False, q
    assert r.json()["reranked"] is True
    assert r.json()["path"] == "same_tx"


def test_two_people_cancel_adjacent_must_contend_slot_lock(false_green_queue):
    """失败测②：两人同时取消相邻候补，后到只留争用痕，业务零写。"""
    c, h_admin, ids = false_green_queue
    h_clerk = _login(c, "clerk")

    # 先到者持档锁
    r = c.post(
        "/api/locks/acquire",
        headers=h_admin,
        json={"lock_key": f"slot:{SLOT}"},
    )
    assert r.status_code == 200, r.text

    # 后到者取消相邻候补：必须抢锁失败
    late = c.post(
        "/api/waitlist/cancel", headers=h_clerk, json={"wait_id": ids["P-Q2"]}
    )
    assert late.status_code == 409, late.text

    q = _by_party(c, h_admin)
    assert q["P-Q2"]["status"] == "候补中"  # 后到零业务写
    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    assert any(
        x["lock_key"] == f"slot:{SLOT}" and x["actor"] == "clerk" for x in cont
    ), cont  # 只留争用痕
    audits = c.get("/api/audit", headers=h_admin).json()["items"]
    assert not any(
        a["actor"] == "clerk"
        and "waitlist_cancel" in a["action"]
        and a["target"] == str(ids["P-Q2"])
        for a in audits
    ), audits

    # 先到者继续取消相邻 P-Q1，P-Q2 递补为头名
    ok = c.post(
        "/api/waitlist/cancel", headers=h_admin, json={"wait_id": ids["P-Q1"]}
    )
    assert ok.status_code == 200, ok.text
    q = _by_party(c, h_admin)
    assert q["P-Q2"]["queue_rank"] == 1, q
    assert q["P-Q2"]["promotable"] is True, q
    assert q["P-Q3"]["queue_rank"] == 2 and q["P-Q3"]["promotable"] is False, q


def test_atomic_rollback_keeps_cancel_and_rank_together(false_green_queue, monkeypatch):
    """甲路径原子性：重排后若落库异常，取消与名次同滚，两边零改。"""
    c, h, ids = false_green_queue
    from db import connect as real_connect
    import waitlist_svc

    calls = {"n": 0}
    real_rerank = waitlist_svc.rerank_slot

    def boom(conn, slot_id, now=None):
        calls["n"] += 1
        real_rerank(conn, slot_id, now)
        raise RuntimeError("名次册写入炸了")

    monkeypatch.setattr(waitlist_svc, "rerank_slot", boom)
    with pytest.raises(RuntimeError):
        c.post("/api/waitlist/cancel", headers=h, json={"wait_id": ids["P-Q1"]})
    assert calls["n"] == 1

    # 回滚后：取消未生效，名次未动，档锁已释放
    with real_connect() as conn:
        row = conn.execute("SELECT status FROM waitlist WHERE id=?", (ids["P-Q1"],)).fetchone()
        assert row["status"] == "候补中"
        locks = conn.execute("SELECT COUNT(*) c FROM slot_locks").fetchone()["c"]
        assert locks == 0
    q = _by_party(c, h)
    # 队列仍是取消前原样：P-Q1 还在队首，名次未动
    assert q["P-Q1"]["queue_rank"] == 1, q
    assert q["P-Q2"]["queue_rank"] == 2, q
    assert q["P-Q3"]["queue_rank"] == 3, q


def test_real_concurrent_adjacent_cancel_only_one_wins(false_green_queue, monkeypatch):
    """真并发：先到者持锁在途时，后到者取消相邻候补必须 409 且零写。

    借重排钩子把先到者按在事务里，制造真实重叠窗口；后到者撞锁只留争用痕。
    """
    import threading
    import time

    import waitlist_svc

    c, h_admin, ids = false_green_queue
    h_clerk = _login(c, "clerk")
    real_rerank = waitlist_svc.rerank_slot
    in_tx = threading.Event()

    def slow_rerank(conn, slot_id, now=None):
        result = real_rerank(conn, slot_id, now)
        in_tx.set()
        time.sleep(0.6)  # 先到者持锁在途，窗口内后到者必撞锁
        return result

    monkeypatch.setattr(waitlist_svc, "rerank_slot", slow_rerank)
    results: dict[str, object] = {}

    def first():
        results["first"] = c.post(
            "/api/waitlist/cancel", headers=h_admin, json={"wait_id": ids["P-Q1"]}
        )

    def second():
        assert in_tx.wait(5), "先到者未进入事务"
        results["second"] = c.post(
            "/api/waitlist/cancel", headers=h_clerk, json={"wait_id": ids["P-Q2"]}
        )

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)

    assert results["first"].status_code == 200, results["first"].text
    assert results["second"].status_code == 409, results["second"].text

    q = _by_party(c, h_admin)
    assert q["P-Q1"]["status"] == "已取消"
    assert q["P-Q2"]["status"] == "候补中"  # 后到零业务写
    assert q["P-Q2"]["queue_rank"] == 1 and q["P-Q2"]["promotable"] is True, q
    recon = c.get(f"/api/reconcile/{SLOT}", headers=h_admin).json()
    assert recon["waitlist_rank_ok"] is True, recon
    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    assert any(
        x["lock_key"] == f"slot:{SLOT}" and x["actor"] == "clerk" for x in cont
    ), cont


def test_deviation_guard_rejects_and_audits(false_green_queue, monkeypatch):
    """偏航护栏：若重排没落名次册（只剩空洞），记「候补重排偏航」并拒写。"""
    import waitlist_svc

    c, h, ids = false_green_queue
    monkeypatch.setattr(waitlist_svc, "rerank_slot", lambda conn, slot_id, now=None: [])
    r = c.post("/api/waitlist/cancel", headers=h, json={"wait_id": ids["P-Q1"]})
    assert r.status_code == 409, r.text
    assert "偏航" in r.json()["detail"], r.text

    # 取消同滚：P-Q1 仍是候补中；名次原样
    q = _by_party(c, h)
    assert q["P-Q1"]["status"] == "候补中", q
    assert q["P-Q1"]["queue_rank"] == 1, q
    # 偏航痕独立落盘
    audits = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["action"] == "候补重排偏航" for a in audits), audits


def test_promote_reranks_and_only_head_may_jump(false_green_queue):
    """晋升写口同态：非头名禁越位；头名晋升须同事务重排。"""
    c, h, ids = false_green_queue
    # 档期已满：越位与容量两道闸都应拦住
    late = c.post("/api/waitlist/promote", headers=h, json={"wait_id": ids["P-Q3"]})
    assert late.status_code == 409, late.text
    assert "越位" in late.json()["detail"], late.text

    # 真腾一席：把容量 +1，模拟释放后候补递补
    from db import connect as real_connect

    with real_connect() as conn:
        conn.execute("UPDATE slots SET capacity=capacity+1 WHERE code=?", (SLOT,))
        conn.commit()

    ok = c.post("/api/waitlist/promote", headers=h, json={"wait_id": ids["P-Q1"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["reranked"] is True
    q = _by_party(c, h)
    assert q["P-Q1"]["status"] == "已晋升"
    assert q["P-Q2"]["queue_rank"] == 1 and q["P-Q2"]["promotable"] is True, q
    assert q["P-Q3"]["queue_rank"] == 2 and q["P-Q3"]["promotable"] is False, q
    recon = c.get(f"/api/reconcile/{SLOT}", headers=h).json()
    assert recon["waitlist_rank_ok"] is True and recon["waitlist_head"] == ids["P-Q2"]


def test_reconcile_flags_rank_hole(false_green_queue):
    """对账异态：若名次册出空洞，对账必须报分裂（防假绿）。"""
    c, h, ids = false_green_queue
    ok = c.get(f"/api/reconcile/{SLOT}", headers=h).json()
    assert ok["waitlist_rank_ok"] is True and ok["ok"] is True, ok

    from db import connect as real_connect

    with real_connect() as conn:
        conn.execute(
            "UPDATE waitlist_rank SET queue_rank=9 WHERE wait_id=?", (ids["P-Q2"],)
        )
        conn.commit()
    bad = c.get(f"/api/reconcile/{SLOT}", headers=h).json()
    assert bad["waitlist_rank_ok"] is False and bad["ok"] is False, bad
    assert c.get("/api/reconcile", headers=h).json()["all_ok"] is False


def test_cancel_audit_and_five_surfaces_same(false_green_queue):
    """五处同态：候补页名次 / 名次列 / 可晋升头 / 对账 / 审计一致。"""
    c, h, ids = false_green_queue
    r = c.post("/api/waitlist/cancel", headers=h, json={"wait_id": ids["P-Q1"]})
    assert r.status_code == 200, r.text

    page = _by_party(c, h)
    assert page["P-Q2"]["queue_rank"] == 1 and page["P-Q2"]["promotable"] is True
    recon = c.get(f"/api/reconcile/{SLOT}", headers=h).json()
    assert recon["waitlist_rank_ok"] is True
    assert recon["waitlist_head"] == ids["P-Q2"]
    proof = c.get("/api/proof/P-Q2", headers=h).json()
    assert proof["waitlist"][0]["queue_rank"] == 1

    audits = c.get("/api/audit", headers=h).json()["items"]
    hit = [a for a in audits if a["action"] == "waitlist_cancel" and a["target"] == str(ids["P-Q1"])]
    assert hit, audits
    import json

    payload = json.loads(hit[0]["payload"])
    assert payload["path"] == "same_tx" and payload["reranked"] is True
    assert payload["reranked_count"] == 2 and payload["head_after"] == ids["P-Q2"]
