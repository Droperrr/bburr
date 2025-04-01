from datetime import datetime
from database import get_db_connection
from tenacity import retry, stop_after_attempt, wait_exponential
from collections import defaultdict, deque
import json

def parse_timestamp(timestamp_str):
    try:
        return datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S.%f')
    except ValueError:
        return datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')

def extract_price_from_swaps(token_id, mint_address, app):
    conn, cursor = get_db_connection()
    cursor.execute('''
    SELECT amount, timestamp
    FROM transactions
    WHERE token_id = ? AND type = 'SWAP'
    ORDER BY timestamp DESC
    LIMIT 1
    ''', (token_id,))
    swap = cursor.fetchone()
    conn.close()

    if swap:
        amount, timestamp = swap
        app.log(f"Извлечена SWAP-транзакция для {mint_address} на {timestamp}, но value_usd отсутствует")
        return None  # Пока нет value_usd в таблице
    app.log(f"Не найдено SWAP-транзакций для {mint_address}")
    return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_wallet_balance(wallet_address, app):
    from api import requests
    helius_api_key = app.helius_api_key
    url = f"https://api.helius.xyz/v0/addresses/{wallet_address}/balances?api-key={helius_api_key}"
    try:
        app.log(f"Запрос баланса для кошелька {wallet_address}")
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        app.log(f"Ответ API для {wallet_address}: {json.dumps(data, indent=2)}")
        if 'tokens' not in data:
            app.log(f"Неверный формат ответа для {wallet_address}")
            return None
        return next((float(t['amount']) / (10 ** t['decimals'])
                    for t in data['tokens'] if t['mint'] == app.current_mint_address), 0.0)
    except KeyError as e:
        app.log(f"Отсутствует ключ в ответе для {wallet_address}: {e}")
        return None
    except Exception as e:
        app.log(f"Ошибка получения баланса для {wallet_address}: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_wallet_balances(wallet_addresses, mint_address, app):
    from api import requests
    helius_api_key = app.helius_api_key
    url = "https://api.helius.xyz/v0/addresses/balances"
    params = {
        'api-key': helius_api_key,
        'addresses': wallet_addresses,
        'includeTokens': 'true'
    }
    try:
        app.log(f"Запрос балансов для {len(wallet_addresses)} кошельков")
        response = requests.get(url, params=params)
        response.raise_for_status()
        balances = {}
        for wallet_data in response.json():
            for token in wallet_data.get('tokens', []):
                if token['mint'] == mint_address:
                    balances[wallet_data['address']] = float(token['amount']) / (10 ** token['decimals'])
        app.log(f"Получены балансы для {len(balances)} кошельков")
        return balances
    except Exception as e:
        app.log(f"Ошибка получения балансов: {e}")
        return {}

def find_connected_wallets(token_id, max_depth=5):
    conn, cursor = get_db_connection()
    cursor.execute('''
    SELECT from_address, to_address
    FROM transactions
    WHERE token_id = ?
    ''', (token_id,))
    relations = cursor.fetchall()
    conn.close()

    graph = defaultdict(list)
    for from_addr, to_addr in relations:
        if from_addr != "unknown" and to_addr != "unknown":
            graph[from_addr].append(to_addr)
            graph[to_addr].append(from_addr)

    connected_groups = []
    visited = set()

    for wallet in graph:
        if wallet in visited:
            continue
        group = set()
        queue = deque([(wallet, 0)])
        visited.add(wallet)
        group.add(wallet)

        while queue:
            current_wallet, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for neighbor in graph[current_wallet]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    group.add(neighbor)
                    queue.append((neighbor, depth + 1))

        connected_groups.append(group)

    return connected_groups