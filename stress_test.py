import os
import time
import json
import requests
import subprocess
import sys
import random

# КОНФИГУРАЦИЯ
NUM_NODES = 30
EXTRA_LINKS = 17
BASE_PORT = 8000
COMPOSE_FILE = "client/stress-test-compose.yml"

def generate_compose():
    compose_data = {"services": {}}
    for i in range(1, NUM_NODES + 1):
        compose_data["services"][f"node{i}"] = {
            "build": {"context": ".", "dockerfile": "docker/messenger.Dockerfile"},
            "ports": [f"{BASE_PORT + i}:8000", f"{9000 + i}:9000"],
            "environment": ["P2P_PORT=9000"],
            "volumes": ["./backend:/app/backend", "./frontend:/app/backend/frontend"]
        }
    with open(COMPOSE_FILE, "w") as f: json.dump(compose_data, f, indent=2)
    print(f"✅ Generated {COMPOSE_FILE} with {NUM_NODES} nodes.")

def run_command(cmd, ignore_errors=False):
    print(f"🚀 Running: {cmd}")
    try: subprocess.run(cmd, shell=True, check=True)
    except subprocess.CalledProcessError:
        if not ignore_errors: raise
        print("   (Command failed, but ignoring...)")

def api_call(node_idx, method, endpoint, data=None):
    url = f"https://localhost:{BASE_PORT + node_idx}{endpoint}"
    try:
        if method == "POST": r = requests.post(url, json=data, verify=False, timeout=4)
        else: r = requests.get(url, verify=False, timeout=4)
        return r.json()
    except: return None

def track_packet(packet_id, target_node_idx, duration=12, packet_type="PACKET"):
    print(f"\n🛰️ TRACKING {packet_type} {packet_id[:12]}...")
    seen_nodes = set()
    for t in range(duration):
        line = f"T+{t}s: "
        target_seen = False
        for i in range(1, NUM_NODES + 1):
            res = api_call(i, "GET", f"/api/debug/packet/{packet_id}")
            if res and res.get("seen"):
                marker = "█"
                seen_nodes.add(i)
                if i == target_node_idx: target_seen = True
            else: marker = "."
            line += f"[{i}:{marker}] "
        print(line.strip())
        if target_seen:
            print(f"✅ {packet_type} reached target Node {target_node_idx}!")
            break
        time.sleep(1)
    print(f"🏁 {packet_type} touched {len(seen_nodes)}/{NUM_NODES} nodes.")
    return target_seen

def dump_routing_tables(sender_idx, receiver_idx, users):
    print("\n" + "-"*20 + " ROUTING TABLE DUMP " + "-"*20)
    crypto_res = api_call(sender_idx, "POST", "/api/debug/get_route_ids", {
        "sender_id": users[sender_idx],
        "receiver_id": users[receiver_idx]
    })
    if not crypto_res: return
    
    route_id_fwd = crypto_res['route_fwd']
    route_id_bwd = crypto_res['route_bwd']
    
    print(f"   Route A->B (FWD): {route_id_fwd[:8]}...")
    print(f"   Route B->A (BWD): {route_id_bwd[:8]}...")

    for i in range(1, NUM_NODES + 1):
        routes = api_call(i, "GET", "/api/debug/routes")
        if routes:
            # В Beta-2 ноды хранят маршруты для обоих направлений
            relevant = [r for r in routes if r['route_id'] in [route_id_fwd, route_id_bwd]]
            if relevant:
                print(f"  Node {i}:")
                for r in sorted(relevant, key=lambda x: x['metric']):
                    dir_label = "FWD" if r['route_id'] == route_id_fwd else "BWD"
                    print(f"    {dir_label} via {r['next_hop_id'][:8]} (metric {r['metric']})")
    print("-" * 62)

def run_test(test_num, users):
    sender_idx, receiver_idx = random.sample(range(1, NUM_NODES + 1), 2)
    
    print("\n" + "="*50)
    print(f"=== TEST #{test_num}: Node {sender_idx} -> Node {receiver_idx} ===")
    print("="*50)

    sender_id = users[sender_idx]
    receiver_id = users[receiver_idx]
    print(f"   Sender:   Node {sender_idx} ({sender_id[:12]}...)")
    print(f"   Receiver: Node {receiver_idx} ({receiver_id[:12]}...)")

    # --- Фаза 1: Первичная Проба (Алиса -> Боб) ---
    print(f"\n📨 Phase 1: Initiating Symmetric Discovery...")
    res = api_call(sender_idx, "POST", "/api/send", {"target_id": receiver_id, "text": f"Handshake from {sender_idx}"})
    
    if not (res and "packet_id" in res and res.get("packet_type") == "PROBE"):
        print("❌ FAILED to initiate PROBE.")
        return

    probe_id = res["packet_id"]
    track_packet(probe_id, receiver_idx, packet_type="PROBE_INIT")

        # --- Фаза 2: Ожидание ответной пробы ---
    print("\n⏳ Phase 2: Waiting for Symmetric Response (15s)...")
    time.sleep(5)
    
    response_probe_id = None
    # Ищем любую новую пробу в outbox получателя
    for _ in range(10):
        outbox = api_call(receiver_idx, "GET", "/api/debug/outbox")
        if outbox:
            for item in outbox:
                pkt = json.loads(item['packet_json'])
                if pkt.get('type') == 'PROBE':
                    response_probe_id = item['packet_id']
                    break
        if response_probe_id: break
        time.sleep(1)

    if response_probe_id:
        track_packet(response_probe_id, sender_idx, packet_type="PROBE_RESP")
    
    print("\n⏳ Stabilizing routes (5s)...")
    time.sleep(5)

    # --- Фаза 3: Передача DATA (Туннель) ---
    print(f"\n📨 Phase 3: Sending DATA through established tunnel...")
    time.sleep(5) # Даем время второй волне пробы дойти
    
    res2 = api_call(sender_idx, "POST", "/api/send", {"target_id": receiver_id, "text": f"Secure Data {test_num}"})
    
    if not (res2 and "packet_id" in res2):
        print("❌ FAILED to send DATA packet.")
        return
        
    data_id = res2["packet_id"]
    packet_type = res2["packet_type"]
    print(f"📦 Sent Packet ID: {data_id} (Type: {packet_type})")

    if packet_type == "DATA":
        print("✅ SUCCESS! System switched to efficient DATA routing.")
        track_packet(data_id, receiver_idx, packet_type="DATA_TUNNEL")
    else:
        print("⚠️ Warning: System still using PROBE. Route not fully established.")
        track_packet(data_id, receiver_idx, packet_type="PROBE_RETRY")

    # --- Фаза 4: Проверка доставки ---
    time.sleep(2)
    msgs = api_call(receiver_idx, "GET", f"/api/messages/{sender_id}")
    if msgs:
        print(f"\n🎉 TEST COMPLETED: Node {receiver_idx} received {len(msgs)} messages.")
    else:
        print(f"\n❌ TEST FAILED: No messages delivered.")

    dump_routing_tables(sender_idx, receiver_idx, users)

def main():
    generate_compose()
    run_command(f"docker-compose -f {COMPOSE_FILE} down --remove-orphans", ignore_errors=True)
    os.system("rm -f client/*.db")
    run_command(f"docker-compose -f {COMPOSE_FILE} up -d --build")
    
    print(f"⏳ Waiting for {NUM_NODES} nodes to initialize (15s)...")
    time.sleep(15)

    users = {} 
    print("\n🔑 LOGGING IN NODES...")
    for i in range(1, NUM_NODES + 1):
        username, password = f"user{i}", "1"
        res = api_call(i, "POST", "/api/login", {"username": username, "password": password})
        if res and "user_id" in res:
            users[i] = res["user_id"]
        else:
            print(f"   ❌ Node {i} login failed!")
    
    if len(users) < NUM_NODES:
        print("Critical failure: Not all nodes are online. Aborting.")
        sys.exit(1)
    
    print("\n🕸️ BUILDING MESH TOPOLOGY...")
    for i in range(1, NUM_NODES):
        api_call(i, "POST", "/api/connect", {"address": f"node{i+1}:9000"})
    for _ in range(EXTRA_LINKS):
        a, b = random.sample(range(1, NUM_NODES + 1), 2)
        if abs(a - b) > 1: # Не дублируем цепочку
            api_call(a, "POST", "/api/connect", {"address": f"node{b}:9000"})
    
    print("⏳ Stabilizing (3s)...")
    time.sleep(3)

    for i in range(3):
        run_test(i + 1, users)

if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    main()