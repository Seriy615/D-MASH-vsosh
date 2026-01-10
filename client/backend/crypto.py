import time
import json
import base64
import os
import hashlib
from typing import Optional

import nacl.utils
import nacl.secret
import nacl.pwhash
from nacl.public import PrivateKey, PublicKey, Box, SealedBox
from nacl.signing import SigningKey, VerifyKey
from nacl.encoding import HexEncoder, Base64Encoder
import blake3

MAX_MESSAGE_AGE = 300 

class CryptoManager:
    def __init__(self):
        self.signing_key: Optional[SigningKey] = None 
        self.verify_key: Optional[VerifyKey] = None   
        self.private_key: Optional[PrivateKey] = None 
        self.public_key: Optional[PublicKey] = None   
        self.sym_key: Optional[bytes] = None          
        self.my_id: str = ""                          

    def derive_keys_from_password(self, username: str, password: str):
        """Генерация всех ключей из пары логин/пароль"""
        salt = hashlib.sha256(username.encode()).digest()[:16]
        
        kdf = nacl.pwhash.argon2id.kdf(
            nacl.secret.SecretBox.KEY_SIZE, password.encode(), salt,
            opslimit=nacl.pwhash.argon2id.OPSLIMIT_SENSITIVE,
            memlimit=nacl.pwhash.argon2id.MEMLIMIT_SENSITIVE
        )
        # Ключи для подписи (Ed25519)
        self.signing_key = SigningKey(kdf)
        self.verify_key = self.signing_key.verify_key
        
        # Ключи для шифрования (Curve25519)
        self.private_key = self.signing_key.to_curve25519_private_key()
        self.public_key = self.verify_key.to_curve25519_public_key()
        
        # ID пользователя - это Hex его публичного ключа подписи
        self.my_id = self.verify_key.encode(encoder=HexEncoder).decode()
        
        # Симметричный ключ для БД
        db_salt = hashlib.sha256((username + "_db_secure").encode()).digest()[:16]
        self.sym_key = nacl.pwhash.argon2id.kdf(
            nacl.secret.SecretBox.KEY_SIZE, password.encode(), db_salt,
            opslimit=nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE,
            memlimit=nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE
        )

    # --- ROUTING & IDENTITY (Blake3) ---
    
    def get_route_id(self, sender_pub_hex: str, receiver_pub_hex: str) -> str:
        """ID маршрута = blake3(A + B). Конкатенация строк."""
        combined = sender_pub_hex + receiver_pub_hex
        return blake3.blake3(combined.encode()).hexdigest()

    def get_target_hash(self, pub_key_hex: str) -> str:
        """Хеш цели = blake3(B)"""
        return blake3.blake3(pub_key_hex.encode()).hexdigest()

    # --- SIGNATURES (Ed25519) ---

    def sign_data(self, data_str: str) -> str:
        """Подписывает строку и возвращает подпись в Base64"""
        signed = self.signing_key.sign(data_str.encode('utf-8'))
        return base64.b64encode(signed.signature).decode('utf-8')

    def verify_sig(self, pub_key_hex: str, data_str: str, sig_b64: str) -> bool:
        """Проверяет подпись данных"""
        try:
            verify_key = VerifyKey(pub_key_hex, encoder=HexEncoder)
            sig_bytes = base64.b64decode(sig_b64)
            verify_key.verify(data_str.encode('utf-8'), sig_bytes)
            return True
        except Exception:
            return False

    # --- E2EE (XSalsa20-Poly1305 + Ed25519 Signature) ---

    def encrypt_message(self, target_pub_key_hex: str, message_text: str) -> str:
        try:
            recipient_verify_key = VerifyKey(target_pub_key_hex, encoder=HexEncoder)
            recipient_pub_key = recipient_verify_key.to_curve25519_public_key()
        except Exception:
            raise ValueError("Invalid target public key")

        timestamp = time.time()
        # Данные для подписи: текст + время + мой ID
        sig_content = f"{message_text}{timestamp}{self.my_id}"
        signature = self.sign_data(sig_content)

        payload = {
            "txt": message_text,
            "ts": timestamp,
            "sid": self.my_id,   # Мой ID (отправитель)
            "sig": signature,    # Моя подпись
            "rnd": base64.b64encode(os.urandom(16)).decode()
        }
        
        payload_bytes = json.dumps(payload).encode('utf-8')
        box = Box(self.private_key, recipient_pub_key)
        encrypted = box.encrypt(payload_bytes)
        return base64.b64encode(encrypted).decode('utf-8')

    def decrypt_message(self, sender_pub_key_hex: str, encrypted_b64: str) -> str:
        """Расшифровывает и ОБЯЗАТЕЛЬНО проверяет подпись автора"""
        try:
            sender_verify_key = VerifyKey(sender_pub_key_hex, encoder=HexEncoder)
            sender_pub_key_curve = sender_verify_key.to_curve25519_public_key()
            
            box = Box(self.private_key, sender_pub_key_curve)
            encrypted_bytes = base64.b64decode(encrypted_b64)
            plaintext_bytes = box.decrypt(encrypted_bytes)
            
            payload = json.loads(plaintext_bytes.decode('utf-8'))
            
            # 1. Проверка времени (защита от Replay-атак)
            if time.time() - payload.get("ts", 0) > MAX_MESSAGE_AGE:
                return "[ERROR: Message expired]"
            
            # 2. Проверка соответствия отправителя
            if payload.get("sid") != sender_pub_key_hex:
                return "[ERROR: Sender ID mismatch]"

            # 3. Проверка цифровой подписи
            sig_content = f"{payload['txt']}{payload['ts']}{payload['sid']}"
            if not self.verify_sig(sender_pub_key_hex, sig_content, payload['sig']):
                return "[ERROR: Invalid Signature]"

            return payload.get("txt", "")
        except Exception as e:
            return f"[ERROR: Decryption Failed]"

    # --- PROBE ENCRYPTION (SealedBox) ---

    def encrypt_for_probe(self, target_pub_key_hex: str, data_str: str) -> str:
        try:
            recipient_verify_key = VerifyKey(target_pub_key_hex, encoder=HexEncoder)
            recipient_pub_key = recipient_verify_key.to_curve25519_public_key()
            box = SealedBox(recipient_pub_key)
            encrypted = box.encrypt(data_str.encode('utf-8'))
            return base64.b64encode(encrypted).decode('utf-8')
        except Exception:
            return ""

    def decrypt_from_probe(self, encrypted_b64: str) -> str:
        try:
            box = SealedBox(self.private_key)
            encrypted_bytes = base64.b64decode(encrypted_b64)
            plaintext = box.decrypt(encrypted_bytes)
            return plaintext.decode('utf-8')
        except Exception:
            return ""

    # --- DB Encryption (SecretBox) ---
    def encrypt_db_field(self, data: str) -> str:
        if not data: return ""
        box = nacl.secret.SecretBox(self.sym_key)
        encrypted = box.encrypt(data.encode('utf-8'))
        return base64.b64encode(encrypted).decode('utf-8')

    def decrypt_db_field(self, data_b64: str) -> str:
        if not data_b64: return ""
        try:
            box = nacl.secret.SecretBox(self.sym_key)
            encrypted = base64.b64decode(data_b64)
            plaintext = box.decrypt(encrypted)
            return plaintext.decode('utf-8')
        except:
            return "[DB DECRYPT FAIL]"