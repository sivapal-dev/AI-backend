from pymongo import MongoClient
import os

uri = "mongodb+srv://by8tech:ChangePassword%4012@cluster0.uhfwy.mongodb.net/by8flow"
client = MongoClient(uri)
db = client["by8flow"]

doc = db["oauth_tokens"].find_one({"_id": "google_calendar"})
if doc:
    print("Found token document:")
    print(f"  client_id: {doc.get('client_id')}")
    print(f"  client_secret: {doc.get('client_secret', '<present>')[:20]}...")
    print(f"  access_token: {doc.get('access_token', '<present>')[:30]}...")
    print(f"  refresh_token: {doc.get('refresh_token', '<missing>')[:30] if doc.get('refresh_token') else '<missing>'}...")
    print(f"  token_expiry: {doc.get('token_expiry')}")
else:
    print("No token document found in oauth_tokens collection")
