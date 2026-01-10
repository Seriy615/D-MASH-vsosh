import os
import hashlib
import json
import uuid
import time
from datetime import datetime
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Optional
from nacl.public import PrivateKey
from nacl.encoding import Base64Encoder

from core import state
from database import DatabaseManager
from crypto import CryptoManager

router = APIRouter()

# --- Pydantic Models ---
class LoginData(BaseModel):
    username: str
    password: str
class ConnectData(BaseModel):
    address: str
class SendData(BaseModel):
    target_id: str
    text: str
class RenameData(BaseModel):
    target_id: str
    name: Optional[str] = None
class ReadChatData(BaseModel):
    chat_id: str
class RouteIdRequest(BaseModel):
    sender_id: str
    receiver_id: str

# --- API ROUTES ---

@router.get("/")
async def root():
    return RedirectResponse(url="/auth/login.html")

# --- DEBUG ЭНДПОИНТЫ ДЛЯ ТЕСТОВ ---

@router.get("/api/debug/packet/{pkt_id}")
async def debug_packet_status(pkt_id: str):
    """Проверяет статус пакета (был ли виден или в очереди)"""
    if not state.system_db: return {"status": "offline"}
    async with state.system_db.conn.execute("SELECT received_at FROM seen_packets WHERE packet_id = ?", (pkt_id,)) as cursor:
        seen = await cursor.fetchone()
    async with state.system_db.conn.execute("SELECT count(*) as cnt FROM outbox WHERE packet_id = ?", (pkt_id,)) as cursor:
        outbox = await cursor.fetchone()
    return {
        "seen": bool(seen), 
        "received_at": seen['received_at'] if seen else None, 
        "in_outbox": outbox['cnt'] if outbox else 0
    }

@router.get("/api/debug/outbox")
async def debug_get_outbox():
    """Возвращает текущую очередь отправки"""
    if not state.system_db: return []
    async with state.system_db.conn.execute("SELECT * FROM outbox") as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

@router.get("/api/debug/routes")
async def debug_get_routes():
    """Возвращает таблицу маршрутизации"""
    if not state.system_db: return []
    async with state.system_db.conn.execute("SELECT * FROM routing_table WHERE expires_at > ?", (time.time(),)) as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

@router.post("/api/debug/get_route_ids")
async def debug_get_route_ids(data: RouteIdRequest):
    """Хелпер для тестов: вычисляет хеши маршрутов"""
    if not state.crypto: return {}
    return {
        "route_fwd": state.crypto.get_route_id(data.sender_id, data.receiver_id),
        "route_bwd": state.crypto.get_route_id(data.receiver_id, data.sender_id)
    }

# --- ОСНОВНЫЕ ЭНДПОИНТЫ ---

@router.post("/api/login")
async def login(data: LoginData):
    crypto = CryptoManager()
    crypto.derive_keys_from_password(data.username, data.password)
    new_user_id = crypto.my_id
    
    if state.is_logged_in and state.user_id != new_user_id:
        state.node.remove_active_user()
        if state.db: await state.db.close()
        state.is_logged_in = False
    
    if not state.is_logged_in:
        await state.system_db.register_local_user(new_user_id)
        
        state.user_id = new_user_id
        state.crypto = crypto
        
        state.db = DatabaseManager(f"node_{new_user_id}.db")
        state.db.set_crypto(crypto)
        await state.db.connect()
        
        state.node.set_active_user(new_user_id, state.db, crypto)
        
        # Загрузка офлайн сообщений
        offline_packets = await state.system_db.fetch_mailbox(new_user_id)
        if offline_packets:
            for pkt_json in offline_packets:
                try:
                    packet = json.loads(pkt_json)
                    # В Beta-2 отправитель зашит внутри E2EE контента
                    await state.node._deliver_to_active_user(packet, None)
                except: pass

        state.is_logged_in = True
    
    return {"status": "ok", "user_id": new_user_id}

@router.post("/api/logout")
async def logout():
    if state.is_logged_in:
        state.node.remove_active_user()
        if state.db: await state.db.close()
        state.db = None
        state.user_id = ""
        state.crypto = None
        state.is_logged_in = False
    return {"status": "ok"}

@router.post("/api/connect")
async def connect_peer(data: ConnectData):
    if not state.node: raise HTTPException(400, "Node not ready")
    res = await state.node.connect_to(data.address)
    return {"success": res}

@router.post("/api/send")
async def send_message(data: SendData):
    if not state.db: raise HTTPException(400)
    
    try: enc_net = state.crypto.encrypt_message(data.target_id, data.text)
    except: raise HTTPException(400, "Invalid Target ID")
    
    pkt_uuid = str(uuid.uuid4())
    enc_local = state.crypto.encrypt_db_field(data.text)
    
    # Сохраняем локально
    await state.db.conn.execute("""
        INSERT INTO messages (packet_id, chat_id, sender_id, content, timestamp, is_outgoing, is_read) 
        VALUES (?, ?, ?, ?, ?, 1, 1)
    """, (pkt_uuid, data.target_id, state.user_id, enc_local, datetime.now().isoformat()))
    await state.db.conn.execute("INSERT OR IGNORE INTO contacts (user_id, last_seen) VALUES (?, ?)", (data.target_id, datetime.now().isoformat()))
    await state.db.conn.commit()

    route_id = state.crypto.get_route_id(state.user_id, data.target_id)
    rev_id = state.crypto.get_route_id(data.target_id, state.user_id)
    
    route = await state.system_db.get_best_route(route_id)

    if route and not route['is_local']:
        # DATA
        packet = {"type": "DATA", "id": pkt_uuid, "route_id": route_id, "content": enc_net, "ttl": 20}
        await state.system_db.mark_packet_seen(pkt_uuid)
        await state.system_db.conn.execute("""
            INSERT INTO outbox (packet_id, next_hop_id, packet_json, exclude_peer) 
            VALUES (?, ?, ?, NULL)
        """, (pkt_uuid, route['next_hop_id'], json.dumps(packet)))
        p_type, status = "DATA", "sent"
    else:
        # PROBE
        # Алиса метит СВОЙ входящий канал (rev_id) как LOCAL
        await state.system_db.add_route(rev_id, "LOCAL", 0, is_local=1, remote_user_id=data.target_id)
        
        sig = state.crypto.sign_data(state.user_id + data.target_id)
        auth = state.crypto.encrypt_for_probe(data.target_id, json.dumps({"sid": state.user_id}))
        
        probe = {
            "type": "PROBE", "id": pkt_uuid, "route_id": route_id, "rev_id": rev_id, 
            "target_hash": state.crypto.get_target_hash(data.target_id), 
            "auth": auth, "sig": sig, "content": enc_net, "metric": 0, "ttl": 20
        }
        await state.system_db.mark_packet_seen(pkt_uuid)
        await state.system_db.conn.execute("""
            INSERT INTO outbox (packet_id, next_hop_id, packet_json, exclude_peer) 
            VALUES (?, NULL, ?, NULL)
        """, (pkt_uuid, json.dumps(probe)))
        p_type, status = "PROBE", "finding_route"

    await state.system_db.conn.commit()
    return {"status": status, "packet_id": pkt_uuid, "packet_type": p_type}

@router.get("/api/state")
async def get_state():
    if not state.node: return {"status": "offline"}
    return {
        "user_id": state.user_id if state.is_logged_in else "OFFLINE", 
        "peers": list(state.node.active_connections.keys())
    }

@router.get("/api/peers")
async def get_contacts():
    if not state.db: return []
    async with state.db.conn.execute("""
        SELECT c.user_id, c.nickname, 
        (SELECT COUNT(id) FROM messages WHERE chat_id = c.user_id AND is_read = 0 AND is_outgoing = 0) as unread_count 
        FROM contacts c
    """) as cursor:
        rows = await cursor.fetchall()
    res = []
    for r in rows:
        d = dict(r)
        if d['nickname']: d['nickname'] = state.crypto.decrypt_db_field(d['nickname'])
        res.append(d)
    return res

@router.get("/api/messages/{chat_id}")
async def get_chat_history(chat_id: str):
    if not state.db: return []
    async with state.db.conn.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY timestamp ASC", (chat_id,)) as cursor:
        rows = await cursor.fetchall()
    res = []
    for r in rows:
        d = dict(r)
        d['content'] = state.crypto.decrypt_db_field(d['content'])
        res.append(d)
    await state.db.conn.execute("UPDATE messages SET is_read = 1 WHERE chat_id = ? AND is_outgoing = 0", (chat_id,))
    await state.db.conn.commit()
    return res

@router.post("/api/rename")
async def rename_peer(data: RenameData):
    if not state.db: raise HTTPException(400)
    enc_name = state.crypto.encrypt_db_field(data.name) if data.name else None
    await state.db.conn.execute("""
        INSERT INTO contacts (user_id, nickname, last_seen) VALUES (?, ?, ?) 
        ON CONFLICT(user_id) DO UPDATE SET nickname=excluded.nickname
    """, (data.target_id, enc_name, datetime.now().isoformat()))
    await state.db.conn.commit()
    return {"status": "ok"}

@router.post("/api/read_chat")
async def mark_chat_as_read(data: ReadChatData):
    if not state.db: raise HTTPException(400)
    await state.db.conn.execute("UPDATE messages SET is_read = 1 WHERE chat_id = ? AND is_outgoing = 0", (data.chat_id,))
    await state.db.conn.commit()
    return {"status": "ok"}