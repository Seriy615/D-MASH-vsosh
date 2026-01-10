import asyncio
import json
from datetime import datetime
from websockets.server import serve
from websockets.client import connect as ws_connect
from database import DatabaseManager

class P2PNode:
    """
    Сетевой демон. Работает постоянно.
    Принимает соединения, держит сокеты, маршрутизирует пакеты.
    """
    def __init__(self, db: DatabaseManager, my_id: str):
        self.db = db
        self.my_id = my_id
        self.active_connections = {} # user_id -> websocket

    async def start_server(self, port: int):
        print(f"🌐 [P2P] Listening on port {port}")
        async with serve(self._handle_incoming, "0.0.0.0", port):
            await asyncio.Future()

    async def connect_to(self, address: str):
        try:
            uri = f"ws://{address}"
            ws = await ws_connect(uri, open_timeout=5)
            await ws.send(self.my_id)
            peer_id = await ws.recv()
            
            if peer_id == self.my_id:
                print("⚠️ [P2P] Self-connection attempt blocked.")
                await ws.close()
                return False

            self.active_connections[peer_id] = ws
            print(f"✅ [P2P] Connected to neighbor {peer_id[:8]}")
            
            await self.db.conn.execute("INSERT OR IGNORE INTO peers (user_id, address, last_seen) VALUES (?, ?, ?)", (peer_id, address, datetime.now().isoformat()))
            await self.db.conn.commit()
            
            asyncio.create_task(self._listen_socket(ws, peer_id))
            return True
        except Exception as e:
            print(f"❌ [P2P] Connection failed: {e}")
            return False

    async def _handle_incoming(self, websocket):
        try:
            peer_id = await websocket.recv()
            if peer_id == self.my_id:
                await websocket.close()
                return
            await websocket.send(self.my_id)
            self.active_connections[peer_id] = websocket
            print(f"🔗 [P2P] Neighbor connected: {peer_id[:8]}")
            await self.db.conn.execute("INSERT OR IGNORE INTO peers (user_id, last_seen) VALUES (?, ?)", (peer_id, datetime.now().isoformat()))
            await self.db.conn.commit()
            await self._listen_socket(websocket, peer_id)
        except Exception:
            pass

    async def _listen_socket(self, websocket, peer_id):
        try:
            async for message in websocket:
                await self._process_envelope(message, from_peer=peer_id)
        except:
            if peer_id in self.active_connections:
                del self.active_connections[peer_id]
            print(f"Neighbor {peer_id[:8]} disconnected")

    async def _process_envelope(self, envelope_json: str, from_peer: str):
        """
        ГЛАВНАЯ ЛОГИКА МАРШРУТИЗАЦИИ (Сортировочный центр)
        """
        try:
            envelope = json.loads(envelope_json)
            
            # 1. Игнорируем мусорный трафик
            if envelope.get("t") == "DUMMY":
                return

            # 2. Обрабатываем реальный пакет
            if envelope.get("t") == "REAL":
                inner_json = envelope.get("d")
                packet = json.loads(inner_json)
                
                pkt_id = packet.get("id")
                target = packet.get("to")
                sender = packet.get("from")
                ttl = packet.get("ttl", 0)

                # 3. Дедупликация (защита от зацикливания)
                if not await self.db.mark_packet_seen(pkt_id):
                    return

                # 4. Проверяем: Это МНЕ?
                if target == self.my_id:
                    print(f"📨 [MAIL] Received message from {sender[:8]}")
                    content = packet.get("content")
                    
                    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
                    # Используем UPSERT, чтобы гарантированно пометить отправителя как контакт
                    await self.db.conn.execute("""
                        INSERT INTO peers (user_id, is_contact, last_seen)
                        VALUES (?, 1, ?)
                        ON CONFLICT(user_id) DO UPDATE SET 
                            is_contact = 1, 
                            last_seen = excluded.last_seen
                    """, (sender, datetime.now().isoformat()))

                    # Сохраняем сообщение как новое (is_read=0)
                    await self.db.conn.execute("""
                        INSERT INTO messages (chat_id, sender_id, content, timestamp, is_outgoing, is_read) 
                        VALUES (?, ?, ?, ?, 0, 0)
                    """, (sender, sender, content, datetime.now().isoformat()))
                    await self.db.conn.commit()
                    return

                # 5. Это НЕ мне -> РЕТРАНСЛЯЦИЯ (Relay)
                if ttl > 0:
                    print(f"🔀 [ROUTER] Relaying {pkt_id[:8]} for {target[:8]} (TTL: {ttl})")
                    packet["ttl"] = ttl - 1
                    new_payload = json.dumps(packet)
                    
                    # Кладем пакет в Outbox. Указываем exclude_peer, чтобы не слать обратно.
                    await self.db.conn.execute("""
                        INSERT INTO outbox (packet_id, target_id, packet_json, exclude_peer) 
                        VALUES (?, ?, ?, ?)
                    """, (pkt_id, target, new_payload, from_peer))
                    await self.db.conn.commit()
                else:
                    print(f"💀 [ROUTER] Packet {pkt_id[:8]} died (TTL expired)")

        except Exception as e:
            print(f"❌ Packet error: {e}")
        try:
            envelope = json.loads(envelope_json)
            if envelope.get("t") == "DUMMY": return

            if envelope.get("t") == "REAL":
                inner_json = envelope.get("d")
                packet = json.loads(inner_json)
                pkt_id, target, sender, ttl = packet.get("id"), packet.get("to"), packet.get("from"), packet.get("ttl", 0)

                if not await self.db.mark_packet_seen(pkt_id): return

                if target == self.my_id:
                    print(f"📨 [MAIL] Received message from {sender[:8]}")
                    content = packet.get("content")
                    await self.db.conn.execute("INSERT OR IGNORE INTO peers (user_id, is_contact, last_seen) VALUES (?, 1, ?)", (sender, datetime.now().isoformat()))
                    await self.db.conn.execute("INSERT INTO messages (chat_id, sender_id, content, timestamp, is_outgoing, is_read) VALUES (?, ?, ?, ?, 0, 0)", (sender, sender, content, datetime.now().isoformat()))
                    await self.db.conn.commit()
                elif ttl > 0:
                    print(f"🔀 [ROUTER] Relaying {pkt_id[:8]} for {target[:8]} (TTL: {ttl})")
                    packet["ttl"] = ttl - 1
                    await self.db.conn.execute("INSERT INTO outbox (packet_id, target_id, packet_json, exclude_peer) VALUES (?, ?, ?, ?)", (pkt_id, target, json.dumps(packet), from_peer))
                    await self.db.conn.commit()
        except Exception as e:
            print(f"❌ Packet error: {e}")