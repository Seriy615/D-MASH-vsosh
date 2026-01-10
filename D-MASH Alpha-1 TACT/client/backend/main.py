import uvicorn
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Импортируем lifespan из core и router из api
from core import lifespan
from api import router

# --- СОЗДАЕМ И СОБИРАЕМ ПРИЛОЖЕНИЕ ЗДЕСЬ ---
app = FastAPI(lifespan=lifespan)

# 1. Подключаем Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Подключаем все API-роуты
app.include_router(router)

# 3. В самом конце монтируем статику
frontend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")


# --- ТОЧКА ВХОДА ---
if __name__ == "__main__":
    uvicorn.run(
        "main:app", # <-- Запускаем 'app' из ЭТОГО файла
        host="0.0.0.0", 
        port=8000, 
        reload=False,
        ssl_keyfile="/app/certs/key.pem", 
        ssl_certfile="/app/certs/cert.pem"
    )