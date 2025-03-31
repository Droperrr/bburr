# database.py
import sqlite3
import logging
from datetime import datetime
from config import DB_PATH

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('token_monitor.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    return conn, cursor

def initialize_database():
    conn, cursor = get_db_connection()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS tokens (
        token_id INTEGER PRIMARY KEY AUTOINCREMENT,
        mint_address TEXT UNIQUE,
        symbol TEXT,
        total_supply REAL,
        decimals INTEGER,
        mcap_ath REAL
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS transactions (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        token_id INTEGER,
        signature TEXT,
        block INTEGER,
        timestamp DATETIME,
        action TEXT,
        from_address TEXT,
        to_address TEXT,
        amount REAL,
        token_symbol TEXT,
        value_usd REAL,
        is_initial_recipient INTEGER DEFAULT 0,
        FOREIGN KEY (token_id) REFERENCES tokens(token_id)
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS price_history (
        price_id INTEGER PRIMARY KEY AUTOINCREMENT,
        token_id INTEGER,
        price_usd REAL,
        timestamp DATETIME,
        FOREIGN KEY (token_id) REFERENCES tokens(token_id)
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS wallet_relations (
        relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        token_id INTEGER,
        from_address TEXT,
        to_address TEXT,
        transaction_id INTEGER,
        FOREIGN KEY (token_id) REFERENCES tokens(token_id),
        FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id)
    )
    ''')
    conn.commit()
    conn.close()
    logger.info(f"База данных инициализирована по пути {DB_PATH}")

def save_transaction(token_id, signature, block, timestamp, action, from_address, to_address, amount, token_symbol, is_initial_recipient=False):
    conn, cursor = get_db_connection()
    cursor.execute('''
    INSERT INTO transactions (token_id, signature, block, timestamp, action, from_address, to_address, amount, token_symbol, is_initial_recipient)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (token_id, signature, block, timestamp, action, from_address, to_address, amount, token_symbol, is_initial_recipient))
    
    if from_address != "unknown" and to_address != "unknown":
        cursor.execute('''
        INSERT INTO wallet_relations (token_id, from_address, to_address, transaction_id)
        VALUES (?, ?, ?, ?)
        ''', (token_id, from_address, to_address, cursor.lastrowid))
    
    conn.commit()
    logger.debug(f"Сохранена транзакция: token_id={token_id}, action={action}, amount={amount}, token_symbol={token_symbol}")
    conn.close()

def save_price(token_id, price_usd, timestamp):
    conn, cursor = get_db_connection()
    cursor.execute('''
    INSERT INTO price_history (token_id, price_usd, timestamp)
    VALUES (?, ?, ?)
    ''', (token_id, price_usd, timestamp))
    conn.commit()
    conn.close()