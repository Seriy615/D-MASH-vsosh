import os
from contextlib import asynccontextmanager
from typing import Optional, Set
import asyncio
# Убираем импорт FastAPI

from database import DatabaseManager
from network import P2PNode
from tact import TactEngine

# --- D-MASH CONFIGURATION ---
TACT_INTERVAL = 1.5
PACKET_SIZE = 4096
P2P_PORT = int(os.getenv("P2P_PORT", 9000))

class AppState:
    node: Optional[P2PNode] = None
    tact: Optional[TactEngine] = None
    db: Optional[DatabaseManager] = None
    user_id: str = ""
    is_logged_in: bool = False
    background_tasks: Set[asyncio.Task] = set()

state = AppState()

@asynccontextmanager
async def lifespan(app): # FastAPI передаст сюда 'app', но мы его не используем
    bootstrap_db = DatabaseManager("bootstrap_peers.db")
    await bootstrap_db.connect()

    state.node = P2PNode(bootstrap_db, "daemon_node_idle")
    state.tact = TactEngine(bootstrap_db, state.node, TACT_INTERVAL, PACKET_SIZE)
    
    t1 = asyncio.create_task(state.node.start_server(P2P_PORT))
    t2 = asyncio.create_task(state.tact.start())
    state.background_tasks.update([t1, t2])
    t1.add_done_callback(state.background_tasks.discard)
    t2.add_done_callback(state.background_tasks.discard)
    
    yield
    
    for task in state.background_tasks: task.cancel()
    if state.db: await state.db.close()
    await bootstrap_db.close()