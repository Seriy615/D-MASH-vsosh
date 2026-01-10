import asyncio
import json
import random
import string
import time
from database import DatabaseManager
from network import P2PNode

class TactEngine:
    def __init__(self, db: DatabaseManager, node: P2PNode, interval: float, packet_size: int):
        self.db = db
        self.node = node
        self.interval = interval
        self.packet_size = packet_size
        self.running = False

    async def start(self):
        self.running = True
        print(f"⏱️ [TACT] Engine started. Tick: {self.interval}s")
        while self.running:
            start_time = time.time()
            await self._tick()
            elapsed = time.time() - start_time
            sleep_time = max(0.1, self.interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def _tick(self):
        neighbors = list(self.node.active_connections.items())
        if not neighbors: return

        async with self.db.conn.execute("SELECT id, next_hop_id, packet_json, exclude_peer FROM outbox ORDER BY created_at ASC LIMIT 5") as cursor:
            rows = await cursor.fetchall()

        if not rows:
            dummy = self._create_envelope("", is_dummy=True)
            for _, ws in neighbors:
                try: await ws.send(dummy)
                except: pass
            return

        for row in rows:
            msg_id, next_hop, payload, exclude_peer = row['id'], row['next_hop_id'], row['packet_json'], row['exclude_peer']
            envelope = self._create_envelope(payload, is_dummy=False)
            if next_hop:
                ws = self.node.active_connections.get(next_hop)
                if ws:
                    try: await ws.send(envelope)
                    except: pass
            else:
                for peer_id, ws in neighbors:
                    if peer_id == exclude_peer: continue # <--- ВОТ ТУТ ЗАЩИТА
                    try: await ws.send(envelope)
                    except: pass
            await self.db.conn.execute("DELETE FROM outbox WHERE id = ?", (msg_id,))
        await self.db.conn.commit()
        
    def _create_envelope(self, payload_str: str, is_dummy: bool) -> str:
        msg_type = "DUMMY" if is_dummy else "REAL"
        envelope = { "t": msg_type, "d": payload_str, "x": "" }
        current_len = len(json.dumps(envelope).encode('utf-8'))
        padding_needed = self.packet_size - current_len
        if padding_needed > 0:
            envelope["x"] = ''.join(random.choices(string.ascii_letters + string.digits, k=padding_needed))
        return json.dumps(envelope)