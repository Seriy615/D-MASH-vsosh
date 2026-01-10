let currentChatId = null;
let peersMap = {}; 
const myId = localStorage.getItem('my_id');

async function init() {
    if (!myId) {
        window.location.href = '/auth/login.html';
        return;
    }
    // Визуально показываем короткий, но сохраняем полный в памяти
    document.getElementById('my-id').innerText = `ID: ${myId.substring(0, 16)}... (Click to Copy)`;
    
    updateState();
    setInterval(updateState, 2000);
    setInterval(refreshMessages, 1000);
}

async function logout() {
    await fetch('/api/logout', { method: 'POST' });
    localStorage.removeItem('my_id');
    window.location.href = '/auth/login.html';
}

async function updateState() {
    const resState = await fetch('/api/state').then(res => res.json()).catch(() => ({ peers: [] }));
    
    document.getElementById('statusBar').innerHTML = `
        <span>NEIGHBORS: ${resState.peers.length}</span>
        <span>ID: ${myId.substring(0,8)}</span>
        <span style="color:#0f0">TACT: ACTIVE</span>
    `;

    const resPeers = await fetch('/api/peers').then(res => res.json()).catch(() => []);
    
    const list = document.getElementById('peers');
    list.innerHTML = '';
    
    peersMap = {};
    resPeers.forEach(p => peersMap[p.user_id] = p);

    resPeers.forEach(p => {
        const div = document.createElement('div');
        div.className = 'peer-item';
        if (currentChatId === p.user_id) div.classList.add('active');
        
        const isOnline = resState.peers.includes(p.user_id);
        const statusColor = isOnline ? '#0f0' : '#555';
        const displayName = p.nickname ? p.nickname : p.user_id.substring(0, 8) + '...';
        
        const unreadCount = (p.user_id !== currentChatId && p.unread_count > 0) ? p.unread_count : 0;
        const unreadBadge = unreadCount > 0 
            ? `<span style="background:#e63946; color:#fff; border-radius:10px; padding:1px 6px; font-size:10px; font-weight:bold; margin-left:8px; line-height:14px; vertical-align:middle;">${unreadCount}</span>`
            : '';
        
        div.innerHTML = `
            <div>
                <div class="peer-name">${displayName} ${unreadBadge}</div>
                <div class.peer-id">${p.user_id.substring(0, 16)}</div>
            </div>
            <div style="width:8px; height:8px; border-radius:50%; background:${statusColor}" title="${isOnline ? 'Online' : 'Offline'}"></div>
        `;
        
        div.onclick = () => startChat(p.user_id);
        list.appendChild(div);
    });
    
    if (currentChatId) {
        const p = peersMap[currentChatId];
        const name = p && p.nickname ? p.nickname : (currentChatId.substring(0, 8) + '...');
        document.getElementById('chatTitle').innerText = `Chat with: ${name}`;
    }
}

async function startChat(targetId = null) {
    targetId = targetId || document.getElementById('targetId').value;
    if(!targetId) return;

    if (!peersMap[targetId]) {
        await fetch('/api/rename', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target_id: targetId, name: null})
        });
        await updateState();
    }
    
    currentChatId = targetId;
    document.getElementById('chatHeader').style.display = 'flex';
    document.getElementById('messages').innerHTML = '';
    
    await fetch('/api/read_chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({chat_id: currentChatId})
    });

    const peerItem = document.querySelector(`.peer-item .peer-id[innerText^='${targetId}']`);
    if(peerItem) {
        const badge = peerItem.closest('.peer-item').querySelector('.unread-badge');
        if(badge) badge.remove();
    }
    
    Array.from(document.querySelectorAll('.peer-item')).forEach(el => {
        el.classList.remove('active');
        const peerIdEl = el.querySelector('.peer-id');
        if (peerIdEl && peerIdEl.innerText.startsWith(targetId)) {
            el.classList.add('active');
        }
    });

    refreshMessages();
}

async function refreshMessages() {
    if(!currentChatId) return;
    
    const res = await fetch(`/api/messages/${currentChatId}`);
    const msgs = await res.json();
    
    const container = document.getElementById('messages');
    const isAtBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 50;

    const newHtml = msgs.map(m => {
        const time = new Date(m.timestamp).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
        const status = m.is_outgoing ? '✓' : '';
        return `
            <div class="msg ${m.is_outgoing ? 'me' : 'other'}">
                ${m.content}
                <div style="font-size: 9px; opacity: 0.5; text-align: right; margin-top: 3px;">${time} ${status}</div>
            </div>
        `;
    }).join('');

    // Проверяем, есть ли новые сообщения (простая проверка по количеству)
    const hasNewMessages = container.querySelectorAll('.msg').length < msgs.length;

    if (container.innerHTML.length !== newHtml.length) {
        container.innerHTML = newHtml;
        if (isAtBottom) {
            container.scrollTop = container.scrollHeight;
        }

        // --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
        // Если мы в этом чате и появились новые сообщения,
        // немедленно сообщаем бекенду, что мы их прочитали.
        if (hasNewMessages) {
            await fetch('/api/read_chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ chat_id: currentChatId })
            });
        }
    }
}

function showConnect() {
    document.getElementById('connectModal').style.display = 'flex';
}

async function connectNode() {
    const addr = document.getElementById('nodeAddress').value;
    await fetch('/api/connect', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({address: addr})
    });
    document.getElementById('connectModal').style.display = 'none';
}

function showRename() {
    if (!currentChatId) return;
    document.getElementById('renameModal').style.display = 'flex';
    document.getElementById('newName').value = '';
    document.getElementById('newName').focus();
}

async function submitRename() {
    const name = document.getElementById('newName').value;
    if (!name || !currentChatId) return;
    
    await fetch('/api/rename', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target_id: currentChatId, name: name})
    });
    
    document.getElementById('renameModal').style.display = 'none';
    updateState();
}

async function send() {
    const txt = document.getElementById('msgInput').value;
    if(!txt || !currentChatId) return;
    
    await fetch('/api/send', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target_id: currentChatId, text: txt})
    });
    
    document.getElementById('msgInput').value = '';
    refreshMessages();
    setTimeout(updateState, 500);
}

// ДОБАВЬ ЭТУ ФУНКЦИЮ В КОНЕЦ
function copyId() {
    if (!myId) return;
    navigator.clipboard.writeText(myId).then(() => {
        const el = document.getElementById('my-id');
        const originalText = el.innerText;
        el.innerText = "COPIED TO CLIPBOARD!";
        el.style.color = "#0f0";
        
        setTimeout(() => {
            el.innerText = `ID: ${myId.substring(0, 16)}... (Click to Copy)`;
            el.style.color = "#666";
        }, 1500);
    }).catch(err => {
        console.error('Failed to copy: ', err);
        // Если не сработало (например, нет HTTPS), покажем полный ID чтобы скопировать руками
        prompt("Copy your full ID:", myId);
    });
}

init();