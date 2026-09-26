"""登录会话和角色权限。"""
from __future__ import annotations
import hashlib,hmac,secrets,sqlite3
from dataclasses import dataclass
from datetime import datetime,timezone
from .storage import utcnow
PERMISSIONS={"viewer":{"read"},"operator":{"read","measure","treatment_ticket","allocate"},"engineer":{"read","measure","treatment_ticket","allocate","analyze"},"quality":{"read","measure","treatment_ticket","allocate","analyze","approve"},"admin":{"read","measure","treatment_ticket","allocate","analyze","approve","admin"}}
@dataclass(frozen=True)
class Principal: user_id: str; role: str
def _digest(password,salt): return hashlib.pbkdf2_hmac("sha256",password.encode(),salt.encode(),70000).hex()
class Auth:
    def __init__(self,db):
        self.db=db; self.db.execute("CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY,role TEXT NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL)"); self.db.execute("CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires_at TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1)"); self.db.commit()
    def create_user(self,user_id,password,role="operator"):
        if role not in PERMISSIONS or len(password)<8: raise ValueError("invalid role or password")
        salt=secrets.token_hex(16); self.db.execute("INSERT INTO users VALUES(?,?,?,?,1,?)",(user_id,role,salt,_digest(password,salt),utcnow())); self.db.commit(); return Principal(user_id,role)
    def login(self,user_id,password):
        row=self.db.execute("SELECT role,salt,password_hash,active FROM users WHERE user_id=?",(user_id,)).fetchone()
        if not row or not row[3] or not hmac.compare_digest(_digest(password,row[1]),row[2]): raise PermissionError("invalid credentials")
        token=secrets.token_urlsafe(28); self.db.execute("INSERT INTO sessions VALUES(?,?,datetime('now','+8 hours'),1)",(token,user_id)); self.db.commit(); return token
    def current(self,token):
        row=self.db.execute("SELECT u.user_id,u.role,u.active,s.active,s.expires_at FROM sessions s JOIN users u ON u.user_id=s.user_id WHERE s.token=?",(token,)).fetchone()
        if not row or not row[2] or not row[3]: raise PermissionError("session is inactive")
        if datetime.fromisoformat(row[4]).replace(tzinfo=timezone.utc)<=datetime.now(timezone.utc): raise PermissionError("session expired")
        return Principal(row[0],row[1])
    def require(self,token,permission):
        p=self.current(token)
        if permission not in PERMISSIONS.get(p.role,set()): raise PermissionError("permission denied")
        return p
    def deactivate(self,user_id): self.db.execute("UPDATE users SET active=0 WHERE user_id=?",(user_id,)); self.db.execute("UPDATE sessions SET active=0 WHERE user_id=?",(user_id,)); self.db.commit()
