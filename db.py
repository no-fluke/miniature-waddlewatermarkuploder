"""
db.py — MongoDB Auth Database for MRBERLIN Bot
Inspired by ITsGOLU_UPLOADER auth system
"""

import os
import time
import certifi
import colorama
from colorama import Fore, Style
from datetime import datetime, timedelta
from typing import Optional, List
from pymongo import MongoClient, errors
from vars import OWNER, MONGO_URL

colorama.init()

class Database:
    def __init__(self, max_retries: int = 3, retry_delay: float = 2.0):
        self._print_startup()
        self.client = None
        self.db = None
        self.users = None
        self.settings = None
        self.bot_settings = None
        self._connect(max_retries, retry_delay)

    def _print_startup(self):
        print(f"\n{Fore.CYAN}{'='*50}")
        print(f"🤖  MRBERLIN Bot — Database Initialization")
        print(f"{'='*50}{Style.RESET_ALL}\n")

    def _connect(self, max_retries: int, retry_delay: float):
        for attempt in range(1, max_retries + 1):
            try:
                print(f"{Fore.YELLOW}⌛ Attempt {attempt}/{max_retries}: Connecting to MongoDB...{Style.RESET_ALL}")
                self.client = MongoClient(
                    MONGO_URL,
                    serverSelectionTimeoutMS=20000,
                    connectTimeoutMS=20000,
                    socketTimeoutMS=30000,
                    tlsCAFile=certifi.where(),
                    retryWrites=True,
                    retryReads=True
                )
                self.client.server_info()  # test connection

                self.db = self.client["mrberlin_db"]
                self.users = self.db["users"]
                self.settings = self.db["user_settings"]
                self.bot_settings = self.db["bot_settings"]

                print(f"{Fore.GREEN}✓ MongoDB Connected!{Style.RESET_ALL}")
                self._init_db()
                return

            except errors.ServerSelectionTimeoutError as e:
                print(f"{Fore.RED}✕ Attempt {attempt} failed: {e}{Style.RESET_ALL}")
                if attempt < max_retries:
                    time.sleep(retry_delay)
                else:
                    raise ConnectionError(f"MongoDB connection failed after {max_retries} attempts") from e
            except Exception as e:
                print(f"{Fore.RED}✕ Unexpected error: {e}{Style.RESET_ALL}")
                raise

    def _init_db(self):
        print(f"{Fore.YELLOW}⌛ Setting up indexes...{Style.RESET_ALL}")
        try:
            # ── Step 1: Drop old/stale indexes ──────────────────────────────
            existing_indexes = [idx["name"] for idx in self.users.list_indexes()]
            old_indexes = ["user_id_unique"]  # add any future stale indexes here

            for idx in old_indexes:
                if idx in existing_indexes:
                    self.users.drop_index(idx)
                    print(f"{Fore.YELLOW}⚠ Dropped old index: {idx}{Style.RESET_ALL}")

            # ── Step 2: Create correct indexes ──────────────────────────────
            # Unique compound index: one record per (bot_username, user_id)
            self.users.create_index(
                [("bot_username", 1), ("user_id", 1)],
                unique=True,
                name="user_identity"
            )
            self.settings.create_index(
                [("user_id", 1)],
                unique=True,
                name="user_settings_idx"
            )
            # Store per-user channel/topic preferences
            self.bot_settings.create_index(
                [("user_id", 1)],
                unique=True,
                name="user_bot_settings_idx"
            )
            print(f"{Fore.GREEN}✓ Indexes ready!{Style.RESET_ALL}\n")
        except Exception as e:
            print(f"{Fore.YELLOW}⚠ Index warning: {e}{Style.RESET_ALL}")

    # ─── AUTH CHECKS ──────────────────────────────────────────────────────────

    def is_admin(self, user_id: int) -> bool:
        """Owner is always admin."""
        return user_id == OWNER

    def is_user_authorized(self, user_id: int, bot_username: str) -> bool:
        """
        Returns True if:
          - user is owner, OR
          - user has a valid (non-expired) subscription entry
        """
        if user_id == OWNER:
            return True
        try:
            user = self.users.find_one({
                "bot_username": bot_username,
                "user_id": user_id
            })
            if not user:
                return False
            expiry = user.get("expiry_date")
            if expiry and expiry < datetime.now():
                return False
            return True
        except Exception as e:
            print(f"{Fore.RED}Auth check error for {user_id}: {e}{Style.RESET_ALL}")
            return False

    # ─── USER CRUD ────────────────────────────────────────────────────────────

    def add_user(self, user_id: int, name: str, days: int, bot_username: str):
        """
        Add or update a user subscription.
        Returns (success: bool, expiry_date: datetime).
        """
        try:
            expiry = datetime.now() + timedelta(days=days)
            self.users.update_one(
                {"bot_username": bot_username, "user_id": user_id},
                {"$set": {
                    "name": name,
                    "user_id": user_id,
                    "bot_username": bot_username,
                    "expiry_date": expiry,
                    "added_date": datetime.now(),
                    "days": days
                }},
                upsert=True
            )
            return True, expiry
        except Exception as e:
            print(f"{Fore.RED}Add user error for {user_id}: {e}{Style.RESET_ALL}")
            return False, None

    def remove_user(self, user_id: int, bot_username: str) -> bool:
        try:
            result = self.users.delete_one({
                "bot_username": bot_username,
                "user_id": user_id
            })
            return result.deleted_count > 0
        except Exception as e:
            print(f"{Fore.RED}Remove user error for {user_id}: {e}{Style.RESET_ALL}")
            return False

    def get_user(self, user_id: int, bot_username: str) -> Optional[dict]:
        try:
            return self.users.find_one(
                {"bot_username": bot_username, "user_id": user_id},
                {"_id": 0}
            )
        except Exception as e:
            print(f"{Fore.RED}Get user error for {user_id}: {e}{Style.RESET_ALL}")
            return None

    def list_users(self, bot_username: str) -> List[dict]:
        try:
            return list(self.users.find(
                {"bot_username": bot_username},
                {"_id": 0, "name": 1, "user_id": 1, "expiry_date": 1, "days": 1}
            ))
        except Exception as e:
            print(f"{Fore.RED}List users error: {e}{Style.RESET_ALL}")
            return []

    def get_user_expiry_info(self, user_id: int, bot_username: str) -> Optional[dict]:
        user = self.get_user(user_id, bot_username)
        if not user:
            return None
        expiry = user.get("expiry_date")
        if isinstance(expiry, str):
            expiry = datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S")
        days_left = (expiry - datetime.now()).days if expiry else 0
        return {
            "name": user.get("name", "Unknown"),
            "user_id": user_id,
            "expiry_date": expiry.strftime("%d-%m-%Y") if expiry else "N/A",
            "days_left": days_left,
            "added_date": user.get("added_date", "Unknown"),
            "is_active": days_left > 0
        }

    # ─── PER-USER CHANNEL & TOPIC SETTINGS ───────────────────────────────────

    def set_user_channel(self, user_id: int, channel_id: int):
        """Save default channel for a user."""
        try:
            self.bot_settings.update_one(
                {"user_id": user_id},
                {"$set": {"default_channel": channel_id}},
                upsert=True
            )
            return True
        except Exception as e:
            print(f"{Fore.RED}Set channel error: {e}{Style.RESET_ALL}")
            return False

    def get_user_channel(self, user_id: int) -> Optional[int]:
        """Get saved default channel for a user."""
        try:
            doc = self.bot_settings.find_one({"user_id": user_id})
            return doc.get("default_channel") if doc else None
        except Exception as e:
            print(f"{Fore.RED}Get channel error: {e}{Style.RESET_ALL}")
            return None

    def set_user_topic(self, user_id: int, channel_id: int, topic_name: str, topic_id: int):
        """
        Save a topic mapping: channel_id -> topic_name -> topic_id.
        """
        try:
            key = f"topics.{channel_id}.{topic_name}"
            self.bot_settings.update_one(
                {"user_id": user_id},
                {"$set": {key: topic_id}},
                upsert=True
            )
            return True
        except Exception as e:
            print(f"{Fore.RED}Set topic error: {e}{Style.RESET_ALL}")
            return False

    def get_user_topics(self, user_id: int, channel_id: int) -> dict:
        """Return all topic_name -> topic_id mappings for a channel."""
        try:
            doc = self.bot_settings.find_one({"user_id": user_id})
            if not doc:
                return {}
            return doc.get("topics", {}).get(str(channel_id), {})
        except Exception as e:
            print(f"{Fore.RED}Get topics error: {e}{Style.RESET_ALL}")
            return {}

    def remove_user_topic(self, user_id: int, channel_id: int, topic_name: str) -> bool:
        """Remove a saved topic mapping."""
        try:
            key = f"topics.{channel_id}.{topic_name}"
            self.bot_settings.update_one(
                {"user_id": user_id},
                {"$unset": {key: ""}}
            )
            return True
        except Exception as e:
            print(f"{Fore.RED}Remove topic error: {e}{Style.RESET_ALL}")
            return False

    def get_all_saved_channels(self, user_id: int) -> dict:
        """Return all saved topic mappings for all channels of a user."""
        try:
            doc = self.bot_settings.find_one({"user_id": user_id})
            return doc.get("topics", {}) if doc else {}
        except Exception:
            return {}

    # ─── YOUTUBE COOKIES ─────────────────────────────────────────────────────

    def save_cookies(self, cookies_text: str) -> bool:
        """Save YouTube cookies to DB, replacing any existing ones."""
        try:
            self.bot_settings.delete_one({"_id": "youtube_cookies"})
            self.bot_settings.insert_one({
                "_id": "youtube_cookies",
                "cookies": cookies_text,
                "updated_at": datetime.now()
            })
            print(f"{Fore.GREEN}✓ YouTube cookies saved to MongoDB!{Style.RESET_ALL}")
            return True
        except Exception as e:
            print(f"{Fore.RED}Save cookies error: {e}{Style.RESET_ALL}")
            return False

    def get_cookies(self) -> str | None:
        """Retrieve YouTube cookies from DB."""
        try:
            doc = self.bot_settings.find_one({"_id": "youtube_cookies"})
            return doc["cookies"] if doc else None
        except Exception as e:
            print(f"{Fore.RED}Get cookies error: {e}{Style.RESET_ALL}")
            return None

    def delete_cookies(self) -> bool:
        """Delete YouTube cookies from DB."""
        try:
            self.bot_settings.delete_one({"_id": "youtube_cookies"})
            print(f"{Fore.YELLOW}✓ YouTube cookies deleted from MongoDB{Style.RESET_ALL}")
            return True
        except Exception as e:
            print(f"{Fore.RED}Delete cookies error: {e}{Style.RESET_ALL}")
            return False

    # ─── CLEANUP ──────────────────────────────────────────────────────────────

    async def cleanup_expired_users(self, bot, bot_username: str) -> int:
        """Notify and remove expired users."""
        try:
            now = datetime.now()
            expired = self.users.find({
                "bot_username": bot_username,
                "expiry_date": {"$lt": now},
                "user_id": {"$ne": OWNER}
            })
            removed = 0
            for user in expired:
                try:
                    await bot.send_message(
                        user["user_id"],
                        f"**⚠️ Subscription Expired!**\n\n"
                        f"• Name: {user['name']}\n"
                        f"• Expired: {user['expiry_date'].strftime('%d-%m-%Y')}\n\n"
                        f"Contact admin to renew."
                    )
                except Exception:
                    pass
                self.users.delete_one({"_id": user["_id"]})
                removed += 1
            return removed
        except Exception as e:
            print(f"{Fore.RED}Cleanup error: {e}{Style.RESET_ALL}")
            return 0

    def close(self):
        if self.client:
            self.client.close()
            print(f"{Fore.YELLOW}✓ MongoDB connection closed{Style.RESET_ALL}")


# ── Global singleton ──────────────────────────────────────────────────────────
try:
    db = Database(max_retries=3, retry_delay=2)
except Exception as e:
    print(f"{Fore.RED}✕ FATAL: DB init failed → {e}{Style.RESET_ALL}")
    raise
