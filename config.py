# config.py
from dotenv import load_dotenv
import os

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "D:/auto/burn/token_transactions.db")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "8b65d9ce-4f9b-4b90-9e4c-af088de240b2")