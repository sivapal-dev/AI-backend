from pymongo import MongoClient

uri = "mongodb+srv://by8tech:ChangePassword%4012@cluster0.uhfwy.mongodb.net/by8flow"
client = MongoClient(uri)
db = client["by8flow"]

# Check oauth_tokens collection
print("=== oauth_tokens collection ===")
doc = db["oauth_tokens"].find_one({"_id": "google_calendar"})
if doc:
    print(f"Found token document:")
    print(f"  _id: {doc.get('_id')}")
    print(f"  client_id: {doc.get('client_id')}")
    print(f"  refresh_token present: {bool(doc.get('refresh_token'))}")
    print(f"  token_expiry: {doc.get('token_expiry')}")
    print(f"  access_token (first 20 chars): {doc.get('access_token', '')[:20]}")
else:
    print("No token document found")

# Also check oauth_states
print("\n=== oauth_states collection ===")
states = list(db["oauth_states"].find())
if states:
    for s in states:
        print(f"State: {s.get('_id')}, user_id: {s.get('user_id')}, created_at: {s.get('created_at')}")
else:
    print("No oauth_states documents")
