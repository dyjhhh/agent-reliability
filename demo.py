"""Synthetic timeline, explicit clock, temporary database, no messages sent."""
from pathlib import Path
from tempfile import TemporaryDirectory
from reliability import AlertGate, job_health


def main():
    with TemporaryDirectory() as temp:
        gate = AlertGate(Path(temp) / "demo.sqlite")
        try:
            for event_id, at, uptime, healthy in [
                ("boot", 100, 100, True),
                ("host", 1400, 1400, False),
                ("first", 2000, 2000, True),
                ("too-soon", 2060, 2060, True),
                ("confirmed", 2180, 2180, True),
            ]:
                row = gate.observe(event_id, "demo-job", "yellow", at, uptime, healthy)
                print(f"{event_id:12} {row['decision']:8} {row['reason']}")
            gate.acknowledge("confirmed", delivered=False, at=2181)
            row = gate.observe("retry", "demo-job", "yellow", 2182, 2182)
            print(f"{'retry':12} {row['decision']:8} {row['reason']} (failed delivery did not silence it)")
            gate.acknowledge("retry", delivered=True, at=2183)
            for event_id, severity, at in [("duplicate", "yellow", 2184), ("escalation", "red", 2185), ("recovery", "clear", 2186)]:
                row = gate.observe(event_id, "demo-job", severity, at, at)
                print(f"{event_id:12} {row['decision']:8} {row['reason']}")
        finally:
            gate.close()
    print("Job evidence:", job_health(4000, None, 600), "/", job_health(4000, 1000, 600))
    print("Delivery acknowledgements above are simulated. No notification was sent.")


if __name__ == "__main__":
    main()
