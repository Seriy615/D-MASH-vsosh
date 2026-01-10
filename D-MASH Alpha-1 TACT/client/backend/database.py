import aiosqlite
from datetime import datetime

class DatabaseManager:
    """
    Менеджер локальной SQLite базы данных.
    У каждого пользователя (ID) своя база данных.
    """
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self._init_tables()

    async def _init_tables(self):
        await self.conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, chat_id TEXT, sender_id TEXT, content TEXT, timestamp TEXT, is_outgoing INTEGER, is_read INTEGER DEFAULT 0)")
        await self.conn.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, packet_id TEXT, target_id TEXT, packet_json TEXT, exclude_peer TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await self.conn.execute("CREATE TABLE IF NOT EXISTS peers (user_id TEXT PRIMARY KEY, address TEXT, nickname TEXT, is_contact INTEGER DEFAULT 0, last_seen TEXT)")
        await self.conn.execute("CREATE TABLE IF NOT EXISTS seen_packets (packet_id TEXT PRIMARY KEY, received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def mark_packet_seen(self, packet_id: str) -> bool:
        try:
            await self.conn.execute("INSERT INTO seen_packets (packet_id) VALUES (?)", (packet_id,))
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False