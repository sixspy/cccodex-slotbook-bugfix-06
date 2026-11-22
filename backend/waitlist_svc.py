"""候补队列：列表、取消（同事务重排）、手工晋升。

BUGFIX-06：取消候补必须连带重排名次，口径封为甲路径 `same_tx`
（持档锁后同事务改「已取消」并重算后续名次写入名次册 `waitlist_rank`）。
禁止只改状态或只从列表删行；偏航记「候补重排偏航」并拒写。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, rerank_slot, waitlist_rank_state, write_audit
from locks import release, try_acquire

router = APIRouter(tags=["waitlist"])

RERANK_PATH = "same_tx"
DEVIATION = "候补重排偏航"


def _acquire_or_contend(conn, lock_key: str, user: str) -> None:
    """抢档锁；后到只落争用痕并 409，业务零写。"""
    if try_acquire(conn, lock_key, user):
        conn.commit()  # 锁行跨请求可见
        return
    conn.commit()  # 争用痕须落盘：否则异常回滚会把它一起吞掉
    raise HTTPException(409, "争用：后到只记争用，不写业务")


def _guard_rank_or_abort(conn, user: str, slot_id: int, slot_code: str) -> None:
    """偏航护栏：重排后名次必须连续且可晋升头唯一，否则记偏航并拒写。"""
    state = waitlist_rank_state(conn, slot_id)
    if state["waitlist_rank_ok"]:
        return
    conn.rollback()  # 取消与名次同滚，两边零改
    write_audit(conn, user, DEVIATION, slot_code, state)
    conn.commit()  # 偏航痕须独立落盘
    raise HTTPException(409, "候补重排偏航：名次未连续，拒写")


class CancelIn(BaseModel):
    wait_id: int


class PromoteIn(BaseModel):
    wait_id: int


@router.get("/api/waitlist")
def list_wait(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT w.*, s.code AS slot_code,
                      r.queue_rank AS rank_rank, r.promotable AS rank_promotable
               FROM waitlist w
               JOIN slots s ON s.id=w.slot_id
               LEFT JOIN waitlist_rank r ON r.wait_id=w.id
               ORDER BY w.slot_id ASC, w.id ASC LIMIT 200"""
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            # 名次与可晋升头一律读名次册；已取消行不再占名次
            active = d["status"] == "候补中"
            d["queue_rank"] = d.pop("rank_rank") if active else None
            d["promotable"] = bool(d.pop("rank_promotable")) if active else False
            items.append(d)
        return {"items": items}


@router.post("/api/waitlist/cancel")
def cancel_wait(body: CancelIn, authorization: AuthHeader = None):
    """甲路径：持档锁 → 同事务改已取消 + 重算后续名次，任一步失败两边同滚。"""
    user = require_writer(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM waitlist WHERE id=?", (body.wait_id,)).fetchone()
        if not row:
            raise HTTPException(404, "候补不存在")
        slot = conn.execute(
            "SELECT * FROM slots WHERE id=?", (row["slot_id"],)
        ).fetchone()
        if not slot:
            raise HTTPException(404, "档期不存在")
        lock_key = f"slot:{slot['code']}"
        # 抢不到档锁 → 409，只留争用痕，业务零写
        _acquire_or_contend(conn, lock_key, user)
        try:
            if row["status"] != "候补中":
                raise HTTPException(409, "候补状态不可取消")
            now = datetime.now().isoformat(timespec="seconds")
            # —— 同一事务：取消 + 重排 ——
            conn.execute("UPDATE waitlist SET status='已取消' WHERE id=?", (body.wait_id,))
            ranked = rerank_slot(conn, row["slot_id"], now)
            _guard_rank_or_abort(conn, user, row["slot_id"], slot["code"])
            write_audit(
                conn,
                user,
                "waitlist_cancel",
                str(body.wait_id),
                {
                    "path": RERANK_PATH,
                    "slot": slot["code"],
                    "reranked": True,
                    "reranked_count": len(ranked),
                    "head_after": ranked[0]["wait_id"] if ranked else None,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
    return {
        "ok": True,
        "path": RERANK_PATH,
        "reranked": True,
        "reranked_count": len(ranked),
        "head_after": ranked[0]["wait_id"] if ranked else None,
    }


@router.post("/api/waitlist/promote")
def promote_wait(body: PromoteIn, authorization: AuthHeader = None):
    """有空位时把候补转成占位（仍须再确认才入正式册）。

    只允许晋升名次册头名（可晋升头），晋升后同事务重排，名次随之前移。
    """
    user = require_writer(authorization)
    with connect() as conn:
        wrow = conn.execute("SELECT * FROM waitlist WHERE id=?", (body.wait_id,)).fetchone()
        if not wrow:
            raise HTTPException(404, "候补不存在")
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (wrow["slot_id"],)).fetchone()
        lock_key = f"slot:{slot['code']}"
        _acquire_or_contend(conn, lock_key, user)
        try:
            if wrow["status"] != "候补中":
                raise HTTPException(409, "候补非候补中")
            rank = conn.execute(
                "SELECT * FROM waitlist_rank WHERE wait_id=?", (body.wait_id,)
            ).fetchone()
            if not rank or rank["queue_rank"] != 1 or not rank["promotable"]:
                raise HTTPException(409, "非可晋升头，禁止越位晋升")
            pending = conn.execute(
                "SELECT COALESCE(SUM(seats),0) s FROM holds WHERE slot_id=? AND status='占位中'",
                (slot["id"],),
            ).fetchone()["s"]
            free = int(slot["capacity"]) - int(slot["booked"]) - int(pending)
            if wrow["seats"] > free:
                raise HTTPException(409, "仍无足够空位，禁止晋升")
            now = datetime.now().isoformat(timespec="seconds")
            expire = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
            cur = conn.execute(
                """INSERT INTO holds(slot_id,party_key,seats,status,expire_at,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (slot["id"], wrow["party_key"], wrow["seats"], "占位中", expire, now),
            )
            conn.execute("UPDATE waitlist SET status='已晋升' WHERE id=?", (body.wait_id,))
            ranked = rerank_slot(conn, slot["id"], now)
            _guard_rank_or_abort(conn, user, slot["id"], slot["code"])
            write_audit(
                conn,
                user,
                "waitlist_promote",
                wrow["party_key"],
                {
                    "hold_id": cur.lastrowid,
                    "path": RERANK_PATH,
                    "reranked_count": len(ranked),
                },
            )
            conn.commit()
            return {"ok": True, "hold_id": cur.lastrowid, "reranked": True}
        except Exception:
            conn.rollback()
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
