import os
import hashlib
import json
import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Optional
from nacl.public import PrivateKey
from nacl.encoding import Base64Encoder

# Импортируем только state из core
from core import state
from database import DatabaseManager

# Создаем APIRouter
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

# --- API ROUTES (используем @router) ---

@router.get("/")
async def root():
    return RedirectResponse(url="/auth/login.html")

@router.post("/api/login")
async def login(data: LoginData):
    combo = f"{data.username}|{data.password}"
    seed = hashlib.sha256(combo.encode()).digest()
    sk = PrivateKey(seed)
    new_user_id = hashlib.sha256(sk.public_key.encode(Base64Encoder)).hexdigest()[:16]
    
    if state.is_logged_in and state.user_id != new_user_id:
        if state.db: await state.db.close()
        state.is_logged_in = False
        print(f"🔄 Switching user to {new_user_id[:8]}")
    
    if not state.is_logged_in:
        db_name = f"node_{new_user_id}.db"
        state.user_id = new_user_id
        state.db = DatabaseManager(db_name)
        await state.db.connect()
        
        state.node.db = state.db
        state.node.my_id = new_user_id
        state.tact.db = state.db
        
        state.is_logged_in = True
    
    return {"status": "ok", "user_id": new_user_id}

@router.post("/api/logout")
async def logout():
    if state.is_logged_in:
        print(f"🔒 Logging out user {state.user_id[:8]}")
        if state.db: await state.db.close()
        state.db = None
        state.user_id = ""
        state.is_logged_in = False
        state.node.my_id = "daemon_node_idle"
    return {"status": "ok"}

@router.post("/api/connect")
async def connect_peer(data: ConnectData):
    if not state.node: raise HTTPException(400, "Node not ready")
    res = await state.node.connect_to(data.address)
    return {"success": res}

@router.post("/api/rename")
async def rename_peer(data: RenameData):
    if not state.db: raise HTTPException(400)
    sql = "INSERT INTO peers (user_id, nickname, is_contact) VALUES (?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET is_contact=1"
    params = [data.target_id, data.name]
    if data.name:
        sql += ", nickname=excluded.nickname"
    await state.db.conn.execute(sql, params)
    await state.db.conn.commit()
    return {"status": "ok"}

@router.post("/api/send")
async def send_message(data: SendData):
    if not state.db: raise HTTPException(400)
    
    await state.db.conn.execute("INSERT INTO messages (chat_id, sender_id, content, timestamp, is_outgoing, is_read) VALUES (?, ?, ?, ?, 1, 1)", (data.target_id, state.user_id, data.text, datetime.now().isoformat()))
    await state.db.conn.execute("INSERT OR IGNORE INTO peers (user_id, is_contact) VALUES (?, 1)", (data.target_id,))

    pkt_id = str(uuid.uuid4())
    packet = {"id": pkt_id, "to": data.target_id, "from": state.user_id, "content": data.text, "ttl": 20}
    packet_str = json.dumps(packet)
    
    await state.db.conn.execute("INSERT INTO outbox (packet_id, target_id, packet_json) VALUES (?, ?, ?)", (pkt_id, data.target_id, packet_str))
    await state.db.conn.commit()
    return {"status": "queued"}

@router.post("/api/read_chat")
async def mark_chat_as_read(data: ReadChatData):
    if not state.db: raise HTTPException(400)
    await state.db.conn.execute("UPDATE messages SET is_read = 1 WHERE chat_id = ? AND is_outgoing = 0", (data.chat_id,))
    await state.db.conn.commit()
    return {"status": "ok"}

@router.get("/api/state")
async def get_state():
    if not state.node: return {"status": "offline"}
    return {"user_id": state.user_id, "peers": list(state.node.active_connections.keys())}

@router.get("/api/peers")
async def get_contacts():
    if not state.db: return []
    async with state.db.conn.execute("SELECT p.user_id, p.nickname, (SELECT COUNT(id) FROM messages WHERE chat_id = p.user_id AND is_read = 0 AND is_outgoing = 0) as unread_count FROM peers p WHERE p.is_contact = 1") as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

@router.get("/api/messages/{chat_id}")
async def get_chat_history(chat_id: str):
    if not state.db: return []
    
    # Шаг 1: Получаем все сообщения для этого чата
    async with state.db.conn.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY timestamp ASC", (chat_id,)) as cursor:
        rows = await cursor.fetchall()
    
    # Шаг 2: СРАЗУ ЖЕ после получения, помечаем их как прочитанные
    # Это атомарно решает проблему "фантомных" бейджей
    await state.db.conn.execute("UPDATE messages SET is_read = 1 WHERE chat_id = ? AND is_outgoing = 0", (chat_id,))
    await state.db.conn.commit()
    
    # Шаг 3: Возвращаем сообщения, которые мы только что получили
    return [dict(row) for row in rows]