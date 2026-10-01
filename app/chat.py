"""Real-time chat, scoped to a booking dispute.

Not open messaging. A websocket connection only goes through if the caller
owns the booking, is staff, or has another booking for the same room that
overlaps this one (the person on the other side of a conflict).

Messages are published to Redis and every app process listens for them, so
this works even if the app is scaled to more than one process later.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth import fetch_account, read_token
from app.cache import async_redis_client
from app.db import pool
from app.ratelimit import allow

router = APIRouter()

MAX_MESSAGE_LENGTH = 2000
HISTORY_LIMIT = 50


class ConnectionManager:
    """Tracks which local websockets are listening to which booking's chat."""

    def __init__(self):
        self.rooms: dict[int, set[WebSocket]] = defaultdict(set)

    def add(self, booking_id, ws):
        self.rooms[booking_id].add(ws)

    def remove(self, booking_id, ws):
        self.rooms[booking_id].discard(ws)
        if not self.rooms[booking_id]:
            del self.rooms[booking_id]

    async def deliver_local(self, booking_id, payload):
        for ws in list(self.rooms.get(booking_id, ())):
            try:
                await ws.send_json(payload)
            except Exception:
                self.remove(booking_id, ws)


manager = ConnectionManager()


async def redis_listener():
    """Background task: relay every message published on chat:* to whichever
    local sockets are listening for that booking."""
    pubsub = async_redis_client.pubsub()
    await pubsub.psubscribe("chat:*")
    async for message in pubsub.listen():
        if message["type"] != "pmessage":
            continue
        booking_id = int(message["channel"].rsplit(":", 1)[1])
        await manager.deliver_local(booking_id, json.loads(message["data"]))


def _token_from_request(websocket: WebSocket) -> str | None:
    """Browsers can't send custom headers on a websocket upgrade, so the
    token travels as a subprotocol instead of a query string -- keeps it
    out of access logs and browser history."""
    raw = websocket.headers.get("sec-websocket-protocol")
    return raw.split(",")[0].strip() if raw else None


async def authenticate(websocket: WebSocket) -> dict | None:
    token = _token_from_request(websocket)
    if not token:
        return None
    try:
        claims = read_token(token)
    except Exception:
        return None
    row = fetch_account(int(claims["sub"]))
    if row is None or not row[3]:
        return None
    return {"id": row[0], "username": row[1], "role": row[2]}


def can_join(conn, booking_id, user) -> bool:
    booking = conn.execute(
        "SELECT room_id, user_id, starts_at, ends_at FROM bookings WHERE id = %s",
        (booking_id,),
    ).fetchone()
    if booking is None:
        return False
    room_id, owner_id, starts_at, ends_at = booking

    if user["role"] in ("moderator", "admin") or user["id"] == owner_id:
        return True

    contesting = conn.execute(
        "SELECT 1 FROM bookings WHERE room_id = %s AND user_id = %s AND id <> %s "
        "AND tstzrange(starts_at, ends_at, '[)') && tstzrange(%s, %s, '[)') LIMIT 1",
        (room_id, user["id"], booking_id, starts_at, ends_at),
    ).fetchone()
    return contesting is not None


@router.websocket("/ws/bookings/{booking_id}")
async def chat_socket(websocket: WebSocket, booking_id: int):
    user = await authenticate(websocket)
    if user is None:
        await websocket.close(code=4401)
        return

    with pool.connection() as conn:
        authorized = can_join(conn, booking_id, user)
    if not authorized:
        await websocket.close(code=4403)
        return

    # Echo the token back as the negotiated subprotocol, as the handshake expects.
    await websocket.accept(subprotocol=_token_from_request(websocket))
    manager.add(booking_id, websocket)

    try:
        with pool.connection() as conn:
            history = conn.execute(
                "SELECT sender_id, body, created_at FROM messages "
                "WHERE booking_id = %s ORDER BY created_at DESC LIMIT %s",
                (booking_id, HISTORY_LIMIT),
            ).fetchall()
        for sender_id, body, created_at in reversed(history):
            await websocket.send_json({
                "sender_id": sender_id, "body": body,
                "created_at": created_at.isoformat(),
            })

        while True:
            body = (await websocket.receive_text()).strip()
            if not body or len(body) > MAX_MESSAGE_LENGTH:
                continue

            if allow("chat", str(user["id"]), limit=20, window_seconds=60) is not None:
                await websocket.send_json({"error": "rate limited, slow down"})
                continue

            now = datetime.now(timezone.utc)
            with pool.connection() as conn:
                # Re-check on every message, not just at connect -- a
                # socket can stay open a while, and if staff resolve the
                # conflict mid-conversation this cuts access immediately.
                if not can_join(conn, booking_id, user):
                    await websocket.close(code=4403)
                    return
                conn.execute(
                    "INSERT INTO messages (booking_id, sender_id, body, created_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (booking_id, user["id"], body, now),
                )
                conn.commit()

            payload = {
                "sender_id": user["id"], "sender": user["username"],
                "body": body, "created_at": now.isoformat(),
            }
            await async_redis_client.publish(f"chat:{booking_id}", json.dumps(payload))

    except WebSocketDisconnect:
        pass
    finally:
        manager.remove(booking_id, websocket)
