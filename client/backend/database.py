import aiosqlite
import time
from datetime import datetime

class DatabaseManager:
    """
    Менеджер локальной SQLite базы данных для архитектуры Beta-2.
    Оперирует маршрутами на основе хешей и дедупликацией пакетов.
    """
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None
        self.crypto = None

    def set_crypto(self, crypto_manager):
        self.crypto = crypto_manager

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self._init_tables()

    async def _init_tables(self):
        # ТАБЛИЦЫ ПОЛЬЗОВАТЕЛЯ (User DB)
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                packet_id TEXT UNIQUE, 
                chat_id TEXT,
                sender_id TEXT,
                content TEXT, 
                timestamp TEXT,
                is_outgoing INTEGER,
                is_read INTEGER DEFAULT 0
            )
        """)
        
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                user_id TEXT PRIMARY KEY,
                nickname TEXT,
                last_seen TEXT
            )
        """)

        # --- ТАБЛИЦЫ ДЕМОНА (System DB) ---
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS neighbors (
                user_id TEXT PRIMARY KEY,
                address TEXT,
                last_seen TEXT
            )
        """)
        
        # Очередь исходящих пакетов
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                packet_id TEXT,
                next_hop_id TEXT, 
                packet_json TEXT,
                exclude_peer TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Дедупликация (храним RouteID поисков и PacketID данных)
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_packets (
                packet_id TEXT PRIMARY KEY,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await self.conn.execute("CREATE TABLE IF NOT EXISTS local_users (user_id TEXT PRIMARY KEY)")

        # Хранилище для офлайн-доставки
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS offline_mailbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id TEXT,
                packet_json TEXT,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # ТАБЛИЦА МАРШРУТИЗАЦИИ (Beta-2)
        # route_id - хеш, определяющий направление (A->B или B->A)
        # next_hop_id - сосед, через которого лежит путь
        # is_local - флаг, если маршрут ведет к локальному пользователю на этой ноде
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS routing_table (
                route_id TEXT,
                next_hop_id TEXT,
                metric INTEGER,
                is_local INTEGER DEFAULT 0,
                remote_user_id TEXT,
                expires_at TIMESTAMP,
                PRIMARY KEY (route_id, next_hop_id)
            )
        """)
        
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    # --- МЕТОДЫ СИСТЕМЫ ---

    async def mark_packet_seen(self, packet_id: str) -> bool:
        """Регистрирует пакет. Возвращает True если пакет новый, False если дубль."""
        try:
            await self.conn.execute("INSERT INTO seen_packets (packet_id) VALUES (?)", (packet_id,))
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def register_local_user(self, user_id: str):
        await self.conn.execute("INSERT OR IGNORE INTO local_users (user_id) VALUES (?)", (user_id,))
        await self.conn.commit()

    async def is_local_user(self, user_id: str) -> bool:
        async with self.conn.execute("SELECT 1 FROM local_users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

    async def save_to_mailbox(self, target_id: str, packet_json: str):
        await self.conn.execute("INSERT INTO offline_mailbox (target_id, packet_json) VALUES (?, ?)", (target_id, packet_json))
        await self.conn.commit()

    async def fetch_mailbox(self, user_id: str):
        async with self.conn.execute("SELECT id, packet_json FROM offline_mailbox WHERE target_id = ?", (user_id,)) as cursor:
            rows = await cursor.fetchall()
        if rows:
            ids = [row['id'] for row in rows]
            await self.conn.execute(f"DELETE FROM offline_mailbox WHERE id IN ({','.join(['?']*len(ids))})", ids)
            await self.conn.commit()
        return [row['packet_json'] for row in rows]

    # --- МЕТОДЫ МАРШРУТИЗАЦИИ (Beta-2) ---

    async def add_route(self, route_id: str, next_hop_id: str, metric: int, is_local: int = 0, remote_user_id: str = None):
        """
        Добавляет или обновляет маршрут. 
        В Beta-2 маршруты строятся автоматически при прохождении PROBE.
        """
        # TTL маршрута 30 минут
        expires = time.time() + 1800 
        await self.conn.execute("""
            INSERT OR REPLACE INTO routing_table (route_id, next_hop_id, metric, is_local, remote_user_id, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (route_id, next_hop_id, metric, is_local, remote_user_id, expires))
        await self.conn.commit()

    async def get_best_route(self, route_id: str):
        """Возвращает лучший по метрике активный путь для route_id."""
        async with self.conn.execute("""
            SELECT next_hop_id, is_local, remote_user_id, metric FROM routing_table 
            WHERE route_id = ? AND expires_at > ? 
            ORDER BY metric ASC LIMIT 1
        """, (route_id, time.time())) as cursor:
            return await cursor.fetchone()