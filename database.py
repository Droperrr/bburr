import sqlite3
import threading
import os

# Указываем новый путь к базе данных
DB_PATH = r"D:\auto\burn\token_transactions.db"

# Проверяем, существует ли директория, и создаем её, если нет
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Глобальное соединение и блокировка
_conn = None
_lock = threading.Lock()

def init_db(db_path=DB_PATH):
    """Инициализирует базу данных, создаёт таблицу transactions, если она не существует."""
    global _conn
    _conn = sqlite3.connect(db_path, timeout=10)  # Таймаут 10 секунд
    cursor = _conn.cursor()
    
    # Создаём таблицу, если она ещё не существует
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id INTEGER,
            signature TEXT UNIQUE,
            block INTEGER,
            timestamp DATETIME,
            type TEXT,
            from_address TEXT,
            to_address TEXT,
            amount REAL,
            symbol TEXT,
            value_sol REAL,  -- Новое поле для стоимости в SOL
            is_initial_recipient INTEGER DEFAULT 0
        )
    ''')
    
    # Проверяем, есть ли столбец is_initial_recipient
    cursor.execute("PRAGMA table_info(transactions)")
    columns = [col[1] for col in cursor.fetchall()]
    if "is_initial_recipient" not in columns:
        cursor.execute("ALTER TABLE transactions ADD COLUMN is_initial_recipient INTEGER DEFAULT 0")
        print("Добавлен столбец is_initial_recipient в таблицу transactions")
    
    # Проверяем, есть ли столбец value_sol
    if "value_sol" not in columns:
        cursor.execute("ALTER TABLE transactions ADD COLUMN value_sol REAL")
        print("Добавлен столбец value_sol в таблицу transactions")
    
    _conn.commit()

def save_transaction(token_id, signature, block, timestamp, type_, from_address, to_address, amount, symbol, value_sol=None, is_initial_recipient=0):
    """Сохраняет транзакцию в базу данных с синхронизацией."""
    with _lock:  # Синхронизируем доступ
        cursor = _conn.cursor()
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO transactions (token_id, signature, block, timestamp, type, from_address, to_address, amount, symbol, value_sol, is_initial_recipient)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (token_id, signature, block, str(timestamp), type_, from_address, to_address, amount, symbol, value_sol, is_initial_recipient))
            _conn.commit()
        except sqlite3.OperationalError as e:
            print(f"Ошибка базы данных при сохранении транзакции {signature}: {e}")
        except Exception as e:
            print(f"Неизвестная ошибка при сохранении транзакции {signature}: {e}")

def get_db_connection():
    """Возвращает глобальное соединение и курсор для базы данных."""
    return _conn, _conn.cursor()

def close_db():
    """Закрывает соединение с базой данных при завершении работы."""
    global _conn
    if _conn:
        _conn.close()
        _conn = None