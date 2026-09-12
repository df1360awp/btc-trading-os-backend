"""Shared, server-side paper execution for USER and SYSTEM strategies."""
import hashlib
import json
import os
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, localcontext
from uuid import uuid4

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

DB_PATH = "/opt/btc-trading-os/market.db"
Q = Decimal("0.00000001")
ZERO = Decimal("0")


def amount(value):
    with localcontext() as ctx:
        ctx.prec = 50
        return format(Decimal(str(value)).quantize(Q, rounding=ROUND_HALF_EVEN), "f")


def now_ms():
    return time.time_ns() // 1_000_000


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_paper_tables(db_path=DB_PATH):
    with connect(db_path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS paper_accounts (
          id TEXT PRIMARY KEY, strategy_type TEXT NOT NULL CHECK(strategy_type IN ('USER','SYSTEM')),
          strategy_id TEXT NOT NULL, initial_balance TEXT NOT NULL, balance TEXT NOT NULL,
          fee_bps TEXT NOT NULL, slippage_bps TEXT NOT NULL, created_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_orders (
          id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES paper_accounts(id),
          kind TEXT NOT NULL CHECK(kind IN ('ENTRY','EXIT')), status TEXT NOT NULL,
          request TEXT NOT NULL, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
          position_id TEXT, fill_price TEXT, quantity TEXT, fee TEXT, reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_paper_orders_account ON paper_orders(account_id, created_ms);
        CREATE TABLE IF NOT EXISTS paper_positions (
          id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES paper_accounts(id),
          entry_order_id TEXT NOT NULL UNIQUE REFERENCES paper_orders(id), direction TEXT NOT NULL CHECK(direction IN ('LONG','SHORT')),
          quantity TEXT NOT NULL, entry_price TEXT NOT NULL, entry_fee TEXT NOT NULL, margin TEXT NOT NULL,
          stop_loss TEXT, take_profit TEXT, opened_ms INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
          exit_order_id TEXT UNIQUE REFERENCES paper_orders(id), exit_price TEXT, exit_fee TEXT,
          closed_ms INTEGER, profit_loss TEXT, reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_paper_positions_account ON paper_positions(account_id,status);
        CREATE TABLE IF NOT EXISTS paper_equity (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL REFERENCES paper_accounts(id),
          timestamp_ms INTEGER NOT NULL, balance TEXT NOT NULL, unrealized_pnl TEXT NOT NULL, equity TEXT NOT NULL, event TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_paper_equity_account ON paper_equity(account_id,sequence);
        CREATE TABLE IF NOT EXISTS paper_idempotency (
          scope TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(scope,key)
        );
        CREATE TABLE IF NOT EXISTS paper_strategies (
          id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES paper_accounts(id), route TEXT NOT NULL CHECK(route IN ('USER','SYSTEM')),
          enabled INTEGER NOT NULL DEFAULT 1, definition TEXT NOT NULL, state TEXT NOT NULL DEFAULT '{}', created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_paper_strategies_enabled ON paper_strategies(route,enabled);
        CREATE TABLE IF NOT EXISTS paper_strategy_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL REFERENCES paper_strategies(id), position_id TEXT,
          event TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, market_context TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_paper_strategy_events ON paper_strategy_events(strategy_id,timestamp_ms);
        """)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AccountRequest(RequestModel):
    account_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    strategy_type: str = Field(pattern=r"^(USER|SYSTEM)$")
    strategy_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    initial_balance: Decimal = Field(default=Decimal("10000"), gt=0, max_digits=24, decimal_places=8)
    fee_bps: Decimal = Field(default=Decimal("4"), ge=0, le=100, decimal_places=4)
    slippage_bps: Decimal = Field(default=Decimal("2"), ge=0, le=100, decimal_places=4)


class EntryRequest(RequestModel):
    direction: str = Field(pattern=r"^(LONG|SHORT)$")
    quantity: Decimal | None = Field(default=None, gt=0, le=Decimal("1000000"), max_digits=16, decimal_places=8)
    equity_fraction: Decimal | None = Field(default=None, gt=0, le=1, max_digits=9, decimal_places=8)
    risk_fraction: Decimal | None = Field(default=None, gt=0, le=1, max_digits=9, decimal_places=8)
    stop_loss: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    take_profit: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)

    @model_validator(mode="after")
    def check_sizing(self):
        if sum(v is not None for v in (self.quantity, self.equity_fraction, self.risk_fraction)) != 1:
            raise ValueError("Specify exactly one of quantity, equity_fraction, or risk_fraction")
        if self.risk_fraction is not None and self.stop_loss is None:
            raise ValueError("risk_fraction requires stop_loss")
        return self


class ProtectionRequest(RequestModel):
    stop_loss: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    take_profit: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)


class PaperError(Exception):
    def __init__(self, code, detail, status=409):
        self.code, self.detail, self.status = code, detail, status


class PaperEngine:
    def __init__(self, db_path=DB_PATH, clock=now_ms):
        self.db_path, self.clock = db_path, clock
        init_paper_tables(db_path)

    def _account(self, db, account_id):
        row = db.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if not row: raise PaperError("NOT_FOUND", "Paper account not found", 404)
        return row

    def _position(self, db, account_id, position_id):
        row = db.execute("SELECT * FROM paper_positions WHERE id=? AND account_id=?", (position_id, account_id)).fetchone()
        if not row: raise PaperError("NOT_FOUND", "Position not found", 404)
        if row["status"] != "OPEN": raise PaperError("POSITION_CLOSED", "Position is already closed")
        return row

    def _tx(self):
        db = connect(self.db_path); db.execute("BEGIN IMMEDIATE")
        return db

    def _value(self, db, account_id, mark_price):
        account = self._account(db, account_id); unrealized = margin = ZERO
        for p in db.execute("SELECT * FROM paper_positions WHERE account_id=? AND status='OPEN'", (account_id,)):
            sign = Decimal(1) if p["direction"] == "LONG" else Decimal(-1)
            unrealized += sign * (mark_price - Decimal(p["entry_price"])) * Decimal(p["quantity"])
            margin += Decimal(p["margin"])
        balance = Decimal(account["balance"]); equity = balance + unrealized
        return {"account_id": account_id, "strategy_type": account["strategy_type"], "strategy_id": account["strategy_id"],
                "currency": "USDC", "balance": amount(balance), "realized_pnl": amount(balance - Decimal(account["initial_balance"])),
                "unrealized_pnl": amount(unrealized), "equity": amount(equity), "reserved_margin": amount(margin),
                "available_balance": amount(max(ZERO, min(balance, equity) - margin)), "mark_price": amount(mark_price)}

    def _snapshot(self, db, account_id, mark_price, event, timestamp):
        value = self._value(db, account_id, mark_price)
        db.execute("INSERT INTO paper_equity(account_id,timestamp_ms,balance,unrealized_pnl,equity,event) VALUES(?,?,?,?,?,?)",
                   (account_id,timestamp,value["balance"],value["unrealized_pnl"],value["equity"],event))

    @staticmethod
    def _guard_protection(direction, reference, stop, take):
        valid = (direction == "LONG" and (stop is None or stop < reference) and (take is None or take > reference)) or (direction == "SHORT" and (stop is None or stop > reference) and (take is None or take < reference))
        if not valid: raise PaperError("INVALID_PROTECTION", "Stop-loss/take-profit are on the wrong side of the price", 422)

    @staticmethod
    def _fill(account, direction, price, closing=False):
        buy = (direction == "LONG") != closing
        slip = Decimal(account["slippage_bps"]) / Decimal("10000")
        return Decimal(amount(price * (1 + slip if buy else 1 - slip)))

    def _receipt(self, db, scope, key, payload, action):
        fingerprint = hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
        old = db.execute("SELECT * FROM paper_idempotency WHERE scope=? AND key=?", (scope,key)).fetchone()
        if old:
            if old["fingerprint"] != fingerprint: raise PaperError("IDEMPOTENCY_CONFLICT", "Idempotency key was used for another request")
            return json.loads(old["response"])
        result = action(); db.execute("INSERT INTO paper_idempotency VALUES(?,?,?,?)", (scope,key,fingerprint,json.dumps(result)))
        return result

    def create_account(self, request, key, mark_price):
        request = AccountRequest.model_validate(request)
        db = self._tx()
        try:
            def action():
                if db.execute("SELECT 1 FROM paper_accounts WHERE id=?", (request.account_id,)).fetchone(): raise PaperError("ACCOUNT_EXISTS", "Account already exists")
                db.execute("INSERT INTO paper_accounts VALUES(?,?,?,?,?,?,?,?)", (request.account_id,request.strategy_type,request.strategy_id,amount(request.initial_balance),amount(request.initial_balance),str(request.fee_bps),str(request.slippage_bps),self.clock()))
                self._snapshot(db,request.account_id,mark_price,"ACCOUNT_CREATED",self.clock()); return self._value(db,request.account_id,mark_price)
            result=self._receipt(db,"accounts",key,request.model_dump(mode="json"),action); db.commit(); return result
        except Exception: db.rollback(); raise
        finally: db.close()

    def enter(self, account_id, request, key, mark_price):
        request = EntryRequest.model_validate(request); db=self._tx()
        try:
            def action():
                account=self._account(db,account_id); fill=self._fill(account,request.direction,mark_price); self._guard_protection(request.direction,fill,request.stop_loss,request.take_profit)
                value=self._value(db,account_id,mark_price); fee_rate=Decimal(account["fee_bps"])/Decimal("10000")
                if request.quantity is not None: quantity=request.quantity
                elif request.equity_fraction is not None: quantity=Decimal(value["equity"])*request.equity_fraction/(fill*(1+fee_rate))
                else:
                    stop_fill=self._fill(account,request.direction,request.stop_loss,True); unit=abs(fill-stop_fill)+(fill+stop_fill)*fee_rate; quantity=Decimal(value["equity"])*request.risk_fraction/unit
                quantity=quantity.quantize(Q,rounding=ROUND_DOWN); margin=fill*quantity; fee=margin*fee_rate
                order_id=str(uuid4())
                db.execute("INSERT INTO paper_orders(id,account_id,kind,status,request,created_ms,updated_ms) VALUES(?,?,'ENTRY','PENDING',?,?,?)",(order_id,account_id,request.model_dump_json(),self.clock(),self.clock()))
                if quantity<=0 or margin+fee>Decimal(value["available_balance"]):
                    db.execute("UPDATE paper_orders SET status='REJECTED',reason='INSUFFICIENT_FUNDS',updated_ms=? WHERE id=?",(self.clock(),order_id)); return dict(db.execute("SELECT * FROM paper_orders WHERE id=?",(order_id,)).fetchone())
                position_id=str(uuid4()); db.execute("INSERT INTO paper_positions(id,account_id,entry_order_id,direction,quantity,entry_price,entry_fee,margin,stop_loss,take_profit,opened_ms,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'OPEN')",(position_id,account_id,order_id,request.direction,amount(quantity),amount(fill),amount(fee),amount(margin),amount(request.stop_loss) if request.stop_loss else None,amount(request.take_profit) if request.take_profit else None,self.clock()))
                db.execute("UPDATE paper_accounts SET balance=? WHERE id=?",(amount(Decimal(account["balance"])-fee),account_id)); db.execute("UPDATE paper_orders SET status='FILLED',position_id=?,fill_price=?,quantity=?,fee=?,updated_ms=? WHERE id=?",(position_id,amount(fill),amount(quantity),amount(fee),self.clock(),order_id)); self._snapshot(db,account_id,mark_price,"ENTRY",self.clock()); return dict(db.execute("SELECT * FROM paper_orders WHERE id=?",(order_id,)).fetchone())
            result=self._receipt(db,account_id,key,{"entry":request.model_dump(mode="json")},action); db.commit(); return result
        except Exception: db.rollback(); raise
        finally: db.close()

    def _close(self,db,position,mark_price,reason,timestamp):
        account=self._account(db,position["account_id"]); fill=self._fill(account,position["direction"],mark_price,True); quantity=Decimal(position["quantity"]); fee=fill*quantity*Decimal(account["fee_bps"])/Decimal("10000"); sign=1 if position["direction"]=="LONG" else -1; net=sign*(fill-Decimal(position["entry_price"]))*quantity-Decimal(position["entry_fee"])-fee; order_id=str(uuid4())
        db.execute("INSERT INTO paper_orders(id,account_id,kind,status,request,created_ms,updated_ms,position_id,fill_price,quantity,fee,reason) VALUES(?,?,'EXIT','FILLED',?,?,?,?,?,?,?,?)",(order_id,account["id"],json.dumps({"reason":reason}),self.clock(),self.clock(),position["id"],amount(fill),amount(quantity),amount(fee),reason))
        db.execute("UPDATE paper_positions SET status='CLOSED',exit_order_id=?,exit_price=?,exit_fee=?,closed_ms=?,profit_loss=?,reason=? WHERE id=?",(order_id,amount(fill),amount(fee),timestamp,amount(net),reason,position["id"])); db.execute("UPDATE paper_accounts SET balance=? WHERE id=?",(amount(Decimal(account["balance"])+sign*(fill-Decimal(position["entry_price"]))*quantity-fee),account["id"])); self._snapshot(db,account["id"],mark_price,reason,timestamp); return dict(db.execute("SELECT * FROM paper_positions WHERE id=?",(position["id"],)).fetchone())

    def close(self,account_id,position_id,key,mark_price):
        db=self._tx()
        try:
            result=self._receipt(db,account_id,key,{"close":position_id},lambda:self._close(db,self._position(db,account_id,position_id),mark_price,"MANUAL_CLOSE",self.clock())); db.commit(); return result
        except Exception: db.rollback(); raise
        finally: db.close()

    def protect(self,account_id,position_id,request,key,mark_price):
        request=ProtectionRequest.model_validate(request); db=self._tx()
        try:
            def action():
                position=self._position(db,account_id,position_id); self._guard_protection(position["direction"],mark_price,request.stop_loss,request.take_profit); db.execute("UPDATE paper_positions SET stop_loss=?,take_profit=? WHERE id=?",(amount(request.stop_loss) if request.stop_loss else None,amount(request.take_profit) if request.take_profit else None,position_id)); return dict(db.execute("SELECT * FROM paper_positions WHERE id=?",(position_id,)).fetchone())
            result=self._receipt(db,account_id,key,{"protect":position_id,**request.model_dump(mode="json")},action); db.commit(); return result
        except Exception: db.rollback(); raise
        finally: db.close()

    def mark(self, mark_price, timestamp=None):
        """Called only by the existing trusted market collector. Stops use observed price, including gaps."""
        mark_price=Decimal(str(mark_price)); timestamp=timestamp or self.clock(); db=self._tx()
        try:
            rows=db.execute("SELECT * FROM paper_positions WHERE status='OPEN' ORDER BY opened_ms,id").fetchall()
            for account_id in {p["account_id"] for p in rows}: self._snapshot(db,account_id,mark_price,"MARK",timestamp)
            closed=0
            for p in rows:
                sign=1 if p["direction"]=="LONG" else -1; stop=p["stop_loss"]; take=p["take_profit"]
                reason="STOP_LOSS" if stop and sign*(mark_price-Decimal(stop))<=0 else "TAKE_PROFIT" if take and sign*(mark_price-Decimal(take))>=0 else None
                if reason: self._close(db,p,mark_price,reason,timestamp); closed+=1
            db.commit(); return {"closed_positions":closed}
        except Exception: db.rollback(); raise
        finally: db.close()

    def account(self,account_id,mark_price):
        with connect(self.db_path) as db: return self._value(db,account_id,Decimal(str(mark_price)))

    def records(self,account_id,kind,limit=100):
        tables={"orders":"paper_orders","positions":"paper_positions","trades":"paper_positions","equity":"paper_equity"}
        if kind not in tables: raise PaperError("NOT_FOUND","Record type not found",404)
        if not 1<=limit<=500: raise PaperError("INVALID_PAGE","limit must be 1..500",422)
        where="account_id=?" + (" AND status='OPEN'" if kind=="positions" else " AND status='CLOSED'" if kind=="trades" else "")
        ordering="closed_ms DESC" if kind=="trades" else "sequence DESC" if kind=="equity" else "created_ms DESC" if kind=="orders" else "opened_ms DESC"
        with connect(self.db_path) as db: self._account(db,account_id); return [dict(x) for x in db.execute(f"SELECT * FROM {tables[kind]} WHERE {where} ORDER BY {ordering} LIMIT ?",(account_id,limit))]

    def metrics(self,account_id):
        with connect(self.db_path) as db:
            account=self._account(db,account_id); pnl=[Decimal(x[0]) for x in db.execute("SELECT profit_loss FROM paper_positions WHERE account_id=? AND status='CLOSED'",(account_id,))]; wins=[x for x in pnl if x>0]; losses=[x for x in pnl if x<0]; gross_win=sum(wins,ZERO); gross_loss=-sum(losses,ZERO); peak=Decimal(account["initial_balance"]); max_dd=ZERO
            for row in db.execute("SELECT equity FROM paper_equity WHERE account_id=? ORDER BY sequence",(account_id,)):
                equity=Decimal(row[0]); peak=max(peak,equity); max_dd=max(max_dd,peak-equity)
            return {"trade_count":len(pnl),"wins":len(wins),"losses":len(losses),"breakeven":len(pnl)-len(wins)-len(losses),"win_rate":amount(Decimal(len(wins))/len(pnl)) if pnl else amount(0),"profit_factor":amount(gross_win/gross_loss) if gross_loss else None,"profit_factor_state":"FINITE" if gross_loss else "NO_LOSSES" if wins else "NO_DECISIVE_TRADES","payoff_ratio":amount((gross_win/len(wins))/(gross_loss/len(losses))) if wins and losses else None,"closed_trade_pnl":amount(sum(pnl,ZERO)),"max_drawdown":amount(max_dd),"max_drawdown_fraction":amount(max_dd/peak) if peak else amount(0),"drawdown_basis":"OBSERVED_MARK_TO_MARKET_EQUITY"}


def require_paper_key(authorization: str | None = Header(default=None)):
    expected=os.getenv("PAPER_API_KEY")
    if not expected: raise HTTPException(status_code=503,detail="Paper Trading API is not configured")
    if authorization != f"Bearer {expected}": raise HTTPException(status_code=401,detail="Invalid Paper Trading API credential",headers={"WWW-Authenticate":"Bearer"})
