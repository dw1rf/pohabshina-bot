from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiosqlite


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat()


@dataclass(slots=True, frozen=True)
class EconomyResult:
    ok: bool
    message: str
    amount: int = 0


class EconomyService:
    """Server-local, integer-only economy with an append-only transaction ledger."""

    MIGRATION_VERSION = "economy_v1"

    COOLDOWNS = {
        "daily": timedelta(hours=20),
        "work": timedelta(minutes=45),
        "crime": timedelta(hours=2),
        "mine": timedelta(minutes=30),
        "fish": timedelta(minutes=30),
        "event_collect": timedelta(minutes=20),
    }
    REWARDS = {
        "daily": (180, 320),
        "work": (70, 140),
        "crime": (-90, 260),
        "mine": (45, 125),
        "fish": (40, 115),
    }
    STARTER_ITEMS = (
        (1, "Энергетик", "Сбрасывает cooldown работы", 450, 220, "cooldown:work"),
        (2, "Счастливая монета", "Даёт случайно 100–260 монет", 700, 300, "coins:100:260"),
        (3, "Корм для питомца", "Восстанавливает силы питомца", 180, 80, "pet:food"),
        (4, "Подарочная коробка", "Подарок для другого участника", 350, 150, "gift"),
        (5, "Билет события", "Даёт 25 сезонных жетонов", 900, 420, "event:25"),
    )

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def init_db(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS economy_wallets (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
                bank INTEGER NOT NULL DEFAULT 0 CHECK(bank >= 0),
                xp INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),
                level INTEGER NOT NULL DEFAULT 1 CHECK(level >= 1),
                streak INTEGER NOT NULL DEFAULT 0 CHECK(streak >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS economy_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                bank_amount INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL,
                reference TEXT,
                idempotency_key TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, user_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_economy_ledger_user
                ON economy_transactions (guild_id, user_id, created_at DESC);
            CREATE TRIGGER IF NOT EXISTS economy_ledger_no_update
                BEFORE UPDATE ON economy_transactions BEGIN
                    SELECT RAISE(ABORT, 'economy ledger is append-only');
                END;
            CREATE TRIGGER IF NOT EXISTS economy_ledger_no_delete
                BEFORE DELETE ON economy_transactions BEGIN
                    SELECT RAISE(ABORT, 'economy ledger is append-only');
                END;

            CREATE TABLE IF NOT EXISTS economy_cooldowns (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                available_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, action)
            );

            CREATE TABLE IF NOT EXISTS economy_items (
                item_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                buy_price INTEGER NOT NULL CHECK(buy_price >= 0),
                sell_price INTEGER NOT NULL CHECK(sell_price >= 0),
                effect TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS economy_inventory (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
                PRIMARY KEY (guild_id, user_id, item_id),
                FOREIGN KEY (item_id) REFERENCES economy_items(item_id)
            );

            CREATE TABLE IF NOT EXISTS economy_market (
                listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                seller_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                price INTEGER NOT NULL CHECK(price > 0),
                status TEXT NOT NULL DEFAULT 'active',
                buyer_id INTEGER,
                created_at TEXT NOT NULL,
                closed_at TEXT,
                FOREIGN KEY (item_id) REFERENCES economy_items(item_id)
            );
            CREATE INDEX IF NOT EXISTS idx_economy_market_active
                ON economy_market (guild_id, status, created_at DESC);

            CREATE TABLE IF NOT EXISTS economy_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                config_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS economy_event_progress (
                event_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                tokens INTEGER NOT NULL DEFAULT 0 CHECK(tokens >= 0),
                level INTEGER NOT NULL DEFAULT 1 CHECK(level >= 1),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (event_id, user_id),
                FOREIGN KEY (event_id) REFERENCES economy_events(event_id)
            );

            CREATE TABLE IF NOT EXISTS economy_achievements (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                achievement TEXT NOT NULL,
                unlocked_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, achievement)
            );
            """
        )
        await db.executemany(
            """
            INSERT INTO economy_items (item_id, name, description, buy_price, sell_price, effect)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                name=excluded.name, description=excluded.description,
                buy_price=excluded.buy_price, sell_price=excluded.sell_price,
                effect=excluded.effect
            """,
            self.STARTER_ITEMS,
        )
        await db.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (self.MIGRATION_VERSION, utcnow_iso()),
        )
        await db.commit()

    async def ensure_wallet(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> None:
        now = utcnow_iso()
        await db.execute(
            """
            INSERT OR IGNORE INTO economy_wallets
                (guild_id, user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, user_id, now, now),
        )

    async def wallet(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> aiosqlite.Row:
        await self.ensure_wallet(db, guild_id, user_id)
        await db.commit()
        cursor = await db.execute(
            "SELECT * FROM economy_wallets WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        assert row is not None
        return row

    @staticmethod
    def level_for_xp(xp: int) -> int:
        return max(1, min(300, int((max(xp, 0) / 250) ** 0.5) + 1))

    async def _ledger(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        user_id: int,
        amount: int,
        bank_amount: int,
        reason: str,
        *,
        reference: str | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        try:
            await db.execute(
                """
                INSERT INTO economy_transactions
                    (guild_id, user_id, amount, bank_amount, reason, reference, idempotency_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, amount, bank_amount, reason, reference, idempotency_key, utcnow_iso()),
            )
        except aiosqlite.IntegrityError:
            return False
        return True

    async def change_balance(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        user_id: int,
        amount: int,
        *,
        reason: str,
        xp: int = 0,
        bank_amount: int = 0,
        reference: str | None = None,
        idempotency_key: str | None = None,
    ) -> EconomyResult:
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self.ensure_wallet(db, guild_id, user_id)
                cursor = await db.execute(
                    "SELECT balance, bank, xp FROM economy_wallets WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                row = await cursor.fetchone()
                assert row is not None
                balance = int(row["balance"]) + amount
                bank = int(row["bank"]) + bank_amount
                new_xp = max(0, int(row["xp"]) + xp)
                if balance < 0 or bank < 0:
                    await db.rollback()
                    return EconomyResult(False, "Недостаточно средств.")
                inserted = await self._ledger(
                    db, guild_id, user_id, amount, bank_amount, reason,
                    reference=reference, idempotency_key=idempotency_key,
                )
                if not inserted:
                    await db.rollback()
                    return EconomyResult(False, "Эта операция уже была обработана.")
                level = self.level_for_xp(new_xp)
                await db.execute(
                    """
                    UPDATE economy_wallets
                    SET balance=?, bank=?, xp=?, level=?, updated_at=?
                    WHERE guild_id=? AND user_id=?
                    """,
                    (balance, bank, new_xp, level, utcnow_iso(), guild_id, user_id),
                )
                await db.commit()
                return EconomyResult(True, "Операция выполнена.", amount)
            except Exception:
                await db.rollback()
                raise

    async def claim_action(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        user_id: int,
        action: str,
        *,
        idempotency_key: str,
    ) -> EconomyResult:
        if action not in self.COOLDOWNS or action not in self.REWARDS:
            return EconomyResult(False, "Неизвестное экономическое действие.")
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT available_at FROM economy_cooldowns WHERE guild_id=? AND user_id=? AND action=?",
                    (guild_id, user_id, action),
                )
                cooldown = await cursor.fetchone()
                now = utcnow()
                if cooldown:
                    available = datetime.fromisoformat(str(cooldown["available_at"]))
                    if available > now:
                        await db.rollback()
                        seconds = int((available - now).total_seconds())
                        return EconomyResult(False, f"Действие будет доступно через {seconds // 60 + 1} мин.")
                await self.ensure_wallet(db, guild_id, user_id)
                low, high = self.REWARDS[action]
                amount = random.randint(low, high)
                cursor = await db.execute(
                    "SELECT balance, xp FROM economy_wallets WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                wallet = await cursor.fetchone()
                assert wallet is not None
                if action == "crime" and amount < 0:
                    amount = -min(int(wallet["balance"]), abs(amount))
                xp_gain = random.randint(8, 18)
                inserted = await self._ledger(
                    db, guild_id, user_id, amount, 0, action,
                    idempotency_key=idempotency_key,
                )
                if not inserted:
                    await db.rollback()
                    return EconomyResult(False, "Эта награда уже была обработана.")
                new_xp = int(wallet["xp"]) + xp_gain
                await db.execute(
                    """
                    UPDATE economy_wallets
                    SET balance=balance+?, xp=?, level=?,
                        streak=CASE WHEN ?='daily' THEN streak+1 ELSE streak END,
                        updated_at=?
                    WHERE guild_id=? AND user_id=?
                    """,
                    (amount, new_xp, self.level_for_xp(new_xp), action, utcnow_iso(), guild_id, user_id),
                )
                await db.execute(
                    """
                    INSERT INTO economy_cooldowns (guild_id, user_id, action, available_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, action)
                    DO UPDATE SET available_at=excluded.available_at
                    """,
                    (guild_id, user_id, action, (now + self.COOLDOWNS[action]).isoformat()),
                )
                await db.commit()
                verb = "потеряно" if amount < 0 else "получено"
                return EconomyResult(True, f"{verb.capitalize()} {abs(amount)} монет и {xp_gain} XP.", amount)
            except Exception:
                await db.rollback()
                raise

    async def transfer(
        self,
        db: aiosqlite.Connection,
        guild_id: int,
        sender_id: int,
        recipient_id: int,
        amount: int,
        *,
        idempotency_key: str,
    ) -> EconomyResult:
        if amount <= 0 or sender_id == recipient_id:
            return EconomyResult(False, "Укажите положительную сумму и другого участника.")
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self.ensure_wallet(db, guild_id, sender_id)
                await self.ensure_wallet(db, guild_id, recipient_id)
                cursor = await db.execute(
                    "SELECT balance FROM economy_wallets WHERE guild_id=? AND user_id=?",
                    (guild_id, sender_id),
                )
                sender = await cursor.fetchone()
                if sender is None or int(sender["balance"]) < amount:
                    await db.rollback()
                    return EconomyResult(False, "Недостаточно монет.")
                if not await self._ledger(db, guild_id, sender_id, -amount, 0, "transfer_out", reference=str(recipient_id), idempotency_key=idempotency_key):
                    await db.rollback()
                    return EconomyResult(False, "Этот перевод уже обработан.")
                await self._ledger(db, guild_id, recipient_id, amount, 0, "transfer_in", reference=str(sender_id), idempotency_key=f"{idempotency_key}:in")
                await db.execute("UPDATE economy_wallets SET balance=balance-?, updated_at=? WHERE guild_id=? AND user_id=?", (amount, utcnow_iso(), guild_id, sender_id))
                await db.execute("UPDATE economy_wallets SET balance=balance+?, updated_at=? WHERE guild_id=? AND user_id=?", (amount, utcnow_iso(), guild_id, recipient_id))
                await db.commit()
                return EconomyResult(True, f"Переведено {amount} монет.", amount)
            except Exception:
                await db.rollback()
                raise

    async def bank_move(self, db: aiosqlite.Connection, guild_id: int, user_id: int, amount: int, *, deposit: bool, idempotency_key: str) -> EconomyResult:
        if amount <= 0:
            return EconomyResult(False, "Сумма должна быть положительной.")
        wallet = await self.wallet(db, guild_id, user_id)
        if deposit and int(wallet["balance"]) < amount:
            return EconomyResult(False, "Недостаточно монет в кошельке.")
        if not deposit and int(wallet["bank"]) < amount:
            return EconomyResult(False, "Недостаточно монет в банке.")
        return await self.change_balance(
            db, guild_id, user_id,
            -amount if deposit else amount,
            bank_amount=amount if deposit else -amount,
            reason="bank_deposit" if deposit else "bank_withdraw",
            idempotency_key=idempotency_key,
        )

    async def inventory(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> list[aiosqlite.Row]:
        cursor = await db.execute(
            """
            SELECT i.item_id, i.name, i.description, i.effect, inv.quantity
            FROM economy_inventory inv
            JOIN economy_items i ON i.item_id=inv.item_id
            WHERE inv.guild_id=? AND inv.user_id=? AND inv.quantity>0
            ORDER BY i.item_id
            """,
            (guild_id, user_id),
        )
        return list(await cursor.fetchall())

    async def add_item(self, db: aiosqlite.Connection, guild_id: int, user_id: int, item_id: int, quantity: int) -> bool:
        if quantity == 0:
            return True
        cursor = await db.execute("SELECT quantity FROM economy_inventory WHERE guild_id=? AND user_id=? AND item_id=?", (guild_id, user_id, item_id))
        row = await cursor.fetchone()
        current = int(row["quantity"] if row else 0)
        if current + quantity < 0:
            return False
        if row is None:
            await db.execute(
                "INSERT INTO economy_inventory (guild_id, user_id, item_id, quantity) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, item_id, quantity),
            )
        else:
            await db.execute(
                "UPDATE economy_inventory SET quantity=? WHERE guild_id=? AND user_id=? AND item_id=?",
                (current + quantity, guild_id, user_id, item_id),
            )
        return True

    async def shop_items(self, db: aiosqlite.Connection) -> list[aiosqlite.Row]:
        cursor = await db.execute("SELECT * FROM economy_items WHERE enabled=1 ORDER BY item_id")
        return list(await cursor.fetchall())

    async def shop_buy(self, db: aiosqlite.Connection, guild_id: int, user_id: int, item_id: int, quantity: int, *, idempotency_key: str) -> EconomyResult:
        if quantity <= 0:
            return EconomyResult(False, "Количество должно быть положительным.")
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("SELECT * FROM economy_items WHERE item_id=? AND enabled=1", (item_id,))
                item = await cursor.fetchone()
                if item is None:
                    await db.rollback()
                    return EconomyResult(False, "Предмет не найден.")
                await self.ensure_wallet(db, guild_id, user_id)
                price = int(item["buy_price"]) * quantity
                cursor = await db.execute("SELECT balance FROM economy_wallets WHERE guild_id=? AND user_id=?", (guild_id, user_id))
                wallet = await cursor.fetchone()
                if wallet is None or int(wallet["balance"]) < price:
                    await db.rollback()
                    return EconomyResult(False, "Недостаточно монет.")
                if not await self._ledger(db, guild_id, user_id, -price, 0, "shop_buy", reference=str(item_id), idempotency_key=idempotency_key):
                    await db.rollback()
                    return EconomyResult(False, "Покупка уже обработана.")
                await db.execute("UPDATE economy_wallets SET balance=balance-?, updated_at=? WHERE guild_id=? AND user_id=?", (price, utcnow_iso(), guild_id, user_id))
                await self.add_item(db, guild_id, user_id, item_id, quantity)
                await db.commit()
                return EconomyResult(True, f"Куплено: {item['name']} ×{quantity} за {price} монет.", -price)
            except Exception:
                await db.rollback()
                raise

    async def shop_sell(self, db: aiosqlite.Connection, guild_id: int, user_id: int, item_id: int, quantity: int, *, idempotency_key: str) -> EconomyResult:
        if quantity <= 0:
            return EconomyResult(False, "Количество должно быть положительным.")
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("SELECT * FROM economy_items WHERE item_id=? AND enabled=1", (item_id,))
                item = await cursor.fetchone()
                if item is None or not await self.add_item(db, guild_id, user_id, item_id, -quantity):
                    await db.rollback()
                    return EconomyResult(False, "Недостаточно предметов или предмет не найден.")
                await self.ensure_wallet(db, guild_id, user_id)
                reward = int(item["sell_price"]) * quantity
                if not await self._ledger(db, guild_id, user_id, reward, 0, "shop_sell", reference=str(item_id), idempotency_key=idempotency_key):
                    await db.rollback()
                    return EconomyResult(False, "Продажа уже обработана.")
                await db.execute("UPDATE economy_wallets SET balance=balance+?, updated_at=? WHERE guild_id=? AND user_id=?", (reward, utcnow_iso(), guild_id, user_id))
                await db.commit()
                return EconomyResult(True, f"Продано: {item['name']} ×{quantity} за {reward} монет.", reward)
            except Exception:
                await db.rollback()
                raise

    async def use_item(self, db: aiosqlite.Connection, guild_id: int, user_id: int, item_id: int) -> EconomyResult:
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT i.* FROM economy_items i
                    JOIN economy_inventory inv ON inv.item_id=i.item_id
                    WHERE inv.guild_id=? AND inv.user_id=? AND inv.item_id=? AND inv.quantity>0
                    """,
                    (guild_id, user_id, item_id),
                )
                item = await cursor.fetchone()
                if item is None:
                    await db.rollback()
                    return EconomyResult(False, "Предмета нет в инвентаре.")
                effect = str(item["effect"])
                amount = 0
                if effect.startswith("coins:"):
                    _, low, high = effect.split(":")
                    amount = random.randint(int(low), int(high))
                    await self.ensure_wallet(db, guild_id, user_id)
                    await db.execute("UPDATE economy_wallets SET balance=balance+?, updated_at=? WHERE guild_id=? AND user_id=?", (amount, utcnow_iso(), guild_id, user_id))
                    await self._ledger(db, guild_id, user_id, amount, 0, "item_use", reference=str(item_id))
                elif effect.startswith("cooldown:"):
                    action = effect.split(":", 1)[1]
                    await db.execute("DELETE FROM economy_cooldowns WHERE guild_id=? AND user_id=? AND action=?", (guild_id, user_id, action))
                elif effect.startswith("event:"):
                    event = await self.active_event(db, guild_id)
                    if event is None:
                        await db.rollback()
                        return EconomyResult(False, "Нет активного события, предмет не потрачен.")
                    tokens = int(effect.split(":", 1)[1])
                    await db.execute(
                        """
                        INSERT INTO economy_event_progress (event_id, guild_id, user_id, tokens, level, updated_at)
                        VALUES (?, ?, ?, ?, 1, ?)
                        ON CONFLICT(event_id, user_id) DO UPDATE SET tokens=tokens+excluded.tokens, updated_at=excluded.updated_at
                        """,
                        (int(event["event_id"]), guild_id, user_id, tokens, utcnow_iso()),
                    )
                elif effect == "pet:food":
                    cursor = await db.execute("SELECT 1 FROM pets WHERE guild_id=? AND owner_id=?", (guild_id, user_id))
                    if await cursor.fetchone() is None:
                        await db.rollback()
                        return EconomyResult(False, "Сначала создайте питомца, предмет не потрачен.")
                    await db.execute(
                        """UPDATE pets SET hunger=MIN(100, hunger+35), health=MIN(100, health+10),
                           energy=MIN(100, energy+10), updated_at=? WHERE guild_id=? AND owner_id=?""",
                        (utcnow_iso(), guild_id, user_id),
                    )
                elif effect == "gift":
                    await db.rollback()
                    return EconomyResult(False, "Эту коробку нужно передать через `/inventory gift`.")
                await self.add_item(db, guild_id, user_id, item_id, -1)
                await db.commit()
                suffix = f" Получено {amount} монет." if amount else ""
                return EconomyResult(True, f"Использован предмет «{item['name']}».{suffix}", amount)
            except Exception:
                await db.rollback()
                raise

    async def market_create(self, db: aiosqlite.Connection, guild_id: int, seller_id: int, item_id: int, quantity: int, price: int) -> EconomyResult:
        if quantity <= 0 or price <= 0:
            return EconomyResult(False, "Количество и цена должны быть положительными.")
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if not await self.add_item(db, guild_id, seller_id, item_id, -quantity):
                    await db.rollback()
                    return EconomyResult(False, "Недостаточно предметов.")
                cursor = await db.execute(
                    "INSERT INTO economy_market (guild_id, seller_id, item_id, quantity, price, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (guild_id, seller_id, item_id, quantity, price, utcnow_iso()),
                )
                await db.commit()
                return EconomyResult(True, f"Лот #{int(cursor.lastrowid or 0)} создан за {price} монет.")
            except Exception:
                await db.rollback()
                raise

    async def market_list(self, db: aiosqlite.Connection, guild_id: int, limit: int = 20) -> list[aiosqlite.Row]:
        cursor = await db.execute(
            """
            SELECT m.*, i.name FROM economy_market m
            JOIN economy_items i ON i.item_id=m.item_id
            WHERE m.guild_id=? AND m.status='active'
            ORDER BY m.created_at DESC LIMIT ?
            """,
            (guild_id, limit),
        )
        return list(await cursor.fetchall())

    async def market_buy(self, db: aiosqlite.Connection, guild_id: int, buyer_id: int, listing_id: int, *, idempotency_key: str) -> EconomyResult:
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("SELECT * FROM economy_market WHERE guild_id=? AND listing_id=? AND status='active'", (guild_id, listing_id))
                listing = await cursor.fetchone()
                if listing is None:
                    await db.rollback()
                    return EconomyResult(False, "Активный лот не найден.")
                seller_id = int(listing["seller_id"])
                if seller_id == buyer_id:
                    await db.rollback()
                    return EconomyResult(False, "Нельзя купить собственный лот.")
                await self.ensure_wallet(db, guild_id, buyer_id)
                await self.ensure_wallet(db, guild_id, seller_id)
                price = int(listing["price"])
                cursor = await db.execute("SELECT balance FROM economy_wallets WHERE guild_id=? AND user_id=?", (guild_id, buyer_id))
                buyer = await cursor.fetchone()
                if buyer is None or int(buyer["balance"]) < price:
                    await db.rollback()
                    return EconomyResult(False, "Недостаточно монет.")
                if not await self._ledger(db, guild_id, buyer_id, -price, 0, "market_buy", reference=str(listing_id), idempotency_key=idempotency_key):
                    await db.rollback()
                    return EconomyResult(False, "Покупка уже обработана.")
                await self._ledger(db, guild_id, seller_id, price, 0, "market_sell", reference=str(listing_id), idempotency_key=f"{idempotency_key}:seller")
                await db.execute("UPDATE economy_wallets SET balance=balance-?, updated_at=? WHERE guild_id=? AND user_id=?", (price, utcnow_iso(), guild_id, buyer_id))
                await db.execute("UPDATE economy_wallets SET balance=balance+?, updated_at=? WHERE guild_id=? AND user_id=?", (price, utcnow_iso(), guild_id, seller_id))
                await self.add_item(db, guild_id, buyer_id, int(listing["item_id"]), int(listing["quantity"]))
                await db.execute("UPDATE economy_market SET status='sold', buyer_id=?, closed_at=? WHERE listing_id=?", (buyer_id, utcnow_iso(), listing_id))
                await db.commit()
                return EconomyResult(True, f"Лот #{listing_id} куплен за {price} монет.", -price)
            except Exception:
                await db.rollback()
                raise

    async def market_cancel(self, db: aiosqlite.Connection, guild_id: int, seller_id: int, listing_id: int) -> EconomyResult:
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("SELECT * FROM economy_market WHERE guild_id=? AND listing_id=? AND seller_id=? AND status='active'", (guild_id, listing_id, seller_id))
                listing = await cursor.fetchone()
                if listing is None:
                    await db.rollback()
                    return EconomyResult(False, "Ваш активный лот не найден.")
                await self.add_item(db, guild_id, seller_id, int(listing["item_id"]), int(listing["quantity"]))
                await db.execute("UPDATE economy_market SET status='cancelled', closed_at=? WHERE listing_id=?", (utcnow_iso(), listing_id))
                await db.commit()
                return EconomyResult(True, f"Лот #{listing_id} отменён, предметы возвращены.")
            except Exception:
                await db.rollback()
                raise

    async def gift_item(self, db: aiosqlite.Connection, guild_id: int, sender_id: int, recipient_id: int, item_id: int, quantity: int) -> EconomyResult:
        if quantity <= 0 or sender_id == recipient_id:
            return EconomyResult(False, "Укажите положительное количество и другого участника.")
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if not await self.add_item(db, guild_id, sender_id, item_id, -quantity):
                    await db.rollback()
                    return EconomyResult(False, "Недостаточно предметов.")
                await self.add_item(db, guild_id, recipient_id, item_id, quantity)
                await db.commit()
                return EconomyResult(True, f"Передано предметов: {quantity}.")
            except Exception:
                await db.rollback()
                raise

    async def claim_basic_quest(self, db: aiosqlite.Connection, guild_id: int, user_id: int, quest: str) -> EconomyResult:
        definitions = {"earn": ("daily_earner", 3, 250), "social": ("social_player", 5, 180)}
        if quest not in definitions:
            return EconomyResult(False, "Неизвестное задание.")
        achievement, required, reward = definitions[quest]
        cursor = await db.execute("SELECT 1 FROM economy_achievements WHERE guild_id=? AND user_id=? AND achievement=?", (guild_id, user_id, achievement))
        if await cursor.fetchone():
            return EconomyResult(False, "Награда за это задание уже получена.")
        if quest == "earn":
            cursor = await db.execute("SELECT COUNT(*) AS n FROM economy_transactions WHERE guild_id=? AND user_id=? AND amount>0", (guild_id, user_id))
        else:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM economy_transactions WHERE guild_id=? AND user_id=? AND reason IN ('transfer_out','transfer_in')", (guild_id, user_id))
        row = await cursor.fetchone()
        progress = int(row["n"] if row else 0)
        if progress < required:
            return EconomyResult(False, f"Прогресс: {progress}/{required}.")
        result = await self.change_balance(db, guild_id, user_id, reward, reason="quest_reward", xp=40, idempotency_key=f"quest:{achievement}")
        if result.ok:
            await db.execute("INSERT OR IGNORE INTO economy_achievements (guild_id, user_id, achievement, unlocked_at) VALUES (?, ?, ?, ?)", (guild_id, user_id, achievement, utcnow_iso()))
            await db.commit()
            return EconomyResult(True, f"Задание завершено: +{reward} монет и 40 XP.", reward)
        return result

    async def top(self, db: aiosqlite.Connection, guild_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        cursor = await db.execute(
            """
            SELECT user_id, balance, bank, xp, level, balance+bank AS wealth
            FROM economy_wallets WHERE guild_id=?
            ORDER BY wealth DESC, xp DESC, user_id ASC LIMIT ?
            """,
            (guild_id, limit),
        )
        return list(await cursor.fetchall())

    async def cooldowns(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> dict[str, int]:
        cursor = await db.execute("SELECT action, available_at FROM economy_cooldowns WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        now = utcnow()
        result: dict[str, int] = {}
        for row in await cursor.fetchall():
            remaining = int((datetime.fromisoformat(str(row["available_at"])) - now).total_seconds())
            if remaining > 0:
                result[str(row["action"])] = remaining
        return result

    async def start_event(self, db: aiosqlite.Connection, guild_id: int, name: str, days: int = 30) -> int:
        await db.execute("UPDATE economy_events SET active=0 WHERE guild_id=? AND active=1", (guild_id,))
        start = utcnow()
        cursor = await db.execute(
            "INSERT INTO economy_events (guild_id, name, starts_at, ends_at, config_json) VALUES (?, ?, ?, ?, ?)",
            (guild_id, name[:80], start.isoformat(), (start + timedelta(days=max(1, min(days, 90)))).isoformat(), json.dumps({"rewards": [100, 250, 500]}, ensure_ascii=False)),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)

    async def active_event(self, db: aiosqlite.Connection, guild_id: int) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM economy_events WHERE guild_id=? AND active=1 AND ends_at>? ORDER BY event_id DESC LIMIT 1",
            (guild_id, utcnow_iso()),
        )
        return await cursor.fetchone()

    async def collect_event(self, db: aiosqlite.Connection, guild_id: int, user_id: int, *, idempotency_key: str) -> EconomyResult:
        async with self._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                event = await self.active_event(db, guild_id)
                if event is None:
                    await db.rollback()
                    return EconomyResult(False, "Сейчас нет активного события.")
                action = await self.claim_action_event_cooldown(db, guild_id, user_id)
                if not action.ok:
                    await db.rollback()
                    return action
                inserted = await self._ledger(
                    db, guild_id, user_id, 0, 0, "event_collect",
                    reference=str(event["event_id"]), idempotency_key=idempotency_key,
                )
                if not inserted:
                    await db.rollback()
                    return EconomyResult(False, "Эта награда уже была обработана.")
                tokens = random.randint(5, 15)
                await db.execute(
                    """
                    INSERT INTO economy_event_progress (event_id, guild_id, user_id, tokens, level, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(event_id, user_id) DO UPDATE SET
                        tokens=tokens+excluded.tokens,
                        level=MIN(50, 1 + CAST((tokens+excluded.tokens)/50 AS INTEGER)),
                        updated_at=excluded.updated_at
                    """,
                    (int(event["event_id"]), guild_id, user_id, tokens, utcnow_iso()),
                )
                await db.commit()
                return EconomyResult(True, f"Собрано {tokens} сезонных жетонов.", tokens)
            except Exception:
                await db.rollback()
                raise

    async def claim_action_event_cooldown(self, db: aiosqlite.Connection, guild_id: int, user_id: int) -> EconomyResult:
        cursor = await db.execute("SELECT available_at FROM economy_cooldowns WHERE guild_id=? AND user_id=? AND action='event_collect'", (guild_id, user_id))
        row = await cursor.fetchone()
        now = utcnow()
        if row and datetime.fromisoformat(str(row["available_at"])) > now:
            seconds = int((datetime.fromisoformat(str(row["available_at"])) - now).total_seconds())
            return EconomyResult(False, f"Следующий сбор через {seconds // 60 + 1} мин.")
        await db.execute(
            """
            INSERT INTO economy_cooldowns (guild_id, user_id, action, available_at)
            VALUES (?, ?, 'event_collect', ?)
            ON CONFLICT(guild_id, user_id, action) DO UPDATE SET available_at=excluded.available_at
            """,
            (guild_id, user_id, (now + self.COOLDOWNS["event_collect"]).isoformat()),
        )
        return EconomyResult(True, "OK")

    async def event_top(self, db: aiosqlite.Connection, guild_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        event = await self.active_event(db, guild_id)
        if event is None:
            return []
        cursor = await db.execute(
            "SELECT user_id, tokens, level FROM economy_event_progress WHERE event_id=? ORDER BY tokens DESC, user_id ASC LIMIT ?",
            (int(event["event_id"]), limit),
        )
        return list(await cursor.fetchall())
