import os
import requests
import json
import time
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from datetime import datetime
from database import save_transaction, get_db_connection
from config import HELIUS_API_KEY

print(f"Используется api.py из: {os.path.abspath(__file__)}")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
}

# Список RPC URL-ов: основной и резервные
RPC_URLS = [
    "https://eclipse.helius-rpc.com/?api-key=8b65d9ce-4f9b-4b90-9e4c-af088de240b2",  # Основной (пинг: 0.39 мс)
    "https://summer-lively-snow.solana-mainnet.quiknode.pro/b69b424247e41676252d186a90dc2d89e52949f1/?api-key=b69b424247e41676252d186a90dc2d89e52949f1",  # Резерв 1 (пинг: 0.43 мс)
    "https://solana-mainnet.g.alchemy.com/v2/B5yFeGSeD3toaX2L_TYlAre1hQNrWczH?api-key=B5yFeGSeD3toaX2L_TYlAre1hQNrWczH",  # Резерв 2 (пинг: 0.47 мс)
    "https://mainnet.helius-rpc.com/?api-key=8b65d9ce-4f9b-4b90-9e4c-af088de240b2",  # Резерв 3 (пинг: 0.50 мс)
    "https://api-mainnet.helius-rpc.com/?api-key=8b65d9ce-4f9b-4b90-9e4c-af088de240b2",  # Резерв 4 (пинг: 0.51 мс)
]

REQUEST_TIMEOUT = 10  # Таймаут 10 секунд

class RPCUnreachableException(Exception):
    """Исключение для случаев, когда RPC недоступен."""
    pass

def parse_timestamp(timestamp_str):
    """Парсит временную метку из строки."""
    try:
        return datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S.%f')
    except ValueError:
        return datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')

def try_request(payload, app, initial_url_index=0):
    """Пытается выполнить запрос, переключаясь на резервные RPC при сбоях."""
    current_url_index = initial_url_index
    while current_url_index < len(RPC_URLS):
        rpc_url = RPC_URLS[current_url_index]
        try:
            app.log(f"Запрос к RPC: {rpc_url}")
            response = requests.post(rpc_url, json=payload, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            app.log(f"Код ответа HTTP: {response.status_code}")
            response.raise_for_status()
            result = response.json()
            app.log(f"Ответ RPC: {json.dumps(result, indent=2)}")
            if "result" in result or "error" not in result:
                return result, current_url_index
            else:
                app.log(f"Некорректный ответ от {rpc_url}: {json.dumps(result, indent=2)}")
                current_url_index += 1
                if current_url_index < len(RPC_URLS):
                    app.log(f"Переключение на резервный RPC: {RPC_URLS[current_url_index]}")
        except (requests.exceptions.Timeout, requests.exceptions.HTTPError, requests.exceptions.RequestException) as e:
            app.log(f"Ошибка запроса к {rpc_url}: {e}")
            current_url_index += 1
            if current_url_index < len(RPC_URLS):
                app.log(f"Переключение на резервный RPC: {RPC_URLS[current_url_index]}")
    raise RPCUnreachableException("Все RPC недоступны")

def ping_rpc(app):
    """Пингует RPC, чтобы проверить его доступность."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getHealth",
        "params": []
    }
    try:
        result, _ = try_request(payload, app)
        if "result" in result and result["result"] == "ok":
            app.log("RPC доступен.")
            return True
        return False
    except Exception as e:
        app.log(f"Ошибка пинга RPC: {e}")
        return False

def wait_for_rpc(app):
    """Ожидает восстановления RPC, пингуя его каждые 5 секунд."""
    app.log("Ожидание восстановления RPC...")
    while True:
        if ping_rpc(app):
            app.log("RPC восстановлен. Продолжаем работу.")
            return
        app.log("RPC всё ещё недоступен. Ожидание 5 секунд перед следующим пингом...")
        time.sleep(5)

def extract_swap_data(tx, mint_address, decimals, app):
    """Извлекает данные о свопах из транзакции."""
    if "meta" not in tx or "innerInstructions" not in tx["meta"]:
        return None
    
    swap_data = None
    for inner in tx["meta"]["innerInstructions"]:
        for ix in inner["instructions"]:
            if "programId" in ix and ix["programId"] == "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSce":  # Raydium Program ID
                # Извлекаем данные о свопе
                if "parsed" in ix and ix["parsed"]["type"] == "swap":
                    info = ix["parsed"]["info"]
                    token_amount = None
                    sol_amount = None
                    for balance in tx["meta"]["postTokenBalances"]:
                        if balance["mint"] == mint_address:
                            token_amount = float(balance["uiTokenAmount"]["amount"]) / (10 ** decimals)
                    for pre_balance, post_balance in zip(tx["meta"]["preBalances"], tx["meta"]["postBalances"]):
                        sol_diff = (post_balance - pre_balance) / 1e9  # Лампорты в SOL
                        if sol_diff != 0:
                            sol_amount = abs(sol_diff)
                            break
                    if token_amount and sol_amount:
                        price_per_token = sol_amount / token_amount if token_amount != 0 else 0
                        swap_data = {
                            "token_amount": token_amount,
                            "sol_amount": sol_amount,
                            "price_per_token": price_per_token
                        }
                        app.log(f"Обнаружен своп: {token_amount} токенов за {sol_amount} SOL, цена за токен: {price_per_token} SOL")
    return swap_data

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=4, max=20),
    retry=retry_if_exception_type(RPCUnreachableException),
    after=lambda retry_state: wait_for_rpc(retry_state.outcome._exception.app) if retry_state.outcome.failed else None
)
def fetch_token_metadata_from_helius(mint_address, app, url_index=0):
    app.log(f"Попытка получить метаданные для {mint_address} через RPC")
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenSupply",
            "params": [mint_address]
        }
        app.log(f"JSON-RPC запрос: {json.dumps(payload, indent=2)}")
        result, new_url_index = try_request(payload, app, url_index)
        if "result" in result and "value" in result["result"]:
            total_supply_raw = result["result"]["value"]["amount"]
            decimals = result["result"]["value"]["decimals"]
            total_supply = float(total_supply_raw) / (10 ** decimals)
            app.log(f"Извлечены метаданные: total_supply={total_supply}, decimals={decimals}")
            return total_supply, decimals, "UNKNOWN", new_url_index
        app.log(f"Не удалось получить метаданные для {mint_address}")
        return None, None, "UNKNOWN", new_url_index
    except requests.exceptions.Timeout:
        app.log(f"Таймаут при запросе метаданных для {mint_address} (ждали {REQUEST_TIMEOUT} секунд)")
        raise RPCUnreachableException("RPC не отвечает") from None
    except requests.exceptions.HTTPError as e:
        app.log(f"HTTP ошибка при запросе метаданных для {mint_address}: {e}")
        return None, None, "UNKNOWN", url_index
    except Exception as e:
        app.log(f"Неизвестная ошибка при запросе метаданных для {mint_address}: {e}")
        return None, None, "UNKNOWN", url_index

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=4, max=20),
    retry=retry_if_exception_type(RPCUnreachableException),
    after=lambda retry_state: wait_for_rpc(retry_state.outcome._exception.app) if retry_state.outcome.failed else None
)
def fetch_token_metadata(mint_address, app):
    total_supply, decimals, symbol, _ = fetch_token_metadata_from_helius(mint_address, app)
    return total_supply, decimals, symbol

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=4, max=20),
    retry=retry_if_exception_type(RPCUnreachableException),
    after=lambda retry_state: wait_for_rpc(retry_state.outcome._exception.app) if retry_state.outcome.failed else None
)
def fetch_historical_transactions(mint_address, token_id, app, url_index=0):
    app.log(f"Загрузка транзакций для {mint_address} через RPC")
    total_supply, decimals, symbol, url_index = fetch_token_metadata_from_helius(mint_address, app, url_index)
    if decimals is None:
        app.log(f"Не удалось получить decimals для {mint_address}. Пропускаем транзакции.")
        return url_index
    before = None
    max_iterations = 100
    iteration = 0
    last_timestamp = None

    while iteration < max_iterations:
        iteration += 1
        app.log(f"Итерация {iteration}/{max_iterations} для загрузки транзакций...")
        if app.paused:
            app.log("Анализ приостановлен. Ожидание возобновления...")
            app.pause_event.wait()
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [mint_address, {"limit": 10, "before": before} if before else {"limit": 10}]
            }
            app.log(f"JSON-RPC запрос для транзакций: {json.dumps(payload, indent=2)}")
            result, url_index = try_request(payload, app, url_index)
            
            if "result" not in result:
                app.log(f"Отсутствует ключ 'result' в ответе для {mint_address}. Завершаем загрузку.")
                break
            if not result["result"]:
                app.log(f"Исторические транзакции для {mint_address} загружены полностью (пустой результат).")
                break

            signatures = result["result"]
            app.log(f"Получено {len(signatures)} подписей для обработки.")
            for sig in signatures:
                tx_payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        sig["signature"],
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0  # Добавляем поддержку версии транзакций
                        }
                    ]
                }
                app.log(f"JSON-RPC запрос для транзакции {sig['signature']}: {json.dumps(tx_payload, indent=2)}")
                tx_result, url_index = try_request(tx_payload, app, url_index)
                if "result" in tx_result and tx_result["result"]:
                    tx = tx_result["result"]
                    signature = tx["transaction"]["signatures"][0]
                    block = tx["slot"]
                    timestamp = datetime.fromtimestamp(tx["blockTime"] if tx["blockTime"] else int(time.time()))
                    
                    if last_timestamp and (last_timestamp - timestamp).total_seconds() > 3600:
                        app.log(f"Обнаружен возможный пропуск транзакций: разрыв между {last_timestamp} и {timestamp}")
                    
                    last_timestamp = timestamp

                    # Проверяем, является ли транзакция свопом
                    swap_data = extract_swap_data(tx, mint_address, decimals, app)
                    if swap_data:
                        save_transaction(token_id, signature, block, timestamp, "SWAP", 
                                       "unknown", "unknown", 
                                       swap_data["token_amount"], symbol, swap_data["price_per_token"])
                        app.log(f"Сохранён своп: {swap_data['token_amount']} токенов за {swap_data['price_per_token']} SOL")

                    # Обрабатываем трансферы
                    token_transfers = []
                    if "meta" in tx and "postTokenBalances" in tx["meta"]:
                        for balance in tx["meta"]["postTokenBalances"]:
                            if balance["mint"] == mint_address:
                                amount_raw = float(balance["uiTokenAmount"]["amount"])
                                amount = amount_raw / (10 ** decimals) if decimals else amount_raw
                                from_address = balance.get("owner", "unknown")
                                to_address = "unknown"
                                token_transfers.append({
                                    "from_address": from_address,
                                    "to_address": to_address,
                                    "amount": amount
                                })
                    for transfer in token_transfers:
                        save_transaction(token_id, signature, block, timestamp, "TRANSFER", 
                                       transfer["from_address"], transfer["to_address"], 
                                       transfer["amount"], symbol, None)
                        app.log(f"Сохранён трансфер: {transfer['amount']} токенов от {transfer['from_address']} к {transfer['to_address']}")
            app.last_signature = signatures[0]["signature"]
            before = signatures[-1]["signature"]
            app.log(f"Загружено {len(signatures)} исторических транзакций, последняя подпись: {app.last_signature}")
        except requests.exceptions.Timeout:
            app.log(f"Таймаут при загрузке транзакций для {mint_address} (ждали {REQUEST_TIMEOUT} секунд)")
            raise RPCUnreachableException("RPC не отвечает") from None
        except requests.exceptions.HTTPError as e:
            app.log(f"HTTP ошибка загрузки транзакций для {mint_address}: {e}")
            break
        except Exception as e:
            app.log(f"Ошибка загрузки транзакций для {mint_address}: {e}")
            break
        finally:
            app.log("Задержка 10 секунд перед следующим запросом")
            time.sleep(10)

    if iteration >= max_iterations:
        app.log(f"Достигнуто максимальное количество итераций ({max_iterations}). Завершаем загрузку.")
    return url_index

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=4, max=20),
    retry=retry_if_exception_type(RPCUnreachableException),
    after=lambda retry_state: wait_for_rpc(retry_state.outcome._exception.app) if retry_state.outcome.failed else None
)
def fetch_real_time_transactions(mint_address, token_id, app, url_index=0):
    if not app.last_signature:
        app.log("Нет начальной подписи для реального времени. Завершите загрузку исторических данных.")
        return url_index
    total_supply, decimals, symbol, url_index = fetch_token_metadata_from_helius(mint_address, app, url_index)
    if decimals is None:
        app.log(f"Не удалось получить decimals для {mint_address}. Пропускаем обновления.")
        return url_index
    last_timestamp = None
    try:
        # Получаем последнюю временную метку из базы для контроля пропусков
        conn, cursor = get_db_connection()
        cursor.execute('''
            SELECT timestamp
            FROM transactions
            WHERE token_id = ?
            ORDER BY timestamp DESC
            LIMIT 1
        ''', (token_id,))
        last_tx = cursor.fetchone()
        if last_tx:
            last_timestamp = parse_timestamp(last_tx[0])
        conn.close()

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [mint_address, {"limit": 10, "until": app.last_signature}]
        }
        app.log(f"JSON-RPC запрос для транзакций в реальном времени: {json.dumps(payload, indent=2)}")
        result, url_index = try_request(payload, app, url_index)
        if "result" not in result or not result["result"]:
            app.log(f"Нет новых транзакций для {mint_address} с момента {app.last_signature}")
            return url_index
        signatures = result["result"]
        for sig in signatures:
            tx_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    sig["signature"],
                    {
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0  # Добавляем поддержку версии транзакций
                    }
                ]
            }
            app.log(f"JSON-RPC запрос для транзакции {sig['signature']}: {json.dumps(tx_payload, indent=2)}")
            tx_result, url_index = try_request(tx_payload, app, url_index)
            if "result" in tx_result and tx_result["result"]:
                tx = tx_result["result"]
                signature = tx["transaction"]["signatures"][0]
                block = tx["slot"]
                timestamp = datetime.fromtimestamp(tx["blockTime"] if tx["blockTime"] else int(time.time()))
                
                if last_timestamp and (timestamp - last_timestamp).total_seconds() > 3600:
                    app.log(f"Обнаружен возможный пропуск транзакций в реальном времени: разрыв между {last_timestamp} и {timestamp}")
                
                last_timestamp = timestamp

                # Проверяем, является ли транзакция свопом
                swap_data = extract_swap_data(tx, mint_address, decimals, app)
                if swap_data:
                    save_transaction(token_id, signature, block, timestamp, "SWAP", 
                                   "unknown", "unknown", 
                                   swap_data["token_amount"], symbol, swap_data["price_per_token"])
                    app.log(f"Сохранён своп: {swap_data['token_amount']} токенов за {swap_data['price_per_token']} SOL")

                # Обрабатываем трансферы
                token_transfers = []
                if "meta" in tx and "postTokenBalances" in tx["meta"]:
                    for balance in tx["meta"]["postTokenBalances"]:
                        if balance["mint"] == mint_address:
                            amount_raw = float(balance["uiTokenAmount"]["amount"])
                            amount = amount_raw / (10 ** decimals) if decimals else amount_raw
                            from_address = balance.get("owner", "unknown")
                            to_address = "unknown"
                            token_transfers.append({
                                "from_address": from_address,
                                "to_address": to_address,
                                "amount": amount
                            })
                for transfer in token_transfers:
                    save_transaction(token_id, signature, block, timestamp, "TRANSFER", 
                                   transfer["from_address"], transfer["to_address"], 
                                   transfer["amount"], symbol, None)
                    app.log(f"Новая транзакция: {transfer['amount']} токенов от {transfer['from_address']} к {transfer['to_address']}")
        if signatures:
            app.last_signature = signatures[0]["signature"]
            app.log(f"Обработано {len(signatures)} новых транзакций, новая последняя подпись: {app.last_signature}")
    except requests.exceptions.Timeout:
        app.log(f"Таймаут при получении данных в реальном времени для {mint_address} (ждали {REQUEST_TIMEOUT} секунд)")
        raise RPCUnreachableException("RPC не отвечает") from None
    except Exception as e:
        app.log(f"Ошибка получения данных в реальном времени для {mint_address}: {e}")
    finally:
        app.log("Задержка 10 секунд перед следующим запросом реального времени")
        time.sleep(10)
    return url_index