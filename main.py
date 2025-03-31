# main.py
import tkinter as tk
from tkinter import scrolledtext, ttk
import threading
import sys
import time
from datetime import datetime
from database import get_db_connection, save_transaction, save_price, initialize_database
from api import fetch_historical_transactions, fetch_current_price, fetch_token_metadata, fetch_swap_order, execute_swap, calculate_and_save_mcap
from utils import parse_timestamp, extract_price_from_swaps, fetch_wallet_balance, fetch_wallet_balances, find_connected_wallets
import logging
from config import HELIUS_API_KEY

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('token_monitor.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

class TokenMonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Token Monitor")
        self.helius_api_key = HELIUS_API_KEY

        self.paused = False
        self.pause_event = threading.Event()
        self.pause_event.set()

        self.root.grid_rowconfigure(0, weight=0)
        self.root.grid_rowconfigure(1, weight=0)
        self.root.grid_rowconfigure(2, weight=1)
        self.root.grid_rowconfigure(3, weight=1)
        self.root.grid_rowconfigure(4, weight=0)
        self.root.grid_rowconfigure(5, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        self.input_frame = ttk.Frame(self.root)
        self.input_frame.grid(row=0, column=0, padx=10, pady=5, sticky="ew")
        self.label = ttk.Label(self.input_frame, text="Введите адреса токенов (через пробел):")
        self.label.pack(side=tk.LEFT)
        self.address_entry = ttk.Entry(self.input_frame)
        self.address_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.start_button = ttk.Button(self.input_frame, text="Начать анализ", command=self.start_analysis)
        self.start_button.pack(side=tk.LEFT)
        self.pause_button = ttk.Button(self.input_frame, text="Пауза", command=self.toggle_pause)
        self.pause_button.pack(side=tk.LEFT, padx=5)
        self.view_db_button = ttk.Button(self.input_frame, text="Просмотреть базу данных", command=self.view_database)
        self.view_db_button.pack(side=tk.LEFT, padx=5)

        self.swap_frame = ttk.Frame(self.root)
        self.swap_frame.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        self.private_key_label = ttk.Label(self.swap_frame, text="Закрытый ключ (base58):")
        self.private_key_label.pack(side=tk.LEFT)
        self.private_key_entry = ttk.Entry(self.swap_frame, show="*")
        self.private_key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.swap_button = ttk.Button(self.swap_frame, text="Выполнить своп", command=self.execute_swap_action)
        self.swap_button.pack(side=tk.LEFT, padx=5)

        self.log_area = scrolledtext.ScrolledText(self.root, wrap=tk.WORD)
        self.log_area.grid(row=2, column=0, padx=10, pady=5, sticky="nsew")

        self.results_frame = ttk.LabelFrame(self.root, text="Результаты анализа", padding=10)
        self.results_frame.grid(row=3, column=0, padx=10, pady=5, sticky="nsew")
        self.results_text = scrolledtext.ScrolledText(self.results_frame, wrap=tk.WORD)
        self.results_text.grid(row=0, column=0, sticky="nsew")

        self.wallet_count_frame = ttk.Frame(self.root)
        self.wallet_count_frame.grid(row=4, column=0, padx=10, pady=5, sticky="ew")
        self.wallet_count_label = ttk.Label(self.wallet_count_frame, text="Обнаружено кошельков: 0")
        self.wallet_count_label.pack(side=tk.LEFT, padx=5)
        self.connected_wallets_label = ttk.Label(self.wallet_count_frame, text="Связанных кошельков: 0")
        self.connected_wallets_label.pack(side=tk.LEFT, padx=5)

        self.wallets_frame = ttk.LabelFrame(self.root, text="Кошельки и балансы", padding=10)
        self.wallets_frame.grid(row=5, column=0, padx=10, pady=5, sticky="nsew")
        self.wallets_tree = ttk.Treeview(self.wallets_frame, columns=("Wallet", "Balance"), show="headings")
        self.wallets_tree.heading("Wallet", text="Адрес кошелька")
        self.wallets_tree.heading("Balance", text="Баланс (токены)")
        self.wallets_tree.column("Wallet", width=300)
        self.wallets_tree.column("Balance", width=150)
        self.wallets_tree.grid(row=0, column=0, sticky="nsew")

        self.wallets_tree.tag_configure("increase", background="lightgreen")
        self.wallets_tree.tag_configure("decrease", background="lightcoral")

        self.current_token_id = None
        self.current_mint_address = None
        self.wallet_update_thread = None
        self.recommendations = {}
        self.previous_balances = {}

    def log(self, message):
        self.log_area.insert(tk.END, f"{datetime.now()} - {message}\n")
        logger.info(message)

    def update_results(self, message):
        self.results_text.insert(tk.END, f"{message}\n\n")
        self.results_text.see(tk.END)

    def get_db_connection(self):
        return get_db_connection()

    def update_wallets(self):
        if not self.current_token_id or not self.current_mint_address:
            self.log("Ошибка: текущий token_id или mint_address не установлен")
            return

        conn, cursor = get_db_connection()
        cursor.execute('''
        SELECT DISTINCT from_address
        FROM transactions
        WHERE token_id = ? AND from_address != 'unknown'
        UNION
        SELECT DISTINCT to_address
        FROM transactions
        WHERE token_id = ? AND to_address != 'unknown'
        ''', (self.current_token_id, self.current_token_id))
        all_wallets = set(row[0] for row in cursor.fetchall())
        total_wallets = len(all_wallets)
        self.wallet_count_label.config(text=f"Обнаружено кошельков: {total_wallets}")

        connected_groups = find_connected_wallets(self.current_token_id, max_depth=5)
        total_connected = sum(len(group) for group in connected_groups if len(group) > 1)
        self.connected_wallets_label.config(text=f"Связанных кошельков: {total_connected}")

        cursor.execute('''
        SELECT DISTINCT to_address, action, amount
        FROM transactions
        WHERE token_id = ?
          AND amount > 0
        UNION
        SELECT DISTINCT from_address, action, amount
        FROM transactions
        WHERE token_id = ?
          AND amount > 0
        ''', (self.current_token_id, self.current_token_id))
        wallets = cursor.fetchall()
        conn.close()

        self.log(f"Найдено {len(wallets)} уникальных адресов для token_id={self.current_token_id}")
        for wallet in wallets:
            self.log(f"Адрес: {wallet[0]}, действие: {wallet[1]}, сумма: {wallet[2]}")

        for item in self.wallets_tree.get_children():
            self.wallets_tree.delete(item)

        if not wallets:
            self.log("Нет кошельков для отображения")
            return

        wallet_addresses = [wallet[0] for wallet in wallets if wallet[0] != "unknown"]
        balances = fetch_wallet_balances(wallet_addresses, self.current_mint_address, self)

        wallet_balances = []
        for wallet in wallets:
            wallet_address = wallet[0]
            if wallet_address == "unknown":
                continue
            balance = balances.get(wallet_address, 0.0)
            previous_balance = self.previous_balances.get(wallet_address, 0.0)
            tag = "increase" if balance > previous_balance else "decrease" if balance < previous_balance else ""
            self.previous_balances[wallet_address] = balance
            wallet_balances.append((wallet_address, balance, tag))

        wallet_balances.sort(key=lambda x: x[1], reverse=True)
        for wallet_address, balance, tag in wallet_balances:
            self.wallets_tree.insert("", tk.END, values=(wallet_address, f"{balance:.6f}"), tags=(tag,))
        self.log(f"Таблица кошельков обновлена: {len(wallet_balances)} записей")

    def start_wallet_updates(self):
        def update_loop():
            while not self.paused and self.current_token_id:
                if self.paused:
                    self.log("Обновление кошельков приостановлено.")
                    self.pause_event.wait()
                self.update_wallets()
                time.sleep(10)
            self.log("Обновление кошельков завершено.")

        if self.wallet_update_thread is None or not self.wallet_update_thread.is_alive():
            self.wallet_update_thread = threading.Thread(target=update_loop, daemon=True)
            self.wallet_update_thread.start()
            self.log("Запущено обновление кошельков в реальном времени.")

    def toggle_pause(self):
        if not self.paused:
            self.paused = True
            self.pause_event.clear()
            self.pause_button.config(text="Возобновить")
            self.log("Анализ приостановлен.")
        else:
            self.paused = False
            self.pause_event.set()
            self.pause_button.config(text="Пауза")
            self.log("Анализ возобновлён.")
            self.start_wallet_updates()

    def view_database(self):
        self.log("Просмотр содержимого базы данных...")
        conn, cursor = get_db_connection()

        def print_table(table_name):
            self.log(f"\nСодержимое таблицы {table_name}:")
            cursor.execute(f"SELECT * FROM {table_name}")
            rows = cursor.fetchall()
            if not rows:
                self.log("Таблица пуста.")
                return
            columns = [description[0] for description in cursor.description]
            self.log(" | ".join(columns))
            self.log("-" * 50)
            for row in rows:
                self.log(" | ".join(str(item) for item in row))

        print_table("tokens")
        print_table("transactions")
        print_table("price_history")
        print_table("wallet_relations")
        conn.close()

    def start_analysis(self):
        mint_addresses = self.address_entry.get().strip().split()
        if not mint_addresses:
            self.log("Ошибка: Не указаны адреса токенов.")
            return

        self.results_text.delete(1.0, tk.END)
        self.recommendations = {}
        self.paused = False
        self.pause_event.set()
        self.pause_button.config(text="Пауза")
        threading.Thread(target=self.run_analysis, args=([mint_addresses[0]],), daemon=True).start()

    def execute_swap_action(self):
        if not self.current_mint_address or not self.current_token_id:
            self.log("Ошибка: Сначала выполните анализ токена.")
            return

        private_key = self.private_key_entry.get().strip()
        if not private_key:
            self.log("Ошибка: Укажите закрытый ключ кошелька.")
            return

        self.log(f"Запуск свопа для токена {self.current_mint_address}...")
        threading.Thread(target=self.run_swap, args=(self.current_mint_address, self.current_token_id, private_key), daemon=True).start()

    def run_swap(self, mint_address, token_id, private_key):
        order_response = fetch_swap_order(mint_address, token_id, self, amount_to_swap=1)
        if not order_response:
            self.log(f"Не удалось получить данные о свопе для {mint_address}")
            return
        execute_swap(order_response, private_key, self, token_id, mint_address, amount_to_swap=1)

    def run_analysis(self, mint_addresses):
        for mint_address in mint_addresses:
            if self.paused:
                self.log("Анализ приостановлен. Ожидание возобновления...")
                self.pause_event.wait()

            conn, cursor = get_db_connection()
            cursor.execute('INSERT OR IGNORE INTO tokens (mint_address, symbol) VALUES (?, ?)', (mint_address, 'UNKNOWN'))
            cursor.execute('SELECT token_id FROM tokens WHERE mint_address = ?', (mint_address,))
            token_id = cursor.fetchone()[0]
            conn.commit()
            conn.close()

            self.current_mint_address = mint_address
            self.current_token_id = token_id

            self.log(f"Загрузка исторических транзакций для токена {mint_address}...")
            fetch_historical_transactions(mint_address, token_id, self)

            self.log(f"Загрузка текущей цены для токена {mint_address}...")
            price_usd = None
            try:
                price_usd = fetch_current_price(mint_address, token_id, self)
            except Exception as e:
                self.log(f"Не удалось получить цену через API: {e}")
                price_usd = extract_price_from_swaps(token_id, mint_address, self)

            self.log(f"Получение total supply для токена {mint_address}...")
            total_supply = None
            try:
                total_supply, _, _ = fetch_token_metadata(mint_address, self)
            except Exception as e:
                self.log(f"Не удалось получить total supply: {e}")

            if total_supply and price_usd:
                self.log(f"Расчёт MCAP для токена {mint_address}...")
                calculate_and_save_mcap(mint_address, token_id, total_supply, self)

            self.analyze_hypotheses()
            self.start_wallet_updates()

    def analyze_hypotheses(self):
        if self.paused:
            self.log("Анализ приостановлен. Ожидание возобновления...")
            self.pause_event.wait()

        conn, cursor = get_db_connection()
        cursor.execute('SELECT token_id, mint_address FROM tokens')
        tokens = cursor.fetchall()
        conn.close()

        for token_id, mint_address in tokens:
            if self.paused:
                self.log("Анализ приостановлен. Ожидание возобновления...")
                self.pause_event.wait()

            self.log(f"\nАнализ гипотез для токена {mint_address} (token_id: {token_id})")
            recommended_exit_time = None
            reasoning = []

            conn, cursor = get_db_connection()
            cursor.execute('''
            SELECT COUNT(DISTINCT to_address) as wallet_count
            FROM transactions
            WHERE action = 'TRANSFER'
              AND token_id = ?
              AND amount > 0
            ''', (token_id,))
            wallet_count = cursor.fetchone()[0]
            conn.close()
            self.log(f"Гипотеза 2: Количество кошельков = {wallet_count}")
            if wallet_count >= 200:
                reasoning.append(f"Большое количество кошельков ({wallet_count} ≥ 200) — возможный признак пампа.")
                if not recommended_exit_time:
                    conn, cursor = get_db_connection()
                    cursor.execute('''
                    SELECT timestamp FROM transactions
                    WHERE token_id = ? AND action = 'TRANSFER'
                    ORDER BY timestamp DESC LIMIT 1
                    ''', (token_id,))
                    last_tx = cursor.fetchone()
                    conn.close()
                    if last_tx:
                        recommended_exit_time = parse_timestamp(last_tx[0])

            if recommended_exit_time:
                recommendation = (f"Токен: {mint_address}\n"
                                 f"Рекомендуемое время выхода: {recommended_exit_time}\n"
                                 f"Аргументация:\n- " + "\n- ".join(reasoning))
            else:
                recommendation = f"Токен: {mint_address}\nНедостаточно данных для рекомендации времени выхода."
            self.log(recommendation)
            self.recommendations[mint_address] = recommendation
            self.update_results(recommendation)

def start_monitoring():
    try:
        initialize_database()
        root = tk.Tk()
        app = TokenMonitorApp(root)
        root.mainloop()
    except KeyboardInterrupt:
        logger.info("Программа завершена пользователем.")
        sys.exit(0)

if __name__ == "__main__":
    start_monitoring()