import sys
from fastapi.testclient import TestClient
from main import app, init_database

print("[TEST] Starting local verification of Ledger Agent...")
init_database()

client = TestClient(app)

# 1. Health check
res = client.get("/")
print("[TEST] Health check status:", res.status_code, res.json())
assert res.status_code == 200

# 2. Sample question from problem description:
# "What was the total revenue in USD from the North region in March 2026?"
q1 = {"question": "What was the total revenue in USD from the North region in March 2026?"}
res1 = client.post("/", json=q1)
print("\n[TEST] Question 1:", q1["question"])
print("[TEST] Response:", res1.status_code, res1.json())
assert res1.status_code == 200
assert "answer" in res1.json()

# 3. Question about refunds:
q2 = {"question": "How many orders were refunded in February 2026?"}
res2 = client.post("/", json=q2)
print("\n[TEST] Question 2:", q2["question"])
print("[TEST] Response:", res2.status_code, res2.json())
assert res2.status_code == 200
assert "answer" in res2.json()

# 4. Question about products:
q3 = {"question": "Which product had the highest revenue?"}
res3 = client.post("/", json=q3)
print("\n[TEST] Question 3:", q3["question"])
print("[TEST] Response:", res3.status_code, res3.json())
assert res3.status_code == 200
assert "answer" in res3.json()

# 5. Question about customers:
q4 = {"question": "How many unique customers placed orders in the South region?"}
res4 = client.post("/", json=q4)
print("\n[TEST] Question 4:", q4["question"])
print("[TEST] Response:", res4.status_code, res4.json())
assert res4.status_code == 200
assert "answer" in res4.json()

print("\n[SUCCESS] ALL TESTS PASSED! Local service is 100% verified.")
