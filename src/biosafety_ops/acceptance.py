"""离线命令行验收入口。"""
from __future__ import annotations
import argparse,json
from .models import MonitoringRecord,ZoneRecord
from .service import BiosafetyService
def run():
    s=BiosafetyService(); s.bootstrap(); t=s.auth.login("admin","biosafety-admin"); s.register_zone_record(t,ZoneRecord("CASE-DEMO","north","water",680,5)); r=s.ingest_monitoring_record(t,MonitoringRecord("RD-DEMO","CASE-DEMO","sensor_source-01",160,230,88,"2026-09-24T10:00:00+00:00")); report=s.risk_report(t,"CASE-DEMO"); order=s.create_treatment_ticket(t,"CASE-DEMO",r["alert_id"],"crew-north",1); s.add_preservation_resource(t,"PUMP-01","mobile-cold-box","north",2); allocation=s.allocate(t,"PUMP-01",order["treatment_ticket_id"],1); return {"status":"ok","zone_record":"CASE-DEMO","severity":r["risk"]["severity"],"probability":report["violation_probability"],"allocation":allocation["plan_id"]}
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--workspace",default="."); parser.parse_args(); print(json.dumps(run(),ensure_ascii=False))
if __name__=="__main__":main()
