# api.py
import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
import base64
from datetime import datetime
import json
from database import save_price
from config import HELIUS_API_KEY

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_historical_transactions(mint_address, token_id, app):
    helius_api_url = f"https://api.helius.xyz/v0/addresses/{mint_address}/transactions?api-key={app.helius_api_key}"
    
    total_supply, decimals, symbol = fetch_token_metadata(mint_address, app)
    if decimals is None:
        app.log(f"Предупреждение: не удалось получить decimals для {mint_address}, используется raw amount")
        decimals = 0
    
    conn, cursor = app.get_db_connection()
    cursor.execute('''
    UPDATE tokens
    SET total_supply = ?, decimals = ?, symbol = ?
    WHERE token_id = ?
    ''', (total_supply, decimals, symbol, token_id))
    conn.commit()
    conn.close()

    initial_recipients = set()
    transfer_count = 0
    MINT_THRESHOLD = 20

    before = None
    while True:
        if app.paused:
            app.log("Анализ приостановлен. Ожидание возобновления...")
            app.pause_event.wait()
        try:
            params = {"before": before} if before else {}
            response = requests.get(helius_api_url, params=params)
            response.raise_for_status()
            transactions = response.json()
            if not transactions:
                break
            for tx in transactions:
                signature = tx.get("signature", "unknown")
                block = tx.get("slot", 0)
                timestamp = datetime.fromtimestamp(tx.get("timestamp", int(time.time())))
                tx_type = tx.get("type", "UNKNOWN")
                token_transfers = tx.get("tokenTransfers", [])
                
                app.log(f"Транзакция {signature}: тип={tx_type}, tokenTransfers={len(token_transfers)}")
                
                if token_transfers:
                    for transfer in token_transfers:
                        if transfer.get("mint") == mint_address:
                            amount_raw = float(transfer.get("tokenAmount", 0))
                            amount = amount_raw / (10 ** decimals) if decimals else amount_raw
                            from_address = transfer.get("fromUserAccount", "unknown")
                            to_address = transfer.get("toUserAccount", "unknown")
                            
                            is_initial_recipient = 0
                            if tx_type == "TRANSFER" and transfer_count < MINT_THRESHOLD and to_address != "unknown":
                                initial_recipients.add(to_address)
                                is_initial_recipient = 1
                                transfer_count += 1
                            
                            from database import save_transaction
                            save_transaction(token_id, signature, block, timestamp, tx_type, from_address, to_address, amount, symbol, is_initial_recipient)
                            app.log(f"Сохранён трансфер: {amount} токенов от {from_address} к {to_address} (тип: {tx_type}, первичный: {is_initial_recipient})")
            before = transactions[-1]["signature"]
            app.log(f"Загружено {len(transactions)} исторических транзакций для {mint_address}")
        except Exception as e:
            app.log(f"Ошибка загрузки транзакций для {mint_address}: {e}")
            break

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_current_price(mint_address, token_id, app):
    url = "https://price.jup.ag/v4/price"
    params = {"ids": mint_address}
    response = requests.get(url, params=params)
    response.raise_for_status()
    price_data = response.json()
    if "data" in price_data and mint_address in price_data["data"]:
        price_usd = float(price_data["data"][mint_address]["price"])
        timestamp = datetime.now()
        save_price(token_id, price_usd, timestamp)
        app.log(f"Сохранена текущая цена для {mint_address}: {price_usd:.4f} USD на {timestamp}")
        return price_usd
    app.log(f"Данные о цене для {mint_address} недоступны через Jupiter Price API")
    return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_token_metadata(mint_address, app):
    url = f"https://api.helius.xyz/v0/tokens/metadata?api-key={app.helius_api_key}"
    params = {"mintAccounts": [mint_address]}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        metadata = response.json()
        if metadata and len(metadata) > 0:
            token_metadata = metadata[0]
            total_supply = float(token_metadata.get("tokenSupply", {}).get("value", 0))
            decimals = token_metadata.get("tokenSupply", {}).get("decimals", 0)
            total_supply = total_supply / (10 ** decimals) if decimals else total_supply
            symbol = token_metadata.get("symbol", "UNKNOWN")
            app.log(f"Total supply для {mint_address}: {total_supply}, decimals: {decimals}, symbol: {symbol}")
            return total_supply, decimals, symbol
        app.log(f"Метаданные для {mint_address} не найдены")
        return None, None, "UNKNOWN"
    except requests.exceptions.HTTPError as e:
        app.log(f"Ошибка HTTP при получении метаданных для {mint_address}: {e}")
        return None, None, "UNKNOWN"
    except Exception as e:
        app.log(f"Неизвестная ошибка при получении метаданных для {mint_address}: {e}")
        return None, None, "UNKNOWN"

def fetch_swap_order(mint_address, token_id, app, amount_to_swap=1):
    _, decimals, _ = fetch_token_metadata(mint_address, app)
    if decimals is None:
        app.log(f"Невозможно получить данные о свопе для {mint_address}: не удалось определить decimals")
        return None
    amount = int(amount_to_swap * (10 ** decimals))
    url = "https://api.jup.ag/ultra/v1/order"
    usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    params = {"inputMint": mint_address, "outputMint": usdc_mint, "amount": amount}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        order_data = response.json()
        app.log(f"Получены данные о свопе для {mint_address}: {json.dumps(order_data, indent=2)}")
        return order_data
    except Exception as e:
        app.log(f"Ошибка получения данных о свопе для {mint_address}: {e}")
        return None

def execute_swap(order_response, private_key_str, app, token_id, mint_address, amount_to_swap):
    if not order_response or "transaction" not in order_response:
        app.log("Невозможно выполнить своп: некорректный ответ от API")
        return
    try:
        transaction_base64 = order_response["transaction"]
        transaction_bytes = base64.b64decode(transaction_base64)
        transaction = VersionedTransaction.from_bytes(transaction_bytes)
        private_key = Keypair.from_base58_string(private_key_str)
        app.log(f"Публичный ключ кошелька: {private_key.pubkey()}")
        transaction.sign([private_key])
        signed_transaction = base64.b64encode(transaction.to_bytes()).decode('utf-8')
        url = "https://api.jup.ag/ultra/v1/execute"
        payload = {"signedTransaction": signed_transaction, "requestId": order_response["requestId"]}
        response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload)
        response.raise_for_status()
        execute_response = response.json()
        if execute_response.get("status") == "Success":
            app.log(f"Своп выполнен успешно: {json.dumps(execute_response, indent=2)}")
            app.log(f"URL транзакции: https://solscan.io/tx/{execute_response['signature']}")
            from database import save_transaction
            save_transaction(
                token_id=token_id,
                signature=execute_response['signature'],
                block=0,  # Helius API не предоставляет block в этом ответе
                timestamp=datetime.now(),
                action='SWAP',
                from_address=str(private_key.pubkey()),
                to_address='DEX',
                amount=amount_to_swap,
                token_symbol=mint_address
            )
        else:
            app.log(f"Своп не выполнен: {json.dumps(execute_response, indent=2)}")
    except Exception as e:
        app.log(f"Ошибка выполнения свопа: {e}")

def calculate_and_save_mcap(mint_address, token_id, total_supply, app):
    from database import get_db_connection
    conn, cursor = get_db_connection()
    cursor.execute('''
    SELECT price_usd, timestamp
    FROM price_history
    WHERE token_id = ?
    ORDER BY timestamp ASC
    ''', (token_id,))
    prices = cursor.fetchall()
    if not prices:
        app.log(f"Нет данных о цене для {mint_address} для расчета MCAP")
        conn.close()
        return

    mcap_ath = 0
    for price, _ in prices:
        mcap = float(price) * total_supply
        if mcap > mcap_ath:
            mcap_ath = mcap

    cursor.execute('''
    UPDATE tokens
    SET mcap_ath = ?
    WHERE token_id = ?
    ''', (mcap_ath, token_id))
    conn.commit()
    conn.close()
    app.log(f"MCAP ATH для {mint_address}: {mcap_ath:.2f} USD")