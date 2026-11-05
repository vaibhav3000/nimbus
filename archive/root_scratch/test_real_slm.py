from edge.slm.engine import EdgeSLMEngine

engine = EdgeSLMEngine(mode="auto")
print("Engine initialized mode:", engine.mode)
print("Model name:", engine.model_name)

test_context = {
    "lease_id": "lease-live-test",
    "version": 1,
    "time_remaining_sec": 540.0,
    "robot_id": "R4",
    "current_pos": [-4.5, 3.0],
    "current_zone": "west_mid_hall",
    "event_description": "Maintenance cart stopped in aisle",
    "allowed_waypoints": ["west_south_hall", "west_waiting_approach"],
    "allowed_zones": ["W-STORE", "W2", "2C"],
}

decision, decided_by, latency_ms = engine.evaluate_decision(test_context)
print("--- TEST RESULT ---")
print("Decided By :", decided_by)
print("Action     :", decision.get("action"))
print("Reason     :", decision.get("reason"))
print("Latency ms :", round(latency_ms, 2))
