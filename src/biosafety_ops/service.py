"""协调转运生物安全监测、告警、工单和应急资源分配的应用服务。"""
from __future__ import annotations
import hashlib,uuid
from .auth import Auth
from .models import MonitoringRecord,ZoneRecord,as_dict,utcnow
from .risk import violation_probability,score_monitoring_record
from .storage import audit,connect,rows,transaction
class BiosafetyService:
    def __init__(self,database=":memory:"): self.db=connect(database); self.auth=Auth(self.db)
    def bootstrap(self):
        for uid,pwd,role in (("admin","biosafety-admin","admin"),("operator","biosafety-operator","operator")):
            try:self.auth.create_user(uid,pwd,role)
            except Exception:pass
    def register_zone_record(self,token,zone_record):
        actor=self.auth.require(token,"admin"); zone_record.validate(); now=utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO zone_records VALUES(?,?,?,?,?,?,?,?)",(zone_record.zone_record_id,zone_record.collection_zone,zone_record.biosafety_type,zone_record.length_m,zone_record.criticality,zone_record.status,now,now)); audit(self.db,"zone_record",zone_record.zone_record_id,"created",actor.user_id,as_dict(zone_record))
        return self.zone_record(token,zone_record.zone_record_id)
    def zone_record(self,token,zone_record_id):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM zone_records WHERE zone_record_id=?",(zone_record_id,)).fetchone()
        if not row:raise KeyError(zone_record_id)
        return dict(row)
    def ingest_monitoring_record(self,token,monitoring_record):
        actor=self.auth.require(token,"measure"); monitoring_record.validate(); seg=self.db.execute("SELECT criticality FROM zone_records WHERE zone_record_id=?",(monitoring_record.zone_record_id,)).fetchone()
        if not seg:raise KeyError(monitoring_record.zone_record_id)
        risk=score_monitoring_record(monitoring_record.speed_kmh,monitoring_record.traffic_flow_vph,monitoring_record.impact_index,seg[0]); fingerprint=hashlib.sha256(f"{monitoring_record.zone_record_id}|{monitoring_record.sensor_source_id}|{monitoring_record.observed_at}".encode()).hexdigest()
        with transaction(self.db):
            if self.db.execute("SELECT monitoring_record_id FROM monitoring_records WHERE monitoring_record_id=?",(monitoring_record.monitoring_record_id,)).fetchone(): return {"monitoring_record_id":monitoring_record.monitoring_record_id,"duplicate":True,"risk":as_dict(risk)}
            self.db.execute("INSERT INTO monitoring_records VALUES(?,?,?,?,?,?,?)",(monitoring_record.monitoring_record_id,monitoring_record.zone_record_id,monitoring_record.sensor_source_id,monitoring_record.speed_kmh,monitoring_record.traffic_flow_vph,monitoring_record.impact_index,monitoring_record.observed_at)); alert_id=None
            if risk.severity in {"high","critical"}:
                alert_id="alert-"+fingerprint[:18]; self.db.execute("INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?,?,?,?)",(alert_id,monitoring_record.zone_record_id,fingerprint,risk.severity,risk.score,"open",utcnow(),None))
            audit(self.db,"monitoring_record",monitoring_record.monitoring_record_id,"ingested",actor.user_id,{"risk":as_dict(risk),"alert_id":alert_id})
        return {"monitoring_record_id":monitoring_record.monitoring_record_id,"duplicate":False,"risk":as_dict(risk),"alert_id":alert_id}
    def risk_report(self,token,zone_record_id):
        self.auth.require(token,"analyze"); monitoring_records=rows(self.db,"SELECT * FROM monitoring_records WHERE zone_record_id=? ORDER BY observed_at",(zone_record_id,)); alerts=rows(self.db,"SELECT * FROM alerts WHERE zone_record_id=? ORDER BY created_at",(zone_record_id,)); return {"zone_record_id":zone_record_id,"monitoring_records":len(monitoring_records),"alerts":alerts,"violation_probability":violation_probability(alerts)}
    def create_treatment_ticket(self,token,zone_record_id,alert_id,assignee,priority=3):
        actor=self.auth.require(token,"treatment_ticket")
        if not assignee.strip() or not 1<=priority<=5:raise ValueError("assignee and priority are invalid")
        if not self.db.execute("SELECT 1 FROM alerts WHERE alert_id=? AND zone_record_id=?",(alert_id,zone_record_id)).fetchone():raise KeyError(alert_id)
        wid="wo-"+uuid.uuid4().hex[:16]
        with transaction(self.db): self.db.execute("INSERT INTO treatment_tickets VALUES(?,?,?,?,?,?,?,?)",(wid,zone_record_id,alert_id,assignee,"open",priority,utcnow(),utcnow())); audit(self.db,"treatment_ticket",wid,"created",actor.user_id,{"zone_record_id":zone_record_id,"alert_id":alert_id})
        return self.treatment_ticket(token,wid)
    def treatment_ticket(self,token,treatment_ticket_id):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM treatment_tickets WHERE treatment_ticket_id=?",(treatment_ticket_id,)).fetchone()
        if not row:raise KeyError(treatment_ticket_id)
        return dict(row)
    def transition_treatment_ticket(self,token,treatment_ticket_id,target,reason):
        actor=self.auth.require(token,"treatment_ticket"); allowed={"open":{"assigned","cancelled"},"assigned":{"in_progress","cancelled"},"in_progress":{"completed","blocked"},"blocked":{"in_progress","cancelled"},"completed":set(),"cancelled":set()}
        if not reason.strip():raise ValueError("transition reason is required")
        with transaction(self.db):
            row=self.db.execute("SELECT status FROM treatment_tickets WHERE treatment_ticket_id=?",(treatment_ticket_id,)).fetchone()
            if not row:raise KeyError(treatment_ticket_id)
            if target not in allowed.get(row[0],set()):raise ValueError("invalid work order transition")
            self.db.execute("UPDATE treatment_tickets SET status=?,updated_at=? WHERE treatment_ticket_id=?",(target,utcnow(),treatment_ticket_id)); audit(self.db,"treatment_ticket",treatment_ticket_id,"transition",actor.user_id,{"from":row[0],"to":target,"reason":reason})
        return self.treatment_ticket(token,treatment_ticket_id)
    def add_preservation_resource(self,token,preservation_resource_id,kind,collection_zone,capacity):
        actor=self.auth.require(token,"admin")
        if capacity<=0 or not kind.strip() or not collection_zone.strip():raise ValueError("preservation_resource fields are invalid")
        with transaction(self.db):self.db.execute("INSERT INTO preservation_resources VALUES(?,?,?,?,?)",(preservation_resource_id,kind,collection_zone,capacity,capacity)); audit(self.db,"preservation_resource",preservation_resource_id,"created",actor.user_id,{"kind":kind,"collection_zone":collection_zone,"capacity":capacity})
        return self.preservation_resource(token,preservation_resource_id)
    def preservation_resource(self,token,preservation_resource_id):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM preservation_resources WHERE preservation_resource_id=?",(preservation_resource_id,)).fetchone()
        if not row:raise KeyError(preservation_resource_id)
        return dict(row)
    def allocate(self,token,preservation_resource_id,treatment_ticket_id,quantity):
        actor=self.auth.require(token,"allocate")
        if quantity<=0:raise ValueError("quantity must be positive")
        aid="alloc-"+uuid.uuid4().hex[:16]
        with transaction(self.db):
            preservation_resource=self.db.execute("SELECT available FROM preservation_resources WHERE preservation_resource_id=?",(preservation_resource_id,)).fetchone()
            if not preservation_resource:raise KeyError(preservation_resource_id)
            if not self.db.execute("SELECT 1 FROM treatment_tickets WHERE treatment_ticket_id=?",(treatment_ticket_id,)).fetchone():raise KeyError(treatment_ticket_id)
            if preservation_resource[0]<quantity:raise ValueError("preservation_resource capacity exceeded")
            old=self.db.execute("SELECT plan_id FROM allocations WHERE preservation_resource_id=? AND treatment_ticket_id=?",(preservation_resource_id,treatment_ticket_id)).fetchone()
            if old:return {"plan_id":old[0],"duplicate":True}
            self.db.execute("INSERT INTO allocations VALUES(?,?,?,?,?)",(aid,preservation_resource_id,treatment_ticket_id,quantity,utcnow())); self.db.execute("UPDATE preservation_resources SET available=available-? WHERE preservation_resource_id=?",(quantity,preservation_resource_id)); audit(self.db,"preservation_resource",preservation_resource_id,"allocated",actor.user_id,{"treatment_ticket_id":treatment_ticket_id,"quantity":quantity})
        return {"plan_id":aid,"duplicate":False,"preservation_resource_id":preservation_resource_id,"quantity":quantity}
    def audit_events(self,token,entity_type,entity_id): self.auth.require(token,"read"); return rows(self.db,"SELECT * FROM audit_events WHERE entity_type=? AND entity_id=? ORDER BY event_id",(entity_type,entity_id))
