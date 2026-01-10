import os
from contextlib import asynccontextmanager
from typing import Optional, Set
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import DatabaseManager
from network import P2PNode
from tact import TactEngine
from crypto import CryptoManager

# --- D-MASH CONFIGURATION ---
TACT_INTERVAL = 1.5
PACKET_SIZE = 4096
P2P_PORT = int(os.getenv("P2P_PORT", 9000))

class AppState:
    node: Optional[P2PNode] = None
    tact: Optional[TactEngine] = None
    
    system_db: Optional[DatabaseManager] = None # База демона
    db: Optional[DatabaseManager] = None        # База юзера
    
    crypto: Optional[CryptoManager] = None
    user_id: str = ""
    is_logged_in: bool = False
    background_tasks: Set[asyncio.Task] = set()

state = AppState()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Запускаем Системную БД
    state.system_db = DatabaseManager("bootstrap_peers.db")
    await state.system_db.connect()

    # 2. Запускаем Демона
    # ИСПРАВЛЕНИЕ: P2PNode теперь принимает только базу данных
    state.node = P2PNode(state.system_db) 
    
    state.tact = TactEngine(state.system_db, state.node, TACT_INTERVAL, PACKET_SIZE)
    
    t1 = asyncio.create_task(state.node.start_server(P2P_PORT))
    t2 = asyncio.create_task(state.tact.start())
    state.background_tasks.update([t1, t2])
    t1.add_done_callback(state.background_tasks.discard)
    t2.add_done_callback(state.background_tasks.discard)
    
    yield
    
    for task in state.background_tasks: task.cancel()
    if state.db: await state.db.close()
    if state.system_db: await state.system_db.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from api import router as api_router
app.include_router(api_router)

frontend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")