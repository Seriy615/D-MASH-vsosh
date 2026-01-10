import asyncio
import json
import uuid
import time
from datetime import datetime
from websockets.server import serve
from websockets.client import connect as ws_connect
from database import DatabaseManager

class P2PNode:
    def __init__(self, system_db: DatabaseManager):
        self.system_db = system_db
        self.active_connections = {} 
        self.active_user_id = None
        self.active_user_db = None
        self.active_crypto = None

    def set_active_user(self, user_id, user_db, crypto):
        self.active_user_id = user_id
        self.active_user_db = user_db
        self.active_crypto = crypto

    def remove_active_user(self):
        self.active_user_id = None
        self.active_user_db = None
        self.active_crypto = None

    async def start_server(self, port: int):
        print(f"🌐 [P2P] Daemon listening on port {port}")
        async with serve(self._handle_incoming, "0.0.0.0", port):
            await asyncio.Future()

    async def connect_to(self, address: str):
        try:
            uri = f"ws://{address}"
            ws = await ws_connect(uri, open_timeout=5)
            my_id_handshake = self.active_user_id if self.active_user_id else "daemon_node"
            await ws.send(my_id_handshake)
            peer_id = await ws.recv()
            
            if peer_id == my_id_handshake and peer_id != "daemon_node":
                 await ws.close()
                 return False

            self.active_connections[peer_id] = ws
            print(f"✅ [P2P] Connected to neighbor {peer_id[:8]}")
            
            await self.system_db.conn.execute("""
                INSERT INTO neighbors (user_id, address, last_seen) 
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen
            """, (peer_id, address, datetime.now().isoformat()))
            await self.system_db.conn.commit()
            
            asyncio.create_task(self._listen_socket(ws, peer_id))
            return True
        except Exception as e:
            print(f"❌ [P2P] Connection failed: {e}")
            return False

    async def _handle_incoming(self, websocket):
        try:
            peer_id = await websocket.recv()
            my_id_handshake = self.active_user_id if self.active_user_id else "daemon_node"
            await websocket.send(my_id_handshake)
            self.active_connections[peer_id] = websocket
            print(f"🔗 [P2P] Neighbor connected: {peer_id[:8]}")
            await self.system_db.conn.execute("""
                INSERT INTO neighbors (user_id, address, last_seen) 
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen
            """, (peer_id, "incoming", datetime.now().isoformat()))
            await self.system_db.conn.commit()
            await self._listen_socket(websocket, peer_id)
        except Exception: pass

    async def _listen_socket(self, websocket, peer_id):
        try:
            async for message in websocket:
                await self._process_envelope(message, from_peer=peer_id)
        except:
            if peer_id in self.active_connections: del self.active_connections[peer_id]

    async def _process_envelope(self, envelope_json: str, from_peer: str):
        try:
            envelope = json.loads(envelope_json)
            if envelope.get("t") == "DUMMY": return

            if envelope.get("t") == "REAL":
                inner_json = envelope.get("d")
                packet = json.loads(inner_json)
                pkt_type = packet.get("type")
                pkt_id = packet.get("id")

                # В Beta-2 мы регистрируем ВСЕ пакеты (PROBE и DATA) для трекера
                is_new = await self.system_db.mark_packet_seen(pkt_id)

                if pkt_type == "PROBE":
                    # Для PROBE дедупликация внутри метода (нужно записать путь до отсева)
                    await self._handle_probe(packet, from_peer, is_new)
                elif pkt_type == "DATA":
                    # Для DATA обрабатываем только если видим впервые
                    if is_new:
                        await self._handle_data(packet, from_peer)
        except Exception as e:
            print(f"❌ Packet error: {e}")

    async def _handle_probe(self, packet, from_peer, is_new_probe):
        probe_id = packet['id']
        route_id = packet['route_id']   
        rev_id = packet['rev_id']       
        target_hash = packet['target_hash']
        metric = packet['metric']

        # 1. ЗАПИСЬ МАРШРУТА (Паутина строится здесь)
        # Мы записываем rev_id, потому что этот путь ведет НАЗАД к источнику пробы
        # ВАЖНО: Не перезаписываем LOCAL маршрут удаленным!
        existing_rev = await self.system_db.get_best_route(rev_id)
        if not (existing_rev and existing_rev['is_local']):
            await self.system_db.add_route(rev_id, from_peer, metric + 1)

        # 2. ПРОВЕРКА ЦЕЛИ
        if self.active_user_id and self.active_crypto:
            if self.active_crypto.get_target_hash(self.active_user_id) == target_hash:
                # МЫ - ЦЕЛЬ (Боб). Обрабатываем только один раз.
                if is_new_probe:
                    sender_id_json = self.active_crypto.decrypt_from_probe(packet['auth'])
                    if sender_id_json:
                        try:
                            sender_data = json.loads(sender_id_json)
                            sender_id = sender_data.get('sid')
                            
                            # Проверяем подпись (A+B).signature(A)
                            sig_data = sender_id + self.active_user_id
                            if self.active_crypto.verify_sig(sender_id, sig_data, packet['sig']):
                                print(f"🎯 [PROBE] Validated source: {sender_id[:8]}")
                                
                                # Боб метит ВХОДЯЩИЙ канал Алисы как LOCAL для себя
                                await self.system_db.add_route(route_id, "LOCAL", 0, is_local=1, remote_user_id=sender_id)

                                # Доставляем сообщение (E2EE)
                                if packet.get('content'):
                                    await self._deliver_to_active_user(packet, sender_id)
                                
                                # РАЗРЫВ ПЕТЛИ: Проверяем, не является ли rev_id уже локальным (значит мы Алиса)
                                if existing_rev and existing_rev['is_local']:
                                    return # Мы Алиса, получили ответ от Боба, цепочка замкнулась.

                                # Если мы Боб - шлем ответную пробу
                                await self._send_probe_response(sender_id)
                        except Exception as e:
                            print(f"Probe validation error: {e}")
                return 

        # 3. РЕТРАНСЛЯЦИЯ (Если пакет новый и TTL позволяет)
        if is_new_probe and packet['ttl'] > 0:
            packet['ttl'] -= 1
            packet['metric'] += 1
            await self.system_db.conn.execute("""
                INSERT INTO outbox (packet_id, next_hop_id, packet_json, exclude_peer) 
                VALUES (?, NULL, ?, ?)
            """, (probe_id, json.dumps(packet), from_peer))
            await self.system_db.conn.commit()

    async def _send_probe_response(self, requester_id):
        """Боб отправляет свою пробу Алисе в ответ"""
        print(f"🔄 [PROBE] Sending symmetric response to {requester_id[:8]}")
        
        # Для Боба: прямой канал (route_id) это B+A, обратный (rev_id) это A+B
        route_id = self.active_crypto.get_route_id(self.active_user_id, requester_id)
        rev_id = self.active_crypto.get_route_id(requester_id, self.active_user_id)
        
        signature = self.active_crypto.sign_data(self.active_user_id + requester_id)
        auth_payload = self.active_crypto.encrypt_for_probe(requester_id, json.dumps({"sid": self.active_user_id}))
        
        # Техническое сообщение о хендшейке
        e2e_content = self.active_crypto.encrypt_message(requester_id, "🤝 [System] Connection established")
        
        probe_pkt_id = str(uuid.uuid4())
        probe_packet = {
            "type": "PROBE",
            "id": probe_pkt_id,
            "route_id": route_id,
            "rev_id": rev_id,
            "target_hash": self.active_crypto.get_target_hash(requester_id),
            "metric": 0,
            "ttl": 20,
            "auth": auth_payload,
            "sig": signature,
            "content": e2e_content
        }
        
        # Боб метит СВОЙ исходящий канал как LOCAL (чтобы не отвечать самому себе)
        await self.system_db.add_route(route_id, "LOCAL", 0, is_local=1, remote_user_id=requester_id)
        await self.system_db.mark_packet_seen(probe_pkt_id)
        
        await self.system_db.conn.execute("""
            INSERT INTO outbox (packet_id, next_hop_id, packet_json, exclude_peer) 
            VALUES (?, NULL, ?, NULL)
        """, (probe_pkt_id, json.dumps(probe_packet)))
        await self.system_db.conn.commit()

    async def _handle_data(self, packet, from_peer):
        """Пересылка данных с поддержкой Multipath Failover"""
        route_id = packet.get('route_id')
        
        # Ищем ВСЕ возможные пути, отсортированные по метрике (от лучшего к худшему)
        async with self.system_db.conn.execute("""
            SELECT next_hop_id, is_local, remote_user_id FROM routing_table 
            WHERE route_id = ? AND expires_at > ? 
            ORDER BY metric ASC
        """, (route_id, time.time())) as cursor:
            routes = await cursor.fetchall()
        
        if not routes: return 

        for route in routes:
            if route['is_local']:
                if self.active_user_id:
                    await self._deliver_to_active_user(packet, route['remote_user_id'])
                return
            
            # Проверяем, активен ли этот сосед прямо сейчас
            next_hop = route['next_hop_id']
            if next_hop in self.active_connections:
                await self.system_db.conn.execute("""
                    INSERT INTO outbox (packet_id, next_hop_id, packet_json, exclude_peer) 
                    VALUES (?, ?, ?, ?)
                """, (packet['id'], next_hop, json.dumps(packet), from_peer))
                await self.system_db.conn.commit()
                return 

    async def _deliver_to_active_user(self, packet, sender_id):
        """Финальная доставка сообщения в БД пользователя с дедупликацией по packet_id"""
        try:
            decrypted_text = self.active_crypto.decrypt_message(sender_id, packet.get("content"))
            msg_uuid = packet.get('id')

            # Дедупликация в БД пользователя по packet_id (колонка UNIQUE)
            try:
                local_content = self.active_crypto.encrypt_db_field(decrypted_text)
                await self.active_user_db.conn.execute("""
                    INSERT INTO messages (packet_id, chat_id, sender_id, content, timestamp, is_outgoing, is_read) 
                    VALUES (?, ?, ?, ?, ?, 0, 0)
                """, (msg_uuid, sender_id, sender_id, local_content, datetime.now().isoformat()))
                
                await self.active_user_db.conn.execute("""
                    INSERT INTO contacts (user_id, last_seen) VALUES (?, ?) 
                    ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen
                """, (sender_id, datetime.now().isoformat()))
                
                await self.active_user_db.conn.commit()
                print(f"📨 [MAIL] Delivered from {sender_id[:8]}")
            except: 
                # Если packet_id уже есть, INSERT упадет - это и есть дедупликация
                pass 
        except Exception as e:
            print(f"Delivery error: {e}")