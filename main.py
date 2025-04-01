import sys
import os
import threading
import time
from PyQt5.QtWidgets import QApplication, QMainWindow, QTextEdit, QLineEdit, QPushButton, QVBoxLayout, QWidget
from PyQt5.QtCore import QTimer, pyqtSignal, pyqtSlot, QThread, Qt  # Добавляем импорт Qt
from database import init_db, save_transaction, close_db
from api import fetch_historical_transactions, fetch_token_metadata, fetch_real_time_transactions
from config import HELIUS_API_KEY

class TokenAnalyzerApp(QMainWindow):
    log_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.log_buffer = []
        self.initUI()
        self.paused = False
        self.is_scanning = False  # Флаг для отслеживания состояния сканирования
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.helius_api_key = HELIUS_API_KEY
        self.last_signature = None
        self.current_mint_address = None
        self.current_url_index = 0  # Индекс текущего RPC URL
        init_db()
        self.log("База данных инициализирована по пути " + os.path.abspath(r"D:\auto\burn\token_transactions.db"))

    def initUI(self):
        self.setWindowTitle("Token Analyzer")
        self.setGeometry(100, 100, 800, 600)
        layout = QVBoxLayout()
        
        # Текстовое поле для логов
        self.textEdit = QTextEdit()
        self.textEdit.setReadOnly(True)
        # Отключаем автоматическую прокрутку
        self.textEdit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.textEdit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        layout.addWidget(self.textEdit)
        
        # Поле для ввода адреса токена
        self.inputField = QLineEdit()
        layout.addWidget(self.inputField)
        
        # Кнопка "Начать анализ" / "Остановить"
        self.startStopButton = QPushButton("Начать анализ")
        self.startStopButton.clicked.connect(self.toggle_analysis)
        layout.addWidget(self.startStopButton)
        
        # Кнопка "Пауза"
        self.pauseButton = QPushButton("Пауза")
        self.pauseButton.clicked.connect(self.toggle_pause)
        layout.addWidget(self.pauseButton)
        
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        
        # Таймер для обновления данных в реальном времени
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_real_time_data)
        
        # Таймер для обновления логов
        self.log_timer = QTimer()
        self.log_timer.timeout.connect(self.flush_logs)
        self.log_timer.start(100)
        self.log_signal.connect(self.append_log)

    @pyqtSlot(str)
    def append_log(self, message):
        self.log_buffer.append(message)

    def flush_logs(self):
        if self.log_buffer:
            for message in self.log_buffer:
                # Добавляем сообщение без автоматической прокрутки
                cursor = self.textEdit.textCursor()
                cursor.movePosition(cursor.End)
                cursor.insertText(message + "\n")
            self.log_buffer.clear()

    def log(self, message):
        self.log_signal.emit(message)

    def toggle_analysis(self):
        if not self.is_scanning:
            # Запускаем анализ
            mint_address = self.inputField.text()
            if not mint_address:
                self.log("Пожалуйста, введите адрес токена.")
                return
            # Валидация адреса токена
            if len(mint_address) != 44 or not mint_address.isalnum():
                self.log("Некорректный адрес токена. Адрес должен быть длиной 44 символа и содержать только буквы и цифры.")
                return
            self.current_mint_address = mint_address
            self.last_signature = None
            self.is_scanning = True
            self.startStopButton.setText("Остановить")
            self.log(f"Запуск анализа для токена {mint_address}...")
            
            # Запускаем анализ в отдельном потоке
            analysis_thread = threading.Thread(target=self.analyze_token, args=(mint_address, 1))
            analysis_thread.daemon = True
            analysis_thread.start()
            
            # Запускаем таймер для обновления данных в реальном времени
            self.timer.start(10000)  # Обновление каждые 10 секунд
        else:
            # Останавливаем анализ
            self.is_scanning = False
            self.paused = False
            self.pause_event.set()  # Снимаем паузу, если она была
            self.timer.stop()  # Останавливаем таймер обновления данных
            self.startStopButton.setText("Начать анализ")
            self.log("Анализ остановлен.")

    def analyze_token(self, mint_address, token_id):
        self.log(f"Начало анализа токена {mint_address} в потоке...")
        total_supply, decimals, symbol = fetch_token_metadata(mint_address, self)
        if decimals is None:
            self.log(f"Токен {mint_address} не поддерживается или не имеет метаданных. Пропускаем анализ.")
            self.is_scanning = False
            self.startStopButton.setText("Начать анализ")
            self.timer.stop()
            return
        self.log(f"Метаданные получены: total_supply={total_supply}, decimals={decimals}, symbol={symbol}")
        
        # Загружаем исторические транзакции
        self.current_url_index = fetch_historical_transactions(mint_address, token_id, self, self.current_url_index)
        self.log(f"Анализ исторических данных для токена {mint_address} завершён.")
        
        # Если сканирование не остановлено, продолжаем в режиме реального времени
        if self.is_scanning:
            self.log("Переход к сбору данных в реальном времени...")

    def toggle_pause(self):
        if self.paused:
            self.paused = False
            self.pause_event.set()
            self.log("Анализ возобновлен.")
        else:
            self.paused = True
            self.pause_event.clear()
            self.log("Анализ приостановлен.")

    def update_real_time_data(self):
        if not self.is_scanning:
            return
        if not self.paused and self.current_mint_address and self.last_signature:
            self.log(f"Обновление данных в реальном времени для {self.current_mint_address}...")
            self.current_url_index = fetch_real_time_transactions(self.current_mint_address, 1, self, self.current_url_index)
        elif not self.current_mint_address:
            self.log("Нет активного токена для обновления в реальном времени.")
        elif not self.last_signature:
            self.log("Ожидание завершения загрузки исторических данных...")

    def closeEvent(self, event):
        close_db()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = TokenAnalyzerApp()
    ex.show()
    sys.exit(app.exec_())